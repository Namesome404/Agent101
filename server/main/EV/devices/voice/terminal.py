# -*- coding: utf-8 -*-
"""语音终端设备适配器（支持回声抑制打断）。

输入：本机麦（默认）或摄像头 RTSP 麦
  → webrtcvad 分段 → 流式 ASR → Muse 试聊(LLM + 视觉工具 + 记忆) → TTS → 本机/摄像头喇叭。
播放时用 TTS PCM 作远端参考；检测到连续真人插话后立即取消 LLM/TTS。

环境变量：
  VOICE_AGENT   智能体 id（默认 1）
  VOICE_INPUT   pc | camera | auto（默认 auto：本机麦可用则 pc）
  VOICE_CAMERA  摄像头 id/src（仅 camera 输入）
  VOICE_OUTPUT  pc | camera（默认 pc）
  MUSE_URL             Muse 地址（默认 http://127.0.0.1:8002）

职责分层（自 terminal.py 拆分而来，行为不变）：
  terminal_state      配置常量 + 共享运行时
  terminal_log        日志/诊断/阶段计时
  terminal_echo       回声参考门控 _ECHO_GATE
  terminal_chat       Muse 会话/预热/单例
  terminal_asr        ASR
  terminal_audio      麦克风/喇叭/播放
  本文件              VAD 主循环 main() + 单轮应答 _respond()
"""
import audioop
import collections
import datetime
import os
import queue
import subprocess
import threading
import time

import webrtcvad  # noqa: E402

import devices.voice.terminal_audio as _t_audio
from common.runtime import require_project_venv

require_project_venv()

from speech.voice_core import (
    duplex_ws_url,
    run_voice_turn,
    split_ready_segments as _core_split_ready_segments,
)
from devices.voice import terminal_state as _st
from devices.voice import terminal_audio
from devices.voice.terminal_audio import (
    _CameraPcmSink,
    _PcMicProc,
    _close_output_stream,
    _drain_mic,
    _drain_queue,
    _enabled_input_signature,
    _ensure_output_stream,
    _feat,
    _greeter,
    _host_audio_prefs,
    _is_self_echo_text,
    _listen_muted,
    _mute_listen,
    _pick_pc_input_device,
    _play_pc,
    _play_pc_stream,
    _speak_duplex_segments,
    _speak_line,
    _speak_segments,
    _speak_unlocked,
    _start_mic,
    _unmute_listen,
)
from devices.voice.terminal_chat import (
    _CONVERSATION_HISTORY,
    _CONVERSATION_LOCK,
    _acquire_singleton,
    _agent_module,
    _agent_tts,
    _mimo_cfg,
    _prewarm_latency,
    _prewarm_llm_turn,
    _prewarm_tts_turn,
    _publish_live_event,
    _publish_shared_message,
    _publish_status,
    _publish_voice_stage,
    _shared_conversation_history,
    _start_local_voice_heartbeat,
    _wait_muse,
    chat_stream,
)
from devices.voice.terminal_echo import _ECHO_GATE
from devices.voice.terminal_log import (
    _DIAG_AUDIO_DIR,
    _DIAG_ENABLED,
    _DIAG_PATH,
    _asr_hallucination_reason,
    _asr_quality_flags,
    _diag_event,
    _emit_turn_summary,
    _save_diag_audio,
    _stage_log,
    _stage_log_at,
    log,
)
from devices.voice.terminal_asr import (
    _camera_stream_asr,
    _finish_asr_async,
    _sanitize_asr_text,
    _warm_producer,
    asr,
)
from devices.voice.terminal_state import (
    _claim_tool_ack,
    _claim_tool_progress,
    _tool_ack_already_claimed,
)
from devices.voice.terminal_state import (
    _normalized_command,
)
from devices.voice.terminal_vad import AdaptiveVadStartGate

import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

# 常量与共享状态统一来自 terminal_state（避免各模块重复定义）
SR = _st.SR
FRAME_MS = _st.FRAME_MS
FRAME_BYTES = _st.FRAME_BYTES
PADDING = _st.PADDING
VAD_CONFIRM_FRAMES = _st.VAD_CONFIRM_FRAMES
VAD_TAIL_SECONDS = _st.VAD_TAIL_SECONDS
VAD_TRIGGER_RATIO = _st.VAD_TRIGGER_RATIO
ASR_EARLY_FINISH_MS = _st.ASR_EARLY_FINISH_MS
ASR_EARLY_FINISH_FRAMES = _st.ASR_EARLY_FINISH_FRAMES
MIN_VOICED = _st.MIN_VOICED
VAD_MODE = _st.VAD_MODE
INPUT_GAIN = _st.INPUT_GAIN
MAX_UTT_SECONDS = _st.MAX_UTT_SECONDS
MAX_UTT_FRAMES = _st.MAX_UTT_FRAMES
GREET = _st.GREET
GREET_TEXT = _st.GREET_TEXT
GREET_COOLDOWN = _st.GREET_COOLDOWN
# 看门狗：麦克风多久没喂帧就判定停摆。
# 正常情况下每 20ms 一帧，静音也照喂（只是 RMS 低），所以 15 秒没有任何一帧
# 一定是坏了，不会误伤安静环境。重开这么多次还不行就退出，交给主进程重启。
MIC_STALL_SECONDS = float(os.environ.get("VOICE_MIC_STALL_SECONDS", "15"))
MIC_STALL_GIVE_UP = int(os.environ.get("VOICE_MIC_STALL_GIVE_UP", "3"))
_LISTEN_MUTE_AFTER_PLAY = _st._LISTEN_MUTE_AFTER_PLAY
MUSE = _st.MUSE
AGENT_ID = _st.AGENT_ID
CAMERA = _st.CAMERA
OUTPUT = _st.OUTPUT
INPUT_MODE = _st.INPUT_MODE
FIRST_SEGMENT_CHARS = _st.FIRST_SEGMENT_CHARS
NEXT_SEGMENT_CHARS = _st.NEXT_SEGMENT_CHARS
TMP = _st.TMP
RTSP_LOW_LATENCY = _st.RTSP_LOW_LATENCY
_speak_lock = _st._speak_lock
_BARGE_IN_EVENT = _st._BARGE_IN_EVENT
_MIC_Q = _st._MIC_Q

_VOICE_FEATURE_FAST = {"checked_at": 0.0, "enabled": True}


