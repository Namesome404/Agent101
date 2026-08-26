# -*- coding: utf-8 -*-
"""语音终端音频 IO：本机单麦采集/RTSP 麦/本机喇叭输出/回声参考。

持有输出流、监听静音、设备开关缓存等本模块私有的可变状态；
被 main() 与 spec 模块调用。
"""
from __future__ import annotations

import audioop
import os
import queue
import subprocess
import threading
import time
import wave

from speech.voice_core import speak_duplex_segments as _core_speak_duplex

from devices.voice.terminal_chat import _publish_voice_stage
from devices.voice.terminal_echo import _ECHO_GATE
from devices.voice.terminal_log import _diag_event, _stage_log, log
from devices.voice.terminal_state import (
    AGENT_ID,
    CAMERA,
    FRAME_BYTES,
    FRAME_MS,
    GREET_TEXT,
    INPUT_GAIN,
    INPUT_MODE,
    MUSE,
    OUTPUT,
    RTSP_LOW_LATENCY,
    SR,
    TMP,
    _BARGE_IN_EVENT,
    _LISTEN_MUTE_AFTER_PLAY,
    _MIC_Q,
    _label_disabled,
    _label_in_list,
    _label_match,
    _speak_lock,
    _http,
)
from devices.camera import audio

_Q_DROP_COUNT = 0
_Q_DROP_REPORT = time.time()

# ---- 麦克风链路诊断计数（找「说完话好几秒状态栏才更新」的丢帧点）----
_MIXER_PUSHED = 0      # mixer 成功推入主队列的帧数
_ECHO_SWALLOWED = 0    # echo gate 返回全零/空帧被丢弃的帧数
_MIXER_TICK = time.time()   # mixer 存活心跳（每轮更新）
_MIXER_LAST_DIAG = time.time()
_MIXER_ALIVE = False   # mixer 线程是否还活着
_MIXER_EXC = None      # mixer 线程异常

_OUTPUT_STREAM = None
_OUTPUT_SAMPLE_RATE = None
_OUTPUT_CHANNELS = 1
_OUTPUT_LAST_USED_AT = 0.0
_OUTPUT_LOCK = threading.Lock()
_LISTEN_MUTED_UNTIL = 0.0

_feat_cache = {"t": 0.0, "greet": True, "voice": True, "mic": True, "speaker": True}
_host_audio_cache = {"t": 0.0, "prefs": None}


def _drain_mic():
    if _MIC_Q is None:
        return
    while True:
        try: _MIC_Q.get_nowait()
        except queue.Empty: break


def _mute_listen(seconds):
    """播放本机喇叭期间/之后短暂静音聆听，避免把自己的声音当用户话。"""
    global _LISTEN_MUTED_UNTIL
    hold = max(0.0, float(seconds or 0.0))
    _LISTEN_MUTED_UNTIL = max(_LISTEN_MUTED_UNTIL, time.time() + hold)


def _unmute_listen():
    """打断生效后立刻恢复聆听，好让插话进入 ASR。"""
    global _LISTEN_MUTED_UNTIL
    _LISTEN_MUTED_UNTIL = 0.0


def _listen_muted():
    # 已判定真人打断时，硬静音失效——否则会把插话整段丢掉
    if _BARGE_IN_EVENT.is_set():
        return False
    return time.time() < _LISTEN_MUTED_UNTIL


def _is_self_echo_text(text):
    """识别结果是否像刚播过的问候/助手套话（回声）。"""
    from devices.voice.terminal_state import _normalized_command as _norm
    a = _norm(text)
    if not a:
        return False
    for candidate in (GREET_TEXT, "有什么需要帮您的吗", "有什么可以帮您", "有什么能帮您的"):
        b = _norm(candidate)
        if not b:
            continue
        if a == b or a in b or b in a:
            return True
    return False


def _host_audio_prefs():
    """设备页本机麦/喇叭启停（与浏览器 localStorage 同步到 DB）。

    麦克风输入是单选：active_mic_labels 最多采用第一项；没有明确选择时
    跟随系统默认输入。采集端还会再次硬限制为一条流，防止旧配置同时开多麦。
    """
    now = time.time()
    if now - _host_audio_cache["t"] <= 1.5 and _host_audio_cache["prefs"] is not None:
        return _host_audio_cache["prefs"]
    prefs = {
        "mic_label": "",
        "disabled_mic_labels": [],
        "active_mic_labels": [],
        "spk_label": "",
        "disabled_spk_labels": [],
        "rescan_token": "",
    }
    try:
        from control_plane import database as db
        import json as _json
        prefs["mic_label"] = str(db.get_setting("host.audio.mic_label", "") or "").strip()
        prefs["spk_label"] = str(db.get_setting("host.audio.spk_label", "") or "").strip()
        prefs["rescan_token"] = str(db.get_setting("host.audio.rescan_token", "") or "").strip()
        for key, field in (
            ("host.audio.disabled_mic_labels", "disabled_mic_labels"),
            ("host.audio.disabled_spk_labels", "disabled_spk_labels"),
            ("host.audio.active_mic_labels", "active_mic_labels"),
        ):
            raw = db.get_setting(key, "[]") or "[]"
            try:
                data = _json.loads(raw)
                prefs[field] = [str(x).strip() for x in data if str(x).strip()]
            except Exception:
                prefs[field] = []
    except Exception:
        pass
    _host_audio_cache["prefs"] = prefs
    _host_audio_cache["t"] = now
    return prefs


def _feat(name):
    """读主控里的开关(视频问候/语音应答/本机麦)，约 2s 缓存，无需重启即可生效。"""
    now = time.time()
    if now - _feat_cache["t"] > 2:
        try:
            from control_plane import database as db
            _feat_cache["greet"] = db.get_setting("feat.camera_greet", "1") == "1"
            # feat.voice 为新键；兼容 feat.camera_voice / feat.voice_terminal
            voice_raw = db.get_setting("feat.voice", None)
            if voice_raw is None:
                voice_raw = db.get_setting("feat.voice_terminal", None)
            if voice_raw is None:
                voice_raw = db.get_setting("feat.camera_voice", "1")
            _feat_cache["voice"] = str(voice_raw or "1") == "1"
            if INPUT_MODE == "pc":
                try:
                    _feat_cache["mic"] = bool(_list_enabled_pc_input_devices())
                except Exception:
                    _feat_cache["mic"] = True
            else:
                cam = None
                if CAMERA:
                    cam = db.get_camera(CAMERA)
                if not cam:
                    cam = db.get_agent_camera(AGENT_ID, require_cap=None)
                _feat_cache["mic"] = True if not cam else db.device_capability_enabled(cam, "mic")
            # 本机扬声器：设备页禁用列表 / 选用
            if OUTPUT != "camera":
                prefs = _host_audio_prefs()
                selected_spk = prefs.get("spk_label") or os.environ.get(
                    "VOICE_OUTPUT_DEVICE", ""
                ).strip()
                disabled_spk = prefs.get("disabled_spk_labels") or []
                if selected_spk and _label_disabled(selected_spk, disabled_spk):
                    _feat_cache["speaker"] = False
                else:
                    try:
                        picked = _pick_pc_output_device()
                        _feat_cache["speaker"] = picked is not None
                    except Exception:
                        _feat_cache["speaker"] = True
            else:
                _feat_cache["speaker"] = True
        except Exception:
            pass
        _feat_cache["t"] = now
    return _feat_cache.get(name, True)