def _voice_feature_enabled_fast() -> bool:
    """为面板暂停提供快速跨进程开关，避开 _feat 的 2s 设备级缓存。"""
    now = time.monotonic()
    if now - float(_VOICE_FEATURE_FAST["checked_at"] or 0.0) >= 0.15:
        try:
            from control_plane import database as db
            raw = db.get_setting("feat.voice", None)
            if raw is None:
                raw = db.get_setting("feat.camera_voice", "1")
            _VOICE_FEATURE_FAST["enabled"] = str(raw or "1") == "1"
        except Exception:
            pass
        _VOICE_FEATURE_FAST["checked_at"] = now
    return bool(_VOICE_FEATURE_FAST["enabled"])


def main():
    parent_pid = int(os.environ.get("MUSE_PARENT_PID", "0") or 0)
    if parent_pid > 1 and os.getppid() != parent_pid:
        log("主进程已不存在，语音终端不再独立运行")
        return
    _acquire_singleton()
    mimo = _mimo_cfg()
    selected_asr, selected_asr_overrides = _agent_module("ASR")
    stream_asr = _camera_stream_asr(selected_asr, selected_asr_overrides)
    mimo_ready = bool(mimo["key"] and "你的" not in str(mimo["key"]))
    if not stream_asr.enabled and not mimo_ready:
        log("火山流式 ASR 与 MiMo ASR 均未配置，退出"); return
    if not _wait_muse():
        log("Muse 未就绪，退出"); return
    tts_provider, tts_overrides = _agent_tts()
    log(
        "启动：agent=%d 输入=%s 输出=%s tts=%s"
        % (
            AGENT_ID,
            "本机麦克风" if INPUT_MODE == "pc" else ("摄像头麦:" + (CAMERA or "默认")),
            "本机喇叭" if OUTPUT != "camera" else "摄像头喇叭",
            tts_provider,
        )
    )
    if _DIAG_ENABLED:
        log("结构化诊断日志:", _DIAG_PATH)
        log("诊断录音目录:", _DIAG_AUDIO_DIR)
        _diag_event(
            "diagnostic_session_started",
            agent_id=AGENT_ID,
            input_mode=INPUT_MODE,
            camera=CAMERA or "default",
            tts_provider=tts_provider,
            output=OUTPUT,
            vad_mode=VAD_MODE,
            barge_required_ms=_ECHO_GATE.required_frames * FRAME_MS,
        )
    if stream_asr.enabled:
        if stream_asr.enable_multilingual:
            log(
                "ASR：火山流式输入多语种版（language=%s；整句定稿）"
                % (stream_asr.language or "默认中英混合")
            )
        else:
            log("ASR：火山双向流式优化版（中英混合；100ms 分包）")
        stream_asr.preconnect()  # 空闲预建 WS，隐藏首轮建连延迟
    else:
        log("ASR：%s（整句识别）" % (selected_asr or "未配置"))
    _prewarm_latency()

    vad = webrtcvad.Vad(VAD_MODE)
    vad_start_gate = AdaptiveVadStartGate()
    if INPUT_MODE != "pc":
        _warm_producer()  # 摄像头麦：先焐热 go2rtc producer
    q = queue.Queue(maxsize=400)
    asr_config_updates = queue.Queue(maxsize=1)
    global _MIC_Q
    _MIC_Q = q
    stop = threading.Event()

    def watch_asr_config():
        """后台读取管理面配置，避免 HTTP 查询卡住 20ms 麦克风消费循环。"""
        last_signature = stream_asr.config_signature()
        while not stop.wait(2.0):
            try:
                latest_provider, latest_overrides = _agent_module("ASR")
                candidate = _camera_stream_asr(
                    latest_provider,
                    latest_overrides,
                )
                signature = candidate.config_signature()
                if not latest_provider or signature == last_signature:
                    continue
                last_signature = signature
                try:
                    asr_config_updates.get_nowait()
                except queue.Empty:
                    pass
                asr_config_updates.put_nowait(
                    (latest_provider, latest_overrides, candidate)
                )
            except Exception as exc:
                log("ASR 配置监视异常:", exc)

    threading.Thread(target=watch_asr_config, daemon=True).start()
    try:
        proc = _start_mic(q, stop)
    except Exception as e:
        log("麦克风暂未打开：%s（设备页打开后会自动接入）" % e)
        proc = _PcMicProc()
        proc.bind_queue(q)
        proc._dead = True
        proc.devices_sig = ""
    _start_local_voice_heartbeat(stop)
    voice_enabled_at_start = _feat("voice")
    _publish_live_event({
        "type": "heartbeat",
        "pid": os.getpid(),
        "listening": voice_enabled_at_start,
        "standby": not voice_enabled_at_start,
    })
    log(
        "已接入%s。持续会话已开启；设备页关闭麦克风即可静音。"
        % ("本机麦克风" if INPUT_MODE == "pc" else "摄像头麦克风")
    )
    if INPUT_MODE != "pc":
        log("RTSP 音频模式：%s" % (
            "低缓冲（nobuffer/low_delay/max_delay=0）"
            if RTSP_LOW_LATENCY else "兼容缓冲"
        ))
    log("延迟口径：以服务器估算的最后一个字结束为 0；包含约 %.0fms VAD 判停。"
        % (VAD_TAIL_SECONDS * 1000))
    log(
        "低延迟：ASR提前收尾=%dms；首句切分=%d字"
        % (ASR_EARLY_FINISH_MS, FIRST_SEGMENT_CHARS)
    )
    log(
        "听觉灵敏度：输入增益=%.1fx，VAD模式=%d，触发占比=%.0f%%；"
        "起点采用自适应噪声确认，触发后不按 RMS 切掉词尾"
        % (INPUT_GAIN, VAD_MODE, VAD_TRIGGER_RATIO * 100)
    )
    if _ECHO_GATE.enabled:
        log(
            "打断：回声参考消除已开启；约%dms真人语音即可打断，且插话会送去识别"
            % (_ECHO_GATE.required_frames * FRAME_MS)
        )
    if GREET:
        threading.Thread(target=_greeter, args=(tts_provider, tts_overrides), daemon=True).start()

    ring = collections.deque(maxlen=PADDING)
    mic_stall_restarts = 0
    terminal_audio.note_mic_frame()
    triggered = False
    voiced = []
    stream_turn_active = False
    early_asr_finish = None
    early_finish_rollbacks = 0
    silence_run_frames = 0
    capture_turn_id = ""
    last_partial_text = ""
    last_partial_push_at = 0.0
    _hb_t = time.time(); _hb_max = 0; _hb_frames = 0  # 麦克风音量心跳
    _mic_zero_warns = 0
    asr_warm_checked_at = 0.0
    try:
        while True:
            if parent_pid > 1 and os.getppid() != parent_pid:
                log("主进程已退出，语音终端同步退出")
                break
            if not triggered and not stream_turn_active and early_asr_finish is None:
                try:
                    latest_asr, latest_overrides, candidate_asr = (
                        asr_config_updates.get_nowait()
                    )
                except queue.Empty:
                    candidate_asr = None
                if candidate_asr is not None:
                    stream_asr.close()
                    selected_asr = latest_asr
                    selected_asr_overrides = latest_overrides
                    stream_asr = candidate_asr
                    log(
                        "检测到 ASR 配置更新：%s；模式=%s；language=%s"
                        % (
                            selected_asr,
                            stream_asr.mode,
                            stream_asr.language or "默认",
                        )
                    )
                if stream_asr.enabled and time.time() - asr_warm_checked_at >= 2.0:
                    asr_warm_checked_at = time.time()
                    stream_asr.preconnect()
            try:
                frame = q.get(timeout=5)
                terminal_audio.note_mic_frame()
                mic_stall_restarts = 0
            except queue.Empty:
                # 看门狗：流「活着」但一帧都不来，也是坏了。
                # poll() 只看 PortAudio 的 stream.active，回调停了它照样返回正常，
                # 于是下面那套重连逻辑一次都不会触发（真实事故：播放时音频设备报
                # -10851 回退默认扬声器之后，麦克风再没喂过帧，进程活着、心跳照跳、
                # 麦克风心跳日志静默 34 分钟，只能人工重启）。
                # 只在「本该在采集」时判定：设备页开着麦、且确实选中了设备。
                if (
                    INPUT_MODE == "pc"
                    and proc.poll() is None
                    and getattr(proc, "devices_sig", "")
                    and _feat("mic")
                    and terminal_audio.mic_silent_seconds() >= MIC_STALL_SECONDS
                ):
                    mic_stall_restarts += 1
                    log(
                        "麦克风停摆 %.0fs（流仍报活着），第 %d 次重开"
                        % (terminal_audio.mic_silent_seconds(), mic_stall_restarts)
                    )
                    try:
                        proc.stop()
                    except Exception:
                        pass
                    terminal_audio.note_mic_frame()  # 重置计时，别连着刷屏
                    try:
                        proc = _start_mic(q, stop)
                        continue
                    except Exception as exc:
                        log("停摆后重开麦克风失败：%s" % exc)
                    if mic_stall_restarts >= MIC_STALL_GIVE_UP:
                        # 重开几次都救不回来，多半是音频子系统本身坏了。
                        # 退出让主进程的监管线程整体重启，比留个聋着的进程强。
                        log("麦克风连续 %d 次重开无效，退出交给监管线程重启整个语音终端"
                            % mic_stall_restarts)
                        raise SystemExit(17)
                if proc.poll() is not None:
                    if INPUT_MODE == "pc":
                        try:
                            want = _enabled_input_signature()
                        except Exception:
                            want = ""
                        if not want:
                            # 明确选择的蓝牙麦暂时不可见时保持选择；它重新连接后
                            # 用新进程发现设备，再由持有麦克风的主循环安全重扫。
                            if terminal_audio.selected_input_ready_for_rescan():
                                log("所选麦克风已重新连接，正在重新枚举…")
                                terminal_audio.apply_rescan()
                                try:
                                    proc = _start_mic(q, stop)
                                    continue
                                except Exception as exc:
                                    log("重连所选麦克风失败：%s" % exc)
                            # 用户关光了全部麦，或所选设备仍未连接：安静等待。
                            if getattr(proc, "devices_sig", None) != "":
                                proc.devices_sig = ""
                            time.sleep(0.4)
                            continue
                    log("麦克风流断开，重连…")
                    if INPUT_MODE != "pc":
                        _warm_producer(retries=3)
                    if isinstance(proc, _PcMicProc):
                        try:
                            proc.stop()
                        except Exception:
                            pass
                    try:
                        proc = _start_mic(q, stop)
                    except Exception as e:
                        log("麦克风重连失败：%s" % e)
                        if INPUT_MODE == "pc":
                            proc = _PcMicProc()
                            proc.bind_queue(q)
                            proc._dead = True
                            proc.devices_sig = ""
                        time.sleep(1.0)
                continue
            # 问候等「硬静音」窗口：丢弃麦帧，避免把自己的喇叭声当用户话
            # （正常应答播放仍靠 echo_gate 打断，不在这里一刀切）
            if _listen_muted():
                _drain_mic()
                triggered = False
                voiced = []
                ring.clear()
                continue
            # 换了音频设备：重新枚举必须由持有麦克风的这个循环来做——
            # 先停麦、再重扫、再把麦开回来。之前在输出流那边直接
            # _terminate/_initialize，等于在麦克风底下抽地毯，它有时 8 秒才回来，
            # 有时再也回不来（用户表现为「说什么都不识别」）。
            if INPUT_MODE == "pc" and terminal_audio.pending_rescan():
                log("收到音频设备切换请求，正在重新枚举…")
                if isinstance(proc, _PcMicProc):
                    try:
                        proc.stop()
                    except Exception:
                        pass
                terminal_audio.apply_rescan()
                try:
                    proc = _start_mic(q, stop)
                except Exception as exc:
                    log("重扫后麦克风重开失败：%s" % exc)
                    proc = _PcMicProc()
                    proc.bind_queue(q)
                    proc._dead = True
                    proc.devices_sig = ""
            # 设备页单选麦克风变更：先关闭旧输入流，再打开新输入流。
            if INPUT_MODE == "pc" and isinstance(proc, _PcMicProc):
                try:
                    want_sig = _enabled_input_signature()
                except Exception:
                    want_sig = proc.devices_sig
                cur_sig = getattr(proc, "devices_sig", "") or ""
                if want_sig != cur_sig:
                    now_sw = time.time()
                    last_sw = float(getattr(proc, "_last_switch_at", 0) or 0)
                    if now_sw - last_sw >= 1.5:
                        proc._last_switch_at = now_sw
                        if not want_sig:
                            if cur_sig:
                                log("设备页已关闭全部本机麦克风")
                                try:
                                    proc.stop()
                                except Exception:
                                    pass
                                proc.devices_sig = ""
                                proc.device_label = ""
                        else:
                            log(
                                "设备页更新麦克风：%s → %s"
                                % (proc.device_label or "(无)", want_sig.replace("|", " + "))
                            )
                            try:
                                proc.stop()
                            except Exception:
                                pass
                            try:
                                proc = _start_mic(q, stop)
                            except Exception as e:
                                log("麦克风重开失败：%s" % e)
                                proc = _PcMicProc()
                                proc.bind_queue(q)
                                proc._dead = True
                                proc.devices_sig = ""
                            triggered = False
                            voiced = []
                            ring.clear()
                            continue
                if not _feat("mic"):
                    _drain_mic()
                    triggered = False
                    voiced = []
                    ring.clear()
                    continue
            # 面板「暂停」是真暂停：音频帧仍被消费以保持进程稳定，
            # 但不进 VAD / ASR / LLM，也不会产生任何识别文字。
            if not _voice_feature_enabled_fast():
                if stream_turn_active:
                    stream_asr.close(discard_warm=False)
                    stream_turn_active = False
                if early_asr_finish is not None:
                    early_asr_finish["asr"].close(discard_warm=False)
                    early_asr_finish = None
                triggered = False
                voiced = []
                ring.clear()
                capture_turn_id = ""
                last_partial_text = ""
                continue
            frame_rms = audioop.rms(frame, 2)
            _hb_frames += 1; _hb_max = max(_hb_max, frame_rms)
            if time.time() - _hb_t >= 3:
                # 帧满速但 RMS=0 是安静环境的正常静音（修复后静音帧也进主循环）；
                # 只有帧数也低（<30/3s）才是麦克风真没数据，才提示权限问题。
                if (
                    _hb_max <= 0
                    and _hb_frames < 30
                    and INPUT_MODE == "pc"
                ):
                    _mic_zero_warns += 1
                    if _mic_zero_warns <= 2 or _mic_zero_warns % 20 == 0:
                        log(
                            "麦克风心跳: 3s内 %d帧, 最大RMS=0。"
                            "若一直为 0，请到「系统设置 → 隐私与安全性 → 麦克风」"
                            "允许运行 EV 的终端/Python 使用麦克风"
                            % _hb_frames
                        )
                else:
                    if _hb_max > 0:
                        _mic_zero_warns = 0
                    mixer_alive = "mixer✓"
                    if not _t_audio._MIXER_ALIVE:
                        mixer_alive = "mixer死:%s" % (_t_audio._MIXER_EXC or "?")
                    log(
                        "麦克风心跳: 3s内 %d帧, 最大RMS=%d %s | %s, 推入=%d, 回声吞帧=%d"
                        % (
                            _hb_frames,
                            _hb_max,
                            "(有声音)" if _hb_max > 240 else "(基本静音)",
                            mixer_alive,
                            _t_audio._MIXER_PUSHED,
                            _t_audio._ECHO_SWALLOWED,
                        )
                    )
                _hb_t = time.time(); _hb_max = 0; _hb_frames = 0
            vad_speech = vad.is_speech(frame, SR)
            # WebRTC VAD 在蓝牙耳麦全双工时会把窄带底噪当成 speech。
            # 只在句子起点叠加动态能量确认；触发后仍完全使用原 VAD，
            # 因此不会拿固定音量门槛切掉已经开始的轻声词尾。
            speech = (
                vad_speech
                if triggered
                else vad_start_gate.accept(frame_rms, vad_speech)
            )
            if not triggered:
                ring.append((frame, speech))
                if (
                    sum(1 for _, s in ring if s)
                    >= VAD_TRIGGER_RATIO * ring.maxlen
                ):
                    triggered = True
                    silence_run_frames = 0
                    voiced = [f for f, _ in ring]; ring.clear()
                    early_asr_finish = None
                    early_finish_rollbacks = 0
                    capture_turn_id = datetime.datetime.now().strftime(
                        "%H%M%S%f",
                    )[:9]
                    last_partial_text = ""
                    last_partial_push_at = 0.0
                    stream_turn_active = stream_asr.start(voiced)
                    _publish_status(
                        "listening",
                        "正在听…",
                        turn_id=capture_turn_id,
                    )
                    threading.Thread(
                        target=_prewarm_llm_turn,
                        daemon=True,
                    ).start()
            else:
                voiced.append(frame)
                replayed_stream = False
                if speech and early_asr_finish is not None:
                    finishing_asr = early_asr_finish["asr"]
                    finishing_asr.close(discard_warm=False)
                    early_asr_finish["done"].wait(1)
                    # 同一实例 start()，吃掉 finish() 已预建的 warm socket
                    stream_asr = finishing_asr
                    stream_turn_active = stream_asr.start(voiced)
                    early_asr_finish = None
                    early_finish_rollbacks += 1
                    replayed_stream = True
                    log("检测到句中续说，ASR 已回放整句并恢复流式识别")
                if stream_turn_active and not replayed_stream:
                    stream_asr.feed(frame)
                now_partial = time.monotonic()
                if now_partial - last_partial_push_at >= 0.08:
                    last_partial_push_at = now_partial
                    # 直接使用流式 ASR 的中间结果，不用词表或正则猜语义。
                    partial_snapshot = stream_asr.partial_snapshot()
                    partial_text, partial_error = _sanitize_asr_text(
                        partial_snapshot.get("text") or "",
                    )
                    if (
                        partial_text
                        and not partial_error
                        and partial_text != last_partial_text
                    ):
                        last_partial_text = partial_text
                        _publish_live_event({
                            "type": "utterance",
                            "role": "user",
                            "text": partial_text,
                            "turn_id": capture_turn_id,
                            "final": False,
                        })
                input_level = min(1.0, max(0.04, frame_rms / 1800.0))
                _publish_voice_stage(
                    listening=True,
                    level=input_level,
                    turn_id=capture_turn_id,
                )
                ring.append((frame, speech))
                silence_run_frames = 0 if speech else silence_run_frames + 1
                # 提前通知 ASR 收尾。句尾是否成立由服务端端点与声学 VAD 决定，
                # 不用词表或正则猜用户是不是还要继续说。
                if (
                    stream_turn_active
                    and ASR_EARLY_FINISH_FRAMES > 0
                    and not speech
                    and silence_run_frames == ASR_EARLY_FINISH_FRAMES
                ):
                    early_asr_finish = _finish_asr_async(stream_asr)
                    stream_turn_active = False
                forced_cutoff = len(voiced) >= MAX_UTT_FRAMES
                # ---- 智能「说完了没」判停 ----
                # 不再只靠本地固定静音时长一刀切：
                # 1. 服务端 definite（火山基于语义判断这句话结束）一到就判停，
                #    它比纯静音更懂「话说完没」，还能避免「句间停顿被误判」。
                # 2. 本地静音作为兜底：静音足够久且 ASR 已开始产结果才判停。
                local_silent_frames = sum(1 for _, s in ring if not s)
                server_done = (
                    stream_asr.enabled
                    and stream_asr.has_server_endpoint()
                    # 服务端 definite 需要 200ms 静音窗口才判定；本地至少
                    # 静音 VAD_CONFIRM_FRAMES-2 帧(≈180ms) 才采纳，防止
                    # definite 与本地静音几乎同帧时把最后一个词尾巴截掉。
                    and local_silent_frames >= max(1, VAD_CONFIRM_FRAMES - 2)
                )
                local_endpoint = (
                    server_done
                    or local_silent_frames >= VAD_CONFIRM_FRAMES
                )
                if forced_cutoff or local_endpoint:
                    vad_decided_at = time.perf_counter()
                    triggered = False
                    _diag_event(
                        "vad_endpoint",
                        turn_id="",
                        ring_len=len(ring),
                        ring_silent=sum(1 for _, s in ring if not s),
                        ring_voiced=sum(1 for _, s in ring if s),
                        silence_run_frames=silence_run_frames,
                        local_silent_frames=local_silent_frames,
                        server_done=server_done,
                        forced_cutoff=forced_cutoff,
                        voiced_len=len(voiced),
                        ring_speech_ratio=round(
                            (sum(1 for _, s in ring if s) / max(1, len(ring))), 3
                        ),
                        vad_conf=round(
                            sum(1 for _, s in ring if s) / max(1, len(ring)), 3
                        ),
                    )
                    # 判停来源诊断：server=服务端语义 definite /
                    # local=本地声学 VAD / forced=时长上限。
                    endpoint_source = (
                        "forced"
                        if forced_cutoff
                        else (
                            "server_definite"
                            if server_done
                            else "local_vad"
                        )
                    )
                    utt = b"".join(voiced); voiced = []; ring.clear()
                    stream_text = ""
                    stream_metrics = {}
                    trailing_silence = (
                        0.0
                        if forced_cutoff
                        else silence_run_frames * FRAME_MS / 1000.0
                    )
                    silence_run_frames = 0
                    if len(utt) >= MIN_VOICED * FRAME_BYTES:
                        rms = audioop.rms(utt, 2)
                        utt_seconds = len(utt) / 2.0 / SR
                        turn_id = capture_turn_id or datetime.datetime.now().strftime(
                            "%H%M%S%f",
                        )[:9]
                        turn_context = {
                            "id": turn_id,
                            "origin": vad_decided_at - trailing_silence,
                            "first_tts_logged": False,
                            "stages": {},
                        }
                        audio_path = _save_diag_audio(turn_id, utt)
                        _diag_event(
                            "utterance_captured",
                            turn_id=turn_id,
                            duration_ms=round(
                                len(utt) / 2.0 / SR * 1000,
                                1,
                            ),
                            rms=rms,
                            forced_cutoff=forced_cutoff,
                            trailing_silence_ms=round(
                                trailing_silence * 1000,
                                1,
                            ),
                            audio_path=audio_path,
                            endpoint_source=endpoint_source,
                        )
                        if forced_cutoff:
                            log("语音达到 %.1fs 上限，提前送识别" % MAX_UTT_SECONDS)
                        log("检测到语音 %.1fs 音量RMS=%d" % (len(utt) / 2.0 / SR, rms))
                        _stage_log(
                            turn_context,
                            "VAD判停",
                            "静音确认=%.0fms；来源=%s；服务端收到音频段=%.1fs"
                            % (
                                trailing_silence * 1000,
                                "时长上限" if forced_cutoff else "本地VAD",
                                len(utt) / 2.0 / SR,
                            ),
                        )
                        # ASR 并行链路（本地 VAD 判定结束即送识别）
                        asr_started_at = time.perf_counter()
                        asr_provider = (
                            "doubao_stream"
                            if stream_asr.enabled
                            else "none"
                        )
                        if early_asr_finish is not None:
                            early_asr_finish["done"].wait(7)
                            stream_text = early_asr_finish["text"]
                            stream_metrics = early_asr_finish["metrics"]
                            stream_metrics["early_finish_lead_ms"] = round(
                                max(
                                    0.0,
                                    vad_decided_at - early_asr_finish["started_at"],
                                ) * 1000,
                                1,
                            )
                            stream_metrics["early_finish_rollbacks"] = (
                                early_finish_rollbacks
                            )
                            early_asr_finish = None
                        elif stream_turn_active:
                            stream_text, stream_metrics = stream_asr.finish(timeout=7)
                        stream_turn_active = False
                        text, invalid_asr_reason = _sanitize_asr_text(stream_text)
                        asr_metrics = stream_metrics
                        if invalid_asr_reason:
                            asr_metrics["invalid_text"] = invalid_asr_reason
                            log(
                                "火山流式 ASR 返回无效文本，已丢弃:",
                                invalid_asr_reason,
                            )
                        if not text and stream_asr.enabled:
                            stream_error = stream_metrics.get("error") or ""
                            if stream_error:
                                log("火山流式 ASR 失败:", stream_error)
                            else:
                                log("火山流式 ASR 未识别到有效语音")
                        should_fallback_mimo = (
                            not text
                            and mimo_ready
                            and not stream_asr.enabled
                        )
                        if should_fallback_mimo:
                            log("使用 MiMo ASR")
                            asr_provider = "mimo"
                            try:
                                text, asr_metrics = asr(
                                    utt,
                                    mimo,
                                    return_metrics=True,
                                )
                                text, invalid_asr_reason = _sanitize_asr_text(text)
                                if invalid_asr_reason:
                                    asr_metrics["invalid_text"] = invalid_asr_reason
                                    log(
                                        "MiMo ASR 返回无效文本，已丢弃:",
                                        invalid_asr_reason,
                                    )
                            except Exception as e:
                                log("ASR 失败:", e); text = ""; asr_metrics = {}
                        asr_total_ms = round(
                            (time.perf_counter() - asr_started_at) * 1000,
                            1,
                        )
                        log("ASR 完成 %.3fs" % (asr_total_ms / 1000.0))
                        quality_flags = _asr_quality_flags(
                            text,
                            asr_provider,
                            rms,
                            forced_cutoff,
                        )
                        _diag_event(
                            "asr_result",
                            turn_id=turn_id,
                            provider=asr_provider,
                            text=text,
                            quality_flags=quality_flags,
                            total_ms=asr_total_ms,
                            audio_path=audio_path,
                            metrics=asr_metrics,
                        )
                        if asr_metrics:
                            if asr_metrics.get("provider") == "doubao_stream":
                                _stage_log(
                                    turn_context,
                                    "ASR完成",
                                    "火山流式；%s；建连=%.1fms；初始化=%.1fms；"
                                    "流启动→首个中间结果=%.1fms；"
                                    "流启动→服务端definite=%.1fms；"
                                    "definite领先提交=%.1fms；提交→最终=%.1fms；"
                                    "提前收尾领先VAD=%.1fms；续说回放=%d；"
                                    "已流传音频=%.0fms；资源=%s"
                                    % (
                                        "预连接复用" if asr_metrics.get("warm_reused") else "冷建连",
                                        float(asr_metrics.get("connect_ms", 0.0)),
                                        float(asr_metrics.get("initialize_ms", 0.0)),
                                        float(asr_metrics.get("first_partial_ms", 0.0)),
                                        float(asr_metrics.get("server_endpoint_ms", 0.0)),
                                        float(asr_metrics.get("server_endpoint_lead_ms", 0.0)),
                                        float(asr_metrics.get("final_after_vad_ms", 0.0)),
                                        asr_metrics.get("early_finish_lead_ms", 0.0),
                                        int(asr_metrics.get("early_finish_rollbacks", 0)),
                                        float(asr_metrics.get("audio_ms", 0.0)),
                                        str(asr_metrics.get("resource_id", "")),
                                    ),
                                )
                            else:
                                _stage_log(
                                    turn_context,
                                    "ASR完成",
                                    "编码=%.1fms；公网请求→响应头=%.1fms"
                                    "（含网络+排队+推理）；响应体下载=%.1fms；JSON=%.1fms；"
                                    "上传WAV=%dB/base64=%dB，响应=%dB"
                                    % (
                                        float(asr_metrics.get("encode_ms", 0.0)),
                                        float(asr_metrics.get("request_to_headers_ms", 0.0)),
                                        float(asr_metrics.get("body_download_ms", 0.0)),
                                        float(asr_metrics.get("json_ms", 0.0)),
                                        int(asr_metrics.get("wav_bytes", 0)),
                                        int(asr_metrics.get("upload_json_bytes", 0)),
                                        int(asr_metrics.get("response_bytes", 0)),
                                    ),
                                )
                        log("听到:", repr(text))
                        if not text:
                            _diag_event(
                                "dialog_suppressed",
                                turn_id=turn_id,
                                reason="asr_empty",
                                provider=asr_provider,
                                quality_flags=quality_flags,
                                audio_path=audio_path,
                            )
                        elif len(text) < 2:
                            _diag_event(
                                "dialog_suppressed",
                                turn_id=turn_id,
                                reason="asr_text_too_short",
                                text=text,
                                provider=asr_provider,
                                quality_flags=quality_flags,
                            )
                        elif _asr_hallucination_reason(text, rms, utt_seconds):
                            hallucination = _asr_hallucination_reason(
                                text, rms, utt_seconds,
                            )
                            log("忽略 ASR 幻觉(%s):" % hallucination, repr(text))
                            _diag_event(
                                "dialog_suppressed",
                                turn_id=turn_id,
                                reason="asr_hallucination",
                                detail=hallucination,
                                text=text,
                                rms=rms,
                                duration_ms=round(utt_seconds * 1000, 1),
                                provider=asr_provider,
                                quality_flags=quality_flags,
                                audio_path=audio_path,
                            )
                            text = ""
                        elif _is_self_echo_text(text):
                            log("忽略自回声问候:", repr(text))
                            _diag_event(
                                "dialog_suppressed",
                                turn_id=turn_id,
                                reason="self_echo_greet",
                                text=text,
                                provider=asr_provider,
                                quality_flags=quality_flags,
                            )
                            text = ""
                        if not text or len(text) < 2:
                            if last_partial_text:
                                _publish_live_event({
                                    "type": "utterance",
                                    "role": "user",
                                    "text": last_partial_text,
                                    "turn_id": turn_id,
                                    "final": True,
                                })
                            _publish_status(
                                "done",
                                "没有识别到可处理的内容",
                                turn_id=turn_id,
                            )
                            _publish_voice_stage(
                                listening=True,
                                speaking=False,
                                level=0.0,
                                turn_id=turn_id,
                            )
                        if text and len(text) >= 2:
                            if stream_asr.enabled:
                                # 下一轮识别动态继承当前对话里的领域词。例如上一轮
                                # 已听清 GitHub，下一轮的同音词就不必从零猜。
                                stream_asr.remember_text(text)
                            latest_tts_provider, latest_tts_overrides = _agent_tts()
                            if latest_tts_provider and (
                                latest_tts_provider != tts_provider
                                or latest_tts_overrides != tts_overrides
                            ):
                                log(
                                    "检测到 TTS 配置切换:",
                                    tts_provider,
                                    "→",
                                    latest_tts_provider,
                                )
                                tts_provider = latest_tts_provider
                                tts_overrides = latest_tts_overrides
                            cmd = text
                            addressed_hint = "always_listening"
                            voice_enabled = _feat("voice") and _feat("mic")
                            if cmd and voice_enabled:
                                log("识别:", cmd)
                                _diag_event(
                                    "dialog_started",
                                    turn_id=turn_id,
                                    command=cmd,
                                    addressed_hint=addressed_hint,
                                    asr_provider=asr_provider,
                                    quality_flags=quality_flags,
                                )
                                _publish_status(
                                    "heard",
                                    cmd,
                                    turn_id=turn_id,
                                )
                                response_status = _respond(
                                    cmd,
                                    tts_provider,
                                    tts_overrides,
                                    q,
                                    turn_context=turn_context,
                                    addressed_hint=addressed_hint,
                                )
                            elif cmd and not voice_enabled:
                                _publish_live_event({
                                    "type": "utterance",
                                    "role": "user",
                                    "text": cmd,
                                    "turn_id": turn_id,
                                    "final": True,
                                })
                                _publish_status(
                                    "done",
                                    "Agent 已暂停，这句没有处理",
                                    turn_id=turn_id,
                                )
                                _diag_event(
                                    "dialog_suppressed",
                                    turn_id=turn_id,
                                    reason="voice_feature_disabled",
                                    command=cmd,
                                )
                    elif stream_turn_active:
                        stream_asr.finish(timeout=2)
                        stream_turn_active = False
                    elif early_asr_finish is not None:
                        early_asr_finish["done"].wait(2)
                        early_asr_finish = None
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if early_asr_finish is not None:
            early_asr_finish["asr"].close()
        stream_asr.close()
        try: proc.terminate()
        except Exception: pass
        _close_output_stream()