def _drain_queue(q):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break


# ---------- 麦克风流 ----------
def _push_mic_frame(q, frame):
    global _Q_DROP_COUNT, _Q_DROP_REPORT
    try:
        b = _ECHO_GATE.process(frame)
    except Exception as exc:
        # echo gate 异常绝不让麦克风帧被吞：直接放行原始帧
        _diag_event("echo_gate_error", error=str(exc)[:300])
        b = frame
    try:
        preroll = _ECHO_GATE.take_preroll()
    except Exception:
        preroll = []
    if preroll:
        _unmute_listen()
        # 丢掉播放期灌进去的静音，让插话预录立刻被主循环吃到
        _drain_queue(q)
        output_frames = list(preroll)
        if b and (not output_frames or output_frames[-1] != b):
            output_frames.append(b)
    elif _BARGE_IN_EVENT.is_set():
        _unmute_listen()
        output_frames = [b]
    else:
        # 播放/回声保护期 echo gate 会吐全零帧：别塞进队列，否则打断后
        # ASR 先吞几百毫秒静音。
        # 平时（无播放）静音帧必须推入主循环——VAD 判停靠 ring 里的静音帧
        # 积累；若全吞掉，用户说完后攒不出静音，判停只能等时长硬上限，
        # 表现就是「说完话好几秒状态栏才更新」。
        if _ECHO_GATE.is_active() and (
            not b or b == _ECHO_GATE.zero_frame or not any(b)
        ):
            global _ECHO_SWALLOWED
            _ECHO_SWALLOWED += 1
            return
        output_frames = [b]
    for output_frame in output_frames:
        try:
            q.put_nowait(output_frame)
        except queue.Full:
            _Q_DROP_COUNT += 1
            now = time.time()
            if now - _Q_DROP_REPORT >= 15.0:
                log(
                    "[diag] 主循环队列已满，丢帧 %d 次（主循环消费过慢）"
                    % _Q_DROP_COUNT
                )
                _Q_DROP_REPORT = now
                _Q_DROP_COUNT = 0
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(output_frame)
            except queue.Full:
                pass


def _list_enabled_pc_input_devices():
    """返回唯一一台本机输入设备；旧的多选配置也只采用第一项。"""
    import sounddevice as sd
    prefs = _host_audio_prefs()
    disabled = prefs.get("disabled_mic_labels") or []
    # 可选白名单：能对上 PortAudio 名才生效，否则只尊重「关闭」列表
    active = prefs.get("active_mic_labels") or []
    candidates = []
    for i, d in enumerate(list(sd.query_devices())):
        if int(d.get("max_input_channels") or 0) <= 0:
            continue
        name = str(d.get("name") or "")
        low = name.lower()
        if "iphone" in low or "continuity" in low:
            continue
        if _label_disabled(name, disabled):
            continue
        candidates.append((i, name))
    if active:
        # 旧版本的设备页会把所有开启的麦都写进来。按保存顺序取第一个能匹配的，
        # 绝不把多项继续传给采集层。明确选中的设备暂时不可见时返回空，
        # 不能静默改用列表里另一只麦克风。
        for wanted in active:
            matched = [(i, n) for i, n in candidates if _label_in_list(n, [wanted])]
            if matched:
                candidates = matched
                break
        else:
            return []
    return _drop_output_device_mic(candidates)


def _drop_output_device_mic(candidates):
    """从候选中选出唯一麦克风，优先跟随系统默认输入。

    此前一律「正在放音的耳机不兼当麦克风」——这条在用户把系统输入也切到耳机
    之后就是帮倒忙：系统输入已经是 AirPods，笔记本麦近乎静音（打断记录里
    near_rms 只有 1~21，而说话时本该上万），于是它听不见人说话。
    现在的规则：系统默认输入是谁就用谁；只有系统没把放音设备当输入时，
    才避开它（那种情况下两路同开会互相干扰）。
    """
    if len(candidates) <= 1:
        return candidates
    now = time.monotonic()
    if now - float(_SYSTEM_DEFAULT_OUTPUT.get("at") or 0) > 10:
        mic_name, spk_name = _system_default_audio_names()
        _SYSTEM_DEFAULT_OUTPUT["name"] = spk_name
        _SYSTEM_DEFAULT_OUTPUT["input"] = mic_name
        _SYSTEM_DEFAULT_OUTPUT["at"] = now
    default_in = str(_SYSTEM_DEFAULT_OUTPUT.get("input") or "").strip()
    if default_in:
        chosen = [(i, n) for i, n in candidates if str(n).strip().lower() == default_in.lower()]
        if chosen:
            if getattr(_drop_output_device_mic, "_last_in", None) != default_in:
                _drop_output_device_mic._last_in = default_in
                log("麦克风：跟随系统默认输入 %s" % default_in)
            return chosen[:1]
    playing = str(_SYSTEM_DEFAULT_OUTPUT.get("name") or "").strip()
    if not playing:
        return candidates[:1]
    kept = [(i, n) for i, n in candidates if str(n).strip().lower() != playing.lower()]
    if kept and len(kept) != len(candidates):
        if getattr(_drop_output_device_mic, "_last", None) != playing:
            _drop_output_device_mic._last = playing
            log("麦克风：跳过正在放音的 %s（同一只耳机既放又收会互相干扰）" % playing)
        return kept[:1]
    _drop_output_device_mic._last = None
    # 系统默认设备枚举失败时也只能开一台；这是输入侧的最终单实例规则。
    return candidates[:1]


def _enabled_input_signature(devices=None):
    items = devices if devices is not None else _list_enabled_pc_input_devices()
    return "|".join("%s:%s" % (i, n) for i, n in items)


def _mix_pcm16_frames(frames):
    """多路 PCM16 按采样峰值合成（谁响用谁），避免简单相加削波。"""
    import array
    usable = [f for f in frames if f and len(f) >= FRAME_BYTES]
    if not usable:
        return b"\x00" * FRAME_BYTES
    if len(usable) == 1:
        return usable[0][:FRAME_BYTES]
    arrs = []
    for f in usable:
        a = array.array("h")
        a.frombytes(f[:FRAME_BYTES])
        arrs.append(a)
    n = len(arrs[0])
    out = array.array("h", [0] * n)
    for i in range(n):
        best = 0
        best_abs = -1
        for a in arrs:
            s = a[i]
            aa = -s if s < 0 else s
            if aa > best_abs:
                best_abs = aa
                best = s
        out[i] = best
    return out.tobytes()


def _find_pc_input_device(configured):
    """按名称/索引在「已开启」列表中查找；找不到返回 None。"""
    configured = (configured or "").strip()
    if not configured:
        return None
    enabled = _list_enabled_pc_input_devices()
    if configured.lstrip("-").isdigit():
        idx = int(configured)
        for i, name in enabled:
            if i == idx:
                return i, name
        return None
    for i, name in enabled:
        if _label_match(name, configured):
            return i, name
    return None


def _pick_pc_input_device(configured=""):
    """取唯一一台已开启麦（兼容旧调用）。"""
    import sounddevice as sd
    if configured:
        found = _find_pc_input_device(configured)
        if found is not None:
            return found
    enabled = _list_enabled_pc_input_devices()
    if enabled:
        return enabled[0]
    devices = list(sd.query_devices())
    default = sd.default.device
    idx = default[0] if isinstance(default, (list, tuple)) else default
    try:
        name = devices[int(idx)]["name"]
    except Exception:
        name = "系统默认"
    return idx, name