def _respond(
    command,
    tts_provider,
    tts_overrides,
    q,
    turn_context=None,
    addressed_hint="conversation_window",
):
    started_at = time.perf_counter()
    _BARGE_IN_EVENT.clear()
    if turn_context is None:
        turn_context = {
            "id": datetime.datetime.now().strftime("%H%M%S"),
            "origin": started_at,
            "first_tts_logged": False,
            "stages": {},
        }
    _diag_event(
        "response_started",
        turn_id=turn_context["id"],
        command=command,
        addressed_hint=addressed_hint,
        tts_provider=tts_provider,
    )
    use_duplex_tts = OUTPUT != "camera"
    if not use_duplex_tts:
        threading.Thread(
            target=_prewarm_tts_turn,
            args=(tts_provider, tts_overrides, turn_context),
            daemon=True,
        ).start()
    if use_duplex_tts:
        # Start the continuous provider only with actual answer text. A short
        # local acknowledgement followed by a several-second model/tool wait
        # made Huoshan stream near-silent PCM for tens of seconds.
        turn_context["suppress_latency_filler"] = True
    else:
        turn_context["suppress_latency_filler"] = False
    if _tool_ack_already_claimed(command):
        turn_context["tool_ack_spoken"] = True
    llm_metrics = {}
    history = _shared_conversation_history()
    turn_id = str((turn_context or {}).get("id") or "")
    # 用户话立刻上屏（不必等整轮结束）
    _publish_shared_message("user", command, turn_id=turn_id, final=True)
    _publish_voice_stage(listening=False, speaking=False, level=0.15, turn_id=turn_id)
    _publish_status("thinking", command, turn_id=turn_id)

    def _delta_stream_factory():
        raw_stream = chat_stream(
            command,
            history=history,
            cancel_event=_BARGE_IN_EVENT,
            addressed_hint=addressed_hint,
            metrics=llm_metrics,
        )
        buf = []
        last_push = 0
        for item in raw_stream:
            if isinstance(item, dict) and item.get("kind") == "tool_wait":
                turn_context["suppress_latency_filler"] = True
                _publish_status(
                    "tool",
                    "调用工具: %s" % (item.get("tool") or ""),
                    turn_id=turn_id,
                )
                yield item
                continue
            if isinstance(item, dict) and item.get("kind") == "tool_ack":
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                # 已播过则丢弃，杜绝第二遍「稍等我搜一下」
                if turn_context.get("tool_ack_spoken") or not _claim_tool_ack(
                    command, text
                ):
                    turn_context["tool_ack_spoken"] = True
                    continue
                turn_context["tool_ack_spoken"] = True
                turn_context["suppress_latency_filler"] = True
                _publish_status(
                    "tool_done",
                    "工具回执: %s" % text,
                    turn_id=turn_id,
                )
                yield item
                continue
            if isinstance(item, dict) and item.get("kind") == "tool_progress":
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                if not _claim_tool_progress(command, text):
                    continue
                turn_context["suppress_latency_filler"] = True
                yield item
                continue
            if isinstance(item, dict):
                yield item
                continue
            delta = item
            buf.append(delta)
            text = "".join(buf)
            if len(text) - last_push >= 24 or text.endswith(("。", "！", "？", "\n")):
                _publish_live_event({
                    "type": "utterance",
                    "role": "assistant",
                    "text": text,
                    "turn_id": turn_id,
                    "final": False,
                })
                last_push = len(text)
            yield delta

    if use_duplex_tts:
        sink = _CameraPcmSink()
        fallback_provider = os.environ.get(
            "CAMERA_TTS_FALLBACK_PROVIDER", "EdgeTTS",
        ).strip()

        def _fallback(remaining_segments, completed_segments):
            provider = tts_provider
            overrides = tts_overrides
            if fallback_provider and fallback_provider != tts_provider:
                provider = fallback_provider
                overrides = {}
                log("双工 TTS 失败，立即切换备用 TTS：%s" % fallback_provider)
            for index, segment in enumerate(
                remaining_segments, start=completed_segments + 1,
            ):
                _speak_unlocked(
                    segment, provider, overrides,
                    turn_context=turn_context, segment_index=index,
                )

        def _stage_log_status(turn_context, stage, detail=""):
            _stage_log(turn_context, stage, detail)
            if stage in ("LLM首字", "首音频写入声卡", "TTS双工就绪"):
                try:
                    _publish_status(
                        {
                            "LLM首字": "回复准备中",
                            "首音频写入声卡": "回复中",
                            "TTS双工就绪": "语音合成中",
                        }[stage],
                        detail or "",
                        turn_id=turn_context.get("id", ""),
                    )
                except Exception:
                    pass

        outcome, reply = run_voice_turn(
            command,
            None,
            tts_provider,
            tts_overrides,
            sink,
            MUSE,
            cancel_event=_BARGE_IN_EVENT,
            turn_context=turn_context,
            speak_lock=_speak_lock,
            log=log,
            stage_log=_stage_log_status,
            stage_log_at=_stage_log_at,
            on_tts_done=lambda **f: _diag_event(
                "tts_completed",
                turn_id=turn_context.get("id"),
                **f,
            ),
            on_tts_error=lambda **f: _diag_event(
                "tts_error",
                turn_id=turn_context.get("id"),
                **f,
            ),
            fallback_speak=(
                _fallback
                if (fallback_provider and fallback_provider != tts_provider)
                else None
            ),
            llm_metrics=llm_metrics,
            started_at=started_at,
            delta_stream_factory=_delta_stream_factory,
            on_tool_progress=(
                None
                if OUTPUT == "camera"
                else lambda text: _speak_unlocked(
                    text,
                    tts_provider,
                    tts_overrides,
                    turn_context=turn_context,
                )
            ),
        )
        if not _BARGE_IN_EVENT.is_set():
            _drain_queue(q)
    else:
        # 摄像头喇叭路径：非 duplex，保留原分段播放
        segments = queue.Queue()
        worker = threading.Thread(
            target=_speak_segments,
            args=(segments, tts_provider, tts_overrides, q, turn_context),
            daemon=True,
        )
        worker.start()
        reply_parts = []
        pending = ""
        first_segment = True
        try:
            for item in _delta_stream_factory():
                if _BARGE_IN_EVENT.is_set():
                    break
                if isinstance(item, dict) and item.get("kind") == "round_done":
                    reply_parts = []
                    pending = ""
                    first_segment = True
                    continue
                if isinstance(item, dict) and item.get("kind") == "tool_wait":
                    continue
                if isinstance(item, dict) and item.get("kind") in (
                    "tool_ack",
                    "tool_progress",
                ):
                    text = str(item.get("text") or "").strip()
                    if text:
                        segments.put(text)
                    continue
                if isinstance(item, dict):
                    continue
                delta = item
                reply_parts.append(delta)
                pending += delta
                ready, pending = _split_ready_segments(pending, first_segment)
                for segment in ready:
                    segments.put(segment)
                if ready:
                    first_segment = False
        except Exception as e:
            log("流式试聊失败:", e)
            segments.put(None)
            worker.join()
            _emit_turn_summary(
                turn_context, "llm_error",
                command=command, llm_metrics=llm_metrics,
            )
            return "llm_error"
        if pending.strip() and not _BARGE_IN_EVENT.is_set():
            segments.put(pending.strip())
        segments.put(None)
        worker.join()
        reply = "".join(reply_parts).strip()
        outcome = (
            "interrupted" if _BARGE_IN_EVENT.is_set()
            else "completed" if reply
            else "llm_empty_reply"
        )

    if outcome == "llm_error":
        _diag_event(
            "dialog_suppressed",
            turn_id=turn_context["id"],
            reason="llm_stream_error",
            command=command,
        )
        _emit_turn_summary(
            turn_context, "llm_error",
            command=command, llm_metrics=llm_metrics,
        )
        # 静默失败 = 用户等十几秒没任何反应，体验就是「卡住不回复」。
        # 失败也要给一个可听信号：明确告知网络/模型暂时不可用，并让用户再试。
        if not _BARGE_IN_EVENT.is_set():
            _publish_status(
                "error",
                "网络或模型暂时不可用，回复失败",
                turn_id=turn_context.get("id", ""),
            )
            try:
                with _speak_lock:
                    _speak_unlocked(
                        "网络有点慢，刚才这句我没能处理。再说一次好吗？",
                        tts_provider,
                        tts_overrides,
                        turn_context=turn_context,
                        segment_index=1,
                    )
            except Exception as error:
                log("LLM 失败提示播报异常:", error)
        enabled_after_error = _voice_feature_enabled_fast()
        _publish_voice_stage(
            speaking=False,
            listening=enabled_after_error,
            standby=not enabled_after_error,
            level=0.0,
            turn_id=turn_context.get("id", ""),
        )
        return "llm_error"

    if reply:
        with _CONVERSATION_LOCK:
            _CONVERSATION_HISTORY.append({"role": "user", "content": command})
            _CONVERSATION_HISTORY.append({"role": "assistant", "content": reply})
        # user 已在开场推过；这里补 assistant 终稿
        _publish_shared_message("assistant", reply, turn_id=turn_id, final=True)
    enabled_after_turn = _voice_feature_enabled_fast()
    _publish_voice_stage(
        speaking=False,
        listening=enabled_after_turn,
        standby=not enabled_after_turn,
        level=0.0,
        turn_id=turn_id,
    )

    upstream = llm_metrics.get("upstream") or {}
    if upstream and use_duplex_tts:
        _stage_log(
            turn_context,
            "LLM完成",
            "Muse→模型流建立=%.1fms（含公网连接+排队）；"
            "上游首数据=%.1fms；首文本=%.1fms；上游总计=%.1fms；"
            "实时工具=%s/%.1fms；重试=%d；结束原因=%s"
            % (
                upstream.get("upstream_stream_ready_ms", 0.0),
                upstream.get("upstream_first_chunk_ms", 0.0),
                upstream.get("upstream_first_text_ms", 0.0),
                upstream.get("upstream_total_ms", 0.0),
                upstream.get("tool_name", "无"),
                upstream.get("tool_ms", 0.0),
                int(upstream.get("retry_count", 0) or 0),
                upstream.get("finish_reason", ""),
            ),
        )
    if outcome in ("llm_empty_reply", "not_addressed") or (
        not reply and outcome == "completed"
    ):
        not_addressed = outcome == "not_addressed" or (
            llm_metrics.get("addressed") is False
            or upstream.get("addressed") is False
            or upstream.get("finish_reason") == "not_addressed"
        )
        if not reply:
            _diag_event(
                "dialog_suppressed",
                turn_id=turn_context["id"],
                reason=(
                    "not_addressed_to_muse"
                    if not_addressed else "llm_empty_reply"
                ),
                command=command,
                addressed_hint=addressed_hint,
            )
            outcome = "not_addressed" if not_addressed else "llm_empty_reply"

    _publish_status(
        "done",
        "回复完成: %s" % (reply[:60] if reply else "无回复"),
        turn_id=turn_id,
    )
    _emit_turn_summary(
        turn_context, outcome,
        command=command, reply=reply, llm_metrics=llm_metrics,
    )
    return outcome


def _split_ready_segments(text, first_segment=True):
    return _core_split_ready_segments(
        text,
        first_segment,
        first_chars=FIRST_SEGMENT_CHARS,
        next_chars=NEXT_SEGMENT_CHARS,
    )


def _muse_tts_websocket_url():
    return duplex_ws_url(MUSE)


if __name__ == "__main__":
    main()