class _PcMicProc:
    """本机麦克风；任意时刻最多持有一条输入流。"""

    def __init__(self):
        self._streams = []
        self._q = None
        self._pending = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._mixer = None
        self._dead = False
        self._error = None
        self.device_label = ""
        self.device_labels = []
        self.devices_sig = ""

    def bind_queue(self, q):
        self._q = q

    def start(self):
        import sounddevice as sd
        # 即使配置层或后续重构意外返回多项，采集层也硬性只打开第一台。
        devices = _list_enabled_pc_input_devices()[:1]
        if not devices:
            self._dead = True
            self.device_label = ""
            self.device_labels = []
            self.devices_sig = ""
            raise RuntimeError("没有已开启的本机麦克风（请在设备页打开至少一个）")
        block = max(1, FRAME_BYTES // 2)
        self._pending = {idx: bytearray() for idx, _ in devices}

        def _make_callback(device_idx):
            def callback(indata, frames, _time, status):
                if self._q is None or self._dead:
                    return
                raw = bytes(indata)
                if INPUT_GAIN != 1.0 and INPUT_GAIN > 0:
                    try:
                        raw = audioop.mul(raw, 2, INPUT_GAIN)
                    except Exception:
                        pass
                with self._cond:
                    buf = self._pending.get(device_idx)
                    if buf is None:
                        return
                    buf.extend(raw)
                    self._cond.notify()
            return callback

        opened = []
        for idx, label in devices:
            try:
                stream = sd.RawInputStream(
                    samplerate=SR,
                    channels=1,
                    dtype="int16",
                    blocksize=block,
                    device=idx,
                    callback=_make_callback(idx),
                )
                stream.start()
                self._streams.append(stream)
                opened.append((idx, label))
            except Exception as e:
                log("麦克风打开失败，跳过 %s：%s" % (label, e))
        if not opened:
            self._dead = True
            raise RuntimeError("所有已开启麦克风均无法打开")
        self.device_labels = [n for _, n in opened]
        self.device_label = " + ".join(self.device_labels)
        self.devices_sig = _enabled_input_signature(opened)
        self._dead = False
        self._mixer = threading.Thread(target=self._mixer_loop, daemon=True)
        global _MIXER_ALIVE, _MIXER_EXC
        _MIXER_ALIVE = True
        _MIXER_EXC = None
        self._mixer.start()
        log("本机麦克风已打开：%s" % self.device_label)

    def _mixer_loop(self):
        global _MIXER_PUSHED, _MIXER_TICK, _MIXER_ALIVE, _MIXER_EXC, _MIXER_LAST_DIAG
        _drop_total = 0
        _drop_count = 0
        _last_report = time.time()
        _loop_push = 0
        try:
            while not self._dead:
                # 等 callback 通知（每 20ms 一次），而不是固定 sleep——
                # sleep 在 GIL 竞争下会被拖慢 4 倍导致积压丢帧
                with self._cond:
                    self._cond.wait(timeout=0.1)
                    if self._dead:
                        break
                    _MIXER_TICK = time.time()
                    # 一次取走每路已积累的所有完整帧。原实现每 100ms 醒来每路
                    # 只取一帧，GIL 竞争下 callback 累积的 5 帧会丢掉 4 帧
                    # （帧率 150→30），用户语音随机落在被丢部分 → 间歇性听不到。
                    # 这里循环切帧，只按延迟预算（200ms）丢弃最旧帧。
                    max_frames = max(1, int(0.2 * 1000 / FRAME_MS))
                    per_device = []
                    for idx, buf in list(self._pending.items()):
                        n_frames = len(buf) // FRAME_BYTES
                        if n_frames > max_frames:
                            over = (n_frames - max_frames) * FRAME_BYTES
                            _drop_total += over
                            _drop_count += 1
                            del buf[:over]
                        n_frames = len(buf) // FRAME_BYTES
                        if n_frames:
                            frames = [
                                bytes(buf[i * FRAME_BYTES:(i + 1) * FRAME_BYTES])
                                for i in range(n_frames)
                            ]
                            del buf[:]
                            per_device.append(frames)
                now = time.time()
                if _drop_count and now - _last_report >= 15.0:
                    log(
                        "[diag] mixer 积压丢帧: %d次, 共丢弃 %.1fms 音频"
                        % (_drop_count, _drop_total / 2.0 / SR * 1000)
                    )
                    _last_report = now
                    _drop_total = 0
                    _drop_count = 0
                if not per_device:
                    # 每 3s 报一次 mixer 存活（含 pending 堆积情况），定位
                    # 「麦克风心跳帧数低」到底是 callback 没喂帧还是 mixer 没取走
                    if now - _MIXER_LAST_DIAG >= 3.0:
                        _MIXER_LAST_DIAG = now
                        pending_lens = {
                            str(idx): len(buf) // FRAME_BYTES
                            for idx, buf in list(self._pending.items())
                        }
                        _diag_event(
                            "mixer_heartbeat",
                            alive=True,
                            pending_frames=pending_lens,
                            pushed=_loop_push,
                            swallowed=_ECHO_SWALLOWED,
                        )
                    continue
                # 多路设备按同一时间位置逐帧合成；单路直接逐帧推入主队列。
                # 不能用 _mix_pcm16_frames 一次性接收多个时间帧——那是多路设备
                # 的采样峰值合成，传多个时间帧会被合成成一帧 → 仍丢帧。
                # 单帧异常（AEC/混音等）只跳过该帧，绝不让 mixer 线程死亡——
                # 线程一死麦克风帧率会掉到 3~47/3s，VAD 判停整体拖慢几秒。
                try:
                    max_len = max(len(v) for v in per_device)
                    for i in range(max_len):
                        aligned = [v[i] for v in per_device if i < len(v)]
                        frame = (
                            _mix_pcm16_frames(aligned)
                            if len(aligned) > 1
                            else aligned[0]
                        )
                        _push_mic_frame(self._q, frame)
                        _MIXER_PUSHED += 1
                        _loop_push += 1
                except Exception as exc:
                    _diag_event(
                        "mixer_frame_error",
                        error=str(exc)[:300],
                        pending={str(k): len(v) // FRAME_BYTES for k, v in list(self._pending.items())},
                    )
                if now - _MIXER_LAST_DIAG >= 3.0:
                    _MIXER_LAST_DIAG = now
                    _diag_event(
                        "mixer_heartbeat",
                        alive=True,
                        pending_frames={},
                        pushed=_loop_push,
                        swallowed=_ECHO_SWALLOWED,
                    )
                    _loop_push = 0
        except Exception as exc:
            _MIXER_EXC = str(exc)
            log("[diag] mixer 线程异常退出: %s" % exc)
        finally:
            _MIXER_ALIVE = False

    def poll(self):
        if self._dead or not self._streams:
            return 1
        alive = 0
        for stream in self._streams:
            try:
                if getattr(stream, "closed", False) or not stream.active:
                    continue
                alive += 1
            except Exception:
                continue
        return None if alive else 1

    def stop(self):
        self._dead = True
        for stream in self._streams:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self._streams = []
        with self._lock:
            self._pending = {}


def _mic_proc_camera():
    command = [
        audio._ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
    ]
    if RTSP_LOW_LATENCY:
        command.extend([
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-analyzeduration",
            "0",
            "-probesize",
            "32768",
            "-max_delay",
            "0",
        ])
    command.extend([
        "-i",
        audio.rtsp_url(CAMERA),
        "-vn",
        "-ar",
        str(SR),
        "-ac",
        "1",
    ])
    if INPUT_GAIN != 1.0:
        command.extend([
            "-af",
            "volume=%.2f" % INPUT_GAIN,
        ])
    command.extend([
        "-flush_packets",
        "1",
        "-f",
        "s16le",
        "-",
    ])
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        bufsize=0 if RTSP_LOW_LATENCY else FRAME_BYTES * 8,
    )


def _start_mic(q, stop):
    """启动麦克风源：pc → sounddevice（单实例）；camera → ffmpeg RTSP。"""
    if INPUT_MODE == "pc":
        proc = _PcMicProc()
        proc.bind_queue(q)
        proc.start()
        return proc
    proc = _mic_proc_camera()
    threading.Thread(target=_reader, args=(proc, q, stop), daemon=True).start()
    return proc


def _reader(proc, q, stop):
    pending = bytearray()
    while not stop.is_set():
        chunk = proc.stdout.read(FRAME_BYTES - len(pending))
        if not chunk:
            break
        pending.extend(chunk)
        if len(pending) < FRAME_BYTES:
            continue
        frame = bytes(pending[:FRAME_BYTES])
        del pending[:FRAME_BYTES]
        _push_mic_frame(q, frame)


# ---------- TTS 与播放 ----------
def tts(text, provider, overrides=None):
    out = os.path.join(TMP, "cam_reply.wav")
    response = _http().post(
        MUSE + "/api/tts/preview",
        json={"provider": provider, "text": text, "overrides": overrides or {}},
        timeout=(5, 150),
    )
    response.raise_for_status()
    with open(out, "wb") as f:
        f.write(response.content)
    return out


class _CameraPcmSink:
    """摄像头终端 Sink：PCM → 本机声卡 + 回声门控。"""

    def __init__(self):
        self._stream = None
        self._sample_rate = None
        self._turn_id = None

    def start(self, sample_rate, *, turn_id=None):
        if not _feat("speaker"):
            raise RuntimeError("本机扬声器已在设备页关闭")
        self._sample_rate = sample_rate
        self._turn_id = turn_id
        self._stream = _ensure_output_stream(sample_rate)
        _ECHO_GATE.begin(sample_rate, playback_id=turn_id)
        _publish_voice_stage(speaking=True, level=0.2, turn_id=turn_id)

    def write(self, pcm):
        try:
            _write_output_stream(self._stream, pcm)
        except Exception:
            _close_output_stream()
            self._stream = _ensure_output_stream(self._sample_rate or 24000)
            _write_output_stream(self._stream, pcm)
        # 旁路声波：用播放 PCM 估电平
        try:
            import array
            samples = array.array("h")
            raw = pcm or b""
            samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
            if samples:
                peak = max(abs(s) for s in samples[:: max(1, len(samples) // 64)])
                _publish_voice_stage(
                    speaking=True,
                    level=min(1.0, peak / 12000.0),
                    turn_id=self._turn_id,
                )
        except Exception:
            pass

    def stop(self):
        if _BARGE_IN_EVENT.is_set():
            _close_output_stream()
        _ECHO_GATE.finish()
        _publish_voice_stage(speaking=False, level=0.0, turn_id=self._turn_id)


def _speak_duplex_segments(
    segments,
    tts_provider,
    tts_overrides,
    q,
    turn_context=None,
    retry_count=0,
):
    """摄像头侧薄封装：VoiceCore duplex + 本机 PCM Sink。"""
    del retry_count  # 重试由 VoiceCore 内部处理
    sink = _CameraPcmSink()
    fallback_provider = os.environ.get(
        "CAMERA_TTS_FALLBACK_PROVIDER",
        "EdgeTTS",
    ).strip()

    def _fallback(remaining_segments, completed_segments):
        playback_provider = tts_provider
        playback_overrides = tts_overrides
        if fallback_provider and fallback_provider != tts_provider:
            playback_provider = fallback_provider
            playback_overrides = {}
            log("双工 TTS 失败，立即切换备用 TTS：%s" % fallback_provider)
        for index, segment in enumerate(
            remaining_segments,
            start=completed_segments + 1,
        ):
            _speak_unlocked(
                segment,
                playback_provider,
                playback_overrides,
                turn_context=turn_context,
                segment_index=index,
            )

    def _on_done(**fields):
        _diag_event(
            "tts_completed",
            turn_id=(turn_context.get("id") if turn_context else None),
            **fields,
        )

    def _on_error(**fields):
        _diag_event(
            "tts_error",
            turn_id=(turn_context.get("id") if turn_context else None),
            **fields,
        )

    try:
        _core_speak_duplex(
            segments,
            tts_provider,
            tts_overrides,
            sink,
            MUSE,
            cancel_event=_BARGE_IN_EVENT,
            turn_context=turn_context,
            speak_lock=_speak_lock,
            log=log,
            stage_log=_stage_log,
            on_error=_on_error,
            on_done=_on_done,
            # 有 EdgeTTS 备用时直接切；否则先由 VoiceCore 重试，再走同 provider 段播
            fallback_speak=(
                _fallback
                if (fallback_provider and fallback_provider != tts_provider)
                else None
            ),
        )
    finally:
        if not _BARGE_IN_EVENT.is_set():
            _drain_queue(q)


def _speak_segments(segments, tts_provider, tts_overrides, q, turn_context=None):
    try:
        with _speak_lock:
            first_segment = True
            buffered_segment = None
            end_seen = False
            segment_index = 0
            while True:
                if _BARGE_IN_EVENT.is_set():
                    log("收到真人插话，停止 TTS 播放")
                    _close_output_stream()
                    break
                segment = buffered_segment if buffered_segment is not None else segments.get()
                buffered_segment = None
                if segment is None:
                    break
                if not first_segment:
                    while len(segment) < 56:
                        try:
                            next_segment = segments.get_nowait()
                        except queue.Empty:
                            break
                        if next_segment is None:
                            end_seen = True
                            break
                        if len(segment) + len(next_segment) <= 56:
                            segment += next_segment
                        else:
                            buffered_segment = next_segment
                            break
                try:
                    segment_index += 1
                    _speak_unlocked(
                        segment,
                        tts_provider,
                        tts_overrides,
                        turn_context=turn_context,
                        segment_index=segment_index,
                    )
                except Exception as error:
                    log("TTS/播放失败:", error)
                    break
                first_segment = False
                if end_seen and buffered_segment is None:
                    break
    finally:
        if not _BARGE_IN_EVENT.is_set():
            _drain_queue(q)


def _play_pc(wav):
    """在本机默认喇叭播放 wav（阻塞），并静音聆听 + 喂回声参考。"""
    if not _feat("speaker"):
        log("本机扬声器已关闭，跳过播放")
        return
    _mute_listen(3600.0)  # 播放期间先盖住；结束再收到短 hangover
    try:
        try:
            import winsound
            winsound.PlaySound(wav, winsound.SND_FILENAME)
            return
        except Exception:
            pass
        # 优先走本机输出流（有回声参考），避免 ffplay 无法消回声
        try:
            with wave.open(wav, "rb") as reader:
                sample_rate = int(reader.getframerate() or 24000)
                channels = int(reader.getnchannels() or 1)
                sample_width = int(reader.getsampwidth() or 2)
                pcm = reader.readframes(reader.getnframes())
            if sample_width != 2:
                raise ValueError("unsupported sample width")
            if channels > 1:
                # 压成单声道再交给输出流（内部会按设备扩成立体声）
                import array
                samples = array.array("h")
                samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
                mono = array.array("h")
                for index in range(0, len(samples), channels):
                    mono.append(samples[index])
                pcm = mono.tobytes()
            output_stream = _ensure_output_stream(sample_rate)
            _ECHO_GATE.begin(sample_rate, playback_id="greet")
            frame_bytes = max(2, int(sample_rate * 0.02) * 2)  # 20ms mono
            offset = 0
            try:
                while offset < len(pcm):
                    if _BARGE_IN_EVENT.is_set():
                        log("问候播放被打断")
                        _close_output_stream()
                        break
                    chunk = pcm[offset: offset + frame_bytes]
                    offset += frame_bytes
                    if not chunk:
                        break
                    if len(chunk) % 2:
                        chunk = chunk[:-1]
                    if chunk:
                        _write_output_stream(output_stream, chunk)
                if not _BARGE_IN_EVENT.is_set():
                    time.sleep(0.05)
            finally:
                _ECHO_GATE.finish()
            return
        except Exception as error:
            log("本机流播放失败，回退 ffplay:", error)
        subprocess.run(
            [
                audio._ffmpeg().replace("ffmpeg", "ffplay"),
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                wav,
            ],
            check=False,
        )
    finally:
        _drain_mic()
        # 3600 只是播放期占位；必须清掉，否则会静音聆听一整小时
        _unmute_listen()
        _mute_listen(_LISTEN_MUTE_AFTER_PLAY)


class _PcmStreamUnsupported(Exception):
    """当前 TTS 不支持 /api/tts/stream 的裸 PCM 流式（该接口只服务 minimax）。"""


# 探到一次「不支持」就记住，后续该 provider 直接走双工/一次性。
# 否则每轮都要白跑一次注定 400 的请求（实测日志里累计 53 次）。
_PCM_STREAM_UNSUPPORTED = set()


def _play_pc_stream(
    text,
    tts_provider,
    tts_overrides,
    turn_context=None,
    segment_index=1,
):
    """从 Muse 接收裸 PCM，收到第一块就开始从本机喇叭播放。"""
    started_at = time.perf_counter()
    response = _http().post(
        MUSE + "/api/tts/stream",
        json={"provider": tts_provider, "text": text, "overrides": tts_overrides or {}},
        timeout=(5, 20),
        stream=True,
    )
    headers_at = time.perf_counter()
    if response.status_code == 400:
        detail = ""
        try:
            detail = str((response.json() or {}).get("error") or "")
        except Exception:
            detail = ""
        if "流式" in detail or "stream" in detail.lower():
            raise _PcmStreamUnsupported(detail or "provider 不支持 PCM 流式")
    response.raise_for_status()
    sample_rate = int(response.headers.get("X-Audio-Sample-Rate", "24000"))
    websocket_setup_ms = float(response.headers.get("X-TTS-WS-Setup-Ms", "0") or 0)
    websocket_reconnected = response.headers.get("X-TTS-WS-Reconnected", "0") == "1"
    _stage_log(
        turn_context,
        "TTS%d响应头" % segment_index,
        "摄像头进程→Muse=%.1fms；其中WS准备=%.1fms，重连=%s"
        % (
            (headers_at - started_at) * 1000,
            websocket_setup_ms,
            "是" if websocket_reconnected else "否",
        ),
    )
    output_stream = _ensure_output_stream(sample_rate)
    _ECHO_GATE.begin(
        sample_rate,
        playback_id=(
            turn_context.get("id")
            if turn_context
            else None
        ),
    )
    first_audio_at = None
    first_write_done_at = None
    tail = b""
    try:
        for chunk in response.iter_content(chunk_size=None):
            if _BARGE_IN_EVENT.is_set():
                log("收到真人插话，停止流式 TTS 播放")
                _close_output_stream()
                break
            if not chunk:
                continue
            if first_audio_at is None:
                first_audio_at = time.perf_counter()
            chunk = tail + chunk
            even_length = len(chunk) - len(chunk) % 2
            if even_length:
                try:
                    _write_output_stream(output_stream, chunk[:even_length])
                except Exception:
                    _close_output_stream()
                    output_stream = _ensure_output_stream(sample_rate)
                    _write_output_stream(output_stream, chunk[:even_length])
                if first_write_done_at is None:
                    first_write_done_at = time.perf_counter()
                    device_latency = getattr(output_stream, "latency", 0.0) or 0.0
                    if isinstance(device_latency, tuple):
                        device_latency = device_latency[-1]
                    log("TTS 首音频 %.3fs，开始播放「%s」" % (
                        first_audio_at - started_at,
                        text,
                    ))
                    _stage_log(
                        turn_context,
                        "首音频写入声卡" if segment_index == 1
                        else "TTS%d首音频写入声卡" % segment_index,
                        "TTS请求→响应头=%.1fms；响应头→首PCM=%.1fms；"
                        "首块写入=%.1fms；声卡缓冲≈%.1fms；"
                        "估算可听时间=+%.3fs"
                        % (
                            (headers_at - started_at) * 1000,
                            (first_audio_at - headers_at) * 1000,
                            (first_write_done_at - first_audio_at) * 1000,
                            float(device_latency) * 1000,
                            time.perf_counter() - turn_context["origin"]
                            + float(device_latency)
                            if turn_context else 0.0,
                        ),
                    )
            tail = chunk[even_length:]
    finally:
        _ECHO_GATE.finish()
        response.close()
    if first_audio_at is None:
        raise RuntimeError("流式 TTS 未返回音频")
    log("TTS 分句播放完成 %.3fs" % (time.perf_counter() - started_at))


_LAST_RESCAN_AT = 0.0
_SYSTEM_DEFAULT_OUTPUT = {"name": "", "input": "", "at": 0.0}


def _current_rescan_token() -> str:
    """直读重扫令牌，不走 1.5 秒的偏好缓存。

    这一轮的 TTS 在几百毫秒内就要开输出流；慢一拍的话「切换好了」这句话本身
    还是从旧设备出——用户看到的就是「说切好了但没生效，下一句才生效」。
    apply_rescan 也必须直读，否则它记下的是旧令牌，下一圈会重复触发。
    """
    try:
        from control_plane import database as db

        return str(db.get_setting("host.audio.rescan_token", "") or "").strip()
    except Exception:
        return str(_host_audio_prefs().get("rescan_token") or "")


_APPLIED_RESCAN_TOKEN = {"value": None}


def pending_rescan() -> bool:
    """有没有人请求重新枚举音频设备（换了输出/输入设备）。

    设备表是 PortAudio 在进程启动时建的，新插的耳机不在里面——偏好里写什么都
    落不了地。重扫必须做，但不能在麦克风流底下抽地毯：之前那版直接
    _terminate/_initialize，麦克风有时 8 秒才回来，有时再也回不来。
    这里只报告「该重扫了」，真正动手交给持有麦克风的那个循环，
    让它先停麦、再重扫、再把麦开回来。
    """
    token = _current_rescan_token()
    if _APPLIED_RESCAN_TOKEN["value"] is None:
        _APPLIED_RESCAN_TOKEN["value"] = token
        return False
    return token != _APPLIED_RESCAN_TOKEN["value"]


def apply_rescan():
    """重新枚举设备并丢掉旧的输出流。调用方必须已经停掉麦克风。"""
    global _OUTPUT_STREAM
    import sounddevice

    _APPLIED_RESCAN_TOKEN["value"] = _current_rescan_token()
    with _OUTPUT_LOCK:
        if _OUTPUT_STREAM is not None:
            try:
                _OUTPUT_STREAM.close()
            except Exception:
                pass
            _OUTPUT_STREAM = None
    for step in (sounddevice._terminate, sounddevice._initialize):
        try:
            step()
        except Exception:
            pass
    _host_audio_cache["t"] = 0.0
    _SYSTEM_DEFAULT_OUTPUT["at"] = 0.0
    try:
        names = [
            str(d.get("name") or "") for d in sounddevice.query_devices()
            if int(d.get("max_output_channels") or 0) > 0
        ]
        log("音频设备已重新枚举：%s" % "、".join(names[:6]))
    except Exception:
        log("音频设备已重新枚举")


def _system_default_audio_names():
    """系统当前的默认输入/输出设备名，用一个独立子进程去问。

    PortAudio 在进程启动时枚举一次，之后热插拔/切换默认设备都看不见。
    子进程是全新的枚举，问一次约 0.3 秒，不影响本进程正在跑的流。
    """
    import subprocess
    import sys as _sys

    try:
        out = subprocess.run(
            [_sys.executable, "-c",
             "import sounddevice as sd;d=sd.query_devices();i,o=sd.default.device;"
             "f=lambda k: d[k]['name'] if isinstance(k,int) and 0<=k<len(d) else '';"
             "print(f(i));print(f(o))"],
            capture_output=True, text=True, timeout=6,
        )
        lines = [x.strip() for x in (out.stdout or "").strip().splitlines()]
        return (lines[-2] if len(lines) >= 2 else ""), (lines[-1] if lines else "")
    except Exception:
        return "", ""


def _system_default_output_name():
    """系统当前的默认输出设备名，用一个独立子进程去问。

    PortAudio 在进程启动时枚举一次设备，之后热插拔/切换默认输出都看不见——
    实测把 Mac 输出切到 AirPods 之后，长驻的语音终端列表里根本没有 AirPods，
    只能退回内置扬声器。子进程是全新的枚举，问一次约 0.3 秒，且完全不影响
    本进程正在跑的麦克风流；只有确认「想要的设备我这儿看不到」时才真去重扫。
    """
    import subprocess
    import sys as _sys

    return _system_default_audio_names()[1]


_FRESH_INPUT_CACHE = {"at": 0.0, "names": []}


def selected_input_ready_for_rescan() -> bool:
    """明确选择的输入重新出现在系统设备表里时，请求本进程安全重扫。

    长驻 PortAudio 看不到后插入/重新连接的蓝牙设备，所以用短命子进程读取
    当下设备表。只在当前明确选择不可见时调用，并限制为两秒一次。
    """
    active = _host_audio_prefs().get("active_mic_labels") or []
    if not active:
        return False
    now = time.monotonic()
    if now - float(_FRESH_INPUT_CACHE.get("at") or 0.0) < 2.0:
        names = list(_FRESH_INPUT_CACHE.get("names") or [])
    else:
        try:
            import json
            import sys as _sys

            result = subprocess.run(
                [
                    _sys.executable,
                    "-c",
                    "import json,sounddevice as sd;"
                    "print(json.dumps([d['name'] for d in sd.query_devices() "
                    "if d['max_input_channels']>0]))",
                ],
                capture_output=True,
                text=True,
                timeout=6,
            )
            names = json.loads((result.stdout or "[]").strip().splitlines()[-1])
        except Exception:
            names = []
        _FRESH_INPUT_CACHE["at"] = now
        _FRESH_INPUT_CACHE["names"] = names
    return any(_label_in_list(name, active) for name in names)


def _pick_pc_output_device():
    """设备页选用的本机喇叭；若选用/全部候选已被禁用则返回 None。"""
    import sounddevice as sd
    prefs = _host_audio_prefs()
    configured = (prefs.get("spk_label") or "").strip() or os.environ.get(
        "VOICE_OUTPUT_DEVICE", ""
    ).strip()
    disabled = prefs.get("disabled_spk_labels") or []
    devices = list(sd.query_devices())

    def _ok(name):
        return not _label_disabled(name, disabled)

    if configured:
        if configured.lstrip("-").isdigit():
            idx = int(configured)
            try:
                info = devices[idx]
                name = str(info.get("name") or configured)
                if int(info.get("max_output_channels") or 0) > 0 and _ok(name):
                    return idx, name
            except Exception:
                pass
        else:
            needle = configured.lower()
            for i, d in enumerate(devices):
                if int(d.get("max_output_channels") or 0) <= 0:
                    continue
                name = str(d.get("name") or "")
                if needle in name.lower() or name.lower() in needle:
                    if _ok(name):
                        return i, name
                    break
        # 用户点名的输出暂时不可见或被关闭时保持选择，不擅自从别的设备出声。
        return None
    # 没有配置偏好时跟随系统默认输出。此前是「取列表里第一个可用的」，
    # 于是用户在系统里切到 AirPods，这边照旧从扬声器出声。
    default_name = str(_SYSTEM_DEFAULT_OUTPUT.get("name") or "")
    if default_name:
        for i, d in enumerate(devices):
            if int(d.get("max_output_channels") or 0) <= 0:
                continue
            name = str(d.get("name") or "")
            if name.lower() == default_name.lower() and _ok(name):
                return i, name
    for i, d in enumerate(devices):
        if int(d.get("max_output_channels") or 0) <= 0:
            continue
        name = str(d.get("name") or "")
        low = name.lower()
        if "iphone" in low or "continuity" in low:
            continue
        if _ok(name):
            return i, name
    return None


def _open_output_stream(sounddevice, device, sample_rate, channels, extra_settings):
    """打开输出流；失败时强制重扫设备缓存（热插拔后枚举失效）后重试一次。"""
    for attempt in range(2):
        try:
            stream = sounddevice.RawOutputStream(
                device=device,
                samplerate=sample_rate,
                channels=channels,
                dtype="int16",
                latency="low",
                extra_settings=extra_settings,
            )
            return stream, None
        except Exception as error:
            # 只有第一次失败才重扫：sounddevice._terminate() 会关闭包括麦克风在
            # 内的所有流，绝不能放在成功路径里无条件执行。
            if attempt != 0:
                return None, error
            try:
                sounddevice._terminate()
            except Exception:
                pass
            try:
                sounddevice._initialize()
            except Exception:
                pass
    return None, RuntimeError("unreachable")


def _ensure_output_stream(sample_rate):
    import sounddevice
    global _OUTPUT_STREAM, _OUTPUT_SAMPLE_RATE, _OUTPUT_CHANNELS, _OUTPUT_LAST_USED_AT
    with _OUTPUT_LOCK:
        now = time.monotonic()
        if (
            _OUTPUT_STREAM is not None
            and _OUTPUT_SAMPLE_RATE == sample_rate
            and not _OUTPUT_STREAM.stopped
            and now - _OUTPUT_LAST_USED_AT <= 30
        ):
            return _OUTPUT_STREAM
        if _OUTPUT_STREAM is not None:
            try:
                _OUTPUT_STREAM.close()
            except Exception:
                pass
        # 开流前先确认系统默认输出是谁：本进程的设备表是启动时枚举的，
        # 之后切到 AirPods 这类新设备根本不在表里，只能退回内置扬声器。
        now_probe = time.monotonic()
        if now_probe - float(_SYSTEM_DEFAULT_OUTPUT.get("at") or 0) > 10:
            _SYSTEM_DEFAULT_OUTPUT["name"] = _system_default_output_name()
            _SYSTEM_DEFAULT_OUTPUT["at"] = now_probe
        wanted = str(_SYSTEM_DEFAULT_OUTPUT.get("name") or "")
        if wanted:
            visible = {
                str(d.get("name") or "").lower()
                for d in sounddevice.query_devices()
                if int(d.get("max_output_channels") or 0) > 0
            }
            if wanted.lower() not in visible:
                # 这里曾经调 _rescan_audio_devices()（_terminate/_initialize）
                # 去让新设备现身。实测代价太大：它把麦克风流一起关掉，监督线程
                # 有时 8 秒才救回来，有时根本救不回来——用户表现为「说什么都不识别」。
                # 现在只记一笔，继续用看得见的设备；换输出设备在语音终端重启后生效。
                if getattr(_ensure_output_stream, "_missing", None) != wanted:
                    _ensure_output_stream._missing = wanted
                    log(
                        "系统默认输出是 %s，但本进程的设备表里没有它"
                        "（设备表在进程启动时枚举）。重启语音终端后才会切过去。" % wanted
                    )
        picked = _pick_pc_output_device()
        if picked is None:
            raise RuntimeError("本机扬声器已在设备页关闭")
        output_device, output_label = picked
        if getattr(_ensure_output_stream, "_last_label", None) != output_label:
            log("本机扬声器：%s" % output_label)
            _ensure_output_stream._last_label = output_label
        extra_settings = None
        if os.name == "nt":
            try:
                for hostapi in sounddevice.query_hostapis():
                    if "WASAPI" not in hostapi["name"].upper():
                        continue
                    wasapi_settings = getattr(sounddevice, "WasapiSettings", None)
                    if wasapi_settings is not None:
                        extra_settings = wasapi_settings(auto_convert=True)
                    break
            except Exception:
                pass
        try:
            device_info = sounddevice.query_devices(output_device)
            max_channels = int(device_info.get("max_output_channels") or 0)
        except Exception:
            device_info = {}
            max_channels = 0
        if max_channels <= 0:
            raise RuntimeError("本机扬声器不可用：%s" % output_label)
        # Mac 扬声器常见为 2 声道；按设备能力打开
        channels = 2 if max_channels >= 2 else 1
        _OUTPUT_STREAM, open_error = _open_output_stream(
            sounddevice, output_device, sample_rate, channels, extra_settings
        )
        if _OUTPUT_STREAM is None:
            # 首选设备打不开（拔插后枚举错位）：回退系统默认输出设备
            try:
                default_device = sounddevice.default.device[1]
                _OUTPUT_STREAM, _ = _open_output_stream(
                    sounddevice, default_device, sample_rate, channels, extra_settings
                )
                if _OUTPUT_STREAM is not None:
                    output_label = str(
                        sounddevice.query_devices(default_device).get("name")
                        or output_label
                    )
                    log("本机扬声器回退默认设备：%s" % output_label)
                    _ensure_output_stream._last_label = output_label
            except Exception:
                _OUTPUT_STREAM = None
        if _OUTPUT_STREAM is None:
            raise RuntimeError(
                "本机扬声器不可用：%s" % output_label
            ) from open_error
        _OUTPUT_STREAM.start()
        _OUTPUT_SAMPLE_RATE = sample_rate
        _OUTPUT_CHANNELS = channels
        _OUTPUT_LAST_USED_AT = now
        return _OUTPUT_STREAM


def _mono_to_device_pcm(audio):
    """TTS 为单声道 PCM；扬声器要立体声时做 L/R 复制。"""
    if _OUTPUT_CHANNELS <= 1 or not audio:
        return audio
    import array
    mono = array.array("h")
    mono.frombytes(audio[: len(audio) - (len(audio) % 2)])
    stereo = array.array("h")
    for sample in mono:
        stereo.append(sample)
        stereo.append(sample)
    return stereo.tobytes()


def _write_output_stream(output_stream, audio):
    global _OUTPUT_LAST_USED_AT
    _ECHO_GATE.feed_reference(audio, _OUTPUT_SAMPLE_RATE)
    output_stream.write(_mono_to_device_pcm(audio))
    _OUTPUT_LAST_USED_AT = time.monotonic()


def _close_output_stream():
    global _OUTPUT_STREAM, _OUTPUT_SAMPLE_RATE, _OUTPUT_CHANNELS, _OUTPUT_LAST_USED_AT
    with _OUTPUT_LOCK:
        if _OUTPUT_STREAM is not None:
            try:
                _OUTPUT_STREAM.stop()
            except Exception:
                pass
            try:
                _OUTPUT_STREAM.close()
            except Exception:
                pass
        _OUTPUT_STREAM = None
        _OUTPUT_SAMPLE_RATE = None
        _OUTPUT_CHANNELS = 1
        _OUTPUT_LAST_USED_AT = 0.0


def _speak_unlocked(
    text,
    tts_provider,
    tts_overrides,
    turn_context=None,
    segment_index=1,
):
    if OUTPUT != "camera" and tts_provider not in _PCM_STREAM_UNSUPPORTED:
        try:
            _play_pc_stream(
                text,
                tts_provider,
                tts_overrides,
                turn_context=turn_context,
                segment_index=segment_index,
            )
            return
        except _PcmStreamUnsupported as error:
            _PCM_STREAM_UNSUPPORTED.add(tts_provider)
            log("该 TTS 不支持 PCM 流式，后续直接走双工/一次性（%s）:" % tts_provider, error)
        except Exception as error:
            log("流式 TTS 回退:", error)
    wav = tts(text, tts_provider, tts_overrides)
    if OUTPUT == "camera":
        audio.speak(wav, CAMERA)
    else:
        _play_pc(wav)


def _speak_line(text, tts_provider, tts_overrides, q):
    segments = queue.Queue()
    segments.put(text)
    segments.put(None)
    try:
        _speak_duplex_segments(
            segments,
            tts_provider,
            tts_overrides,
            q,
        )
    except Exception as error:
        log("唤醒应答播放失败，继续进入对话窗口:", error)
        if not _BARGE_IN_EVENT.is_set():
            _drain_queue(q)


def _greeter(tts_provider, tts_overrides):
    """问候：启动后播报一次（本机麦 / 摄像头麦同策略；视觉触发已随视觉链路移除）。"""
    import shutil
    try:
        greet_wav = os.path.join(TMP, "greet.wav")
        shutil.copyfile(tts(GREET_TEXT, tts_provider, tts_overrides), greet_wav)
    except Exception as e:
        log("问候语合成失败，问候关闭:", e)
        return

    log("问候已开启（启动后播报）「%s」" % GREET_TEXT)
    time.sleep(0.6)
    if _feat("greet"):
        log("启动问候 → 播报")
        _mute_listen(2.0)  # 合成/开播前先别听
        with _speak_lock:
            _play_pc(greet_wav)
        _drain_mic()
        _unmute_listen()
        _mute_listen(max(_LISTEN_MUTE_AFTER_PLAY, 0.35))
        log("启动问候结束，短暂静音聆听防回环")
