# -*- coding: utf-8 -*-
"""Camera 设备音频 I/O：RTSP 麦克风采集与 WebRTC 扬声器回传。

摄像头解析(go2rtc 地址 + 流名)直接读 data/camera.json 与设备绑定，
不再依赖视觉模块（视觉链路已移除）。
- capture_mic(seconds, out_wav)  从摄像头麦录一段 16k mono wav
- speak(wav_path)                把 wav 经 go2rtc backchannel 推到摄像头喇叭(双向对讲)
"""
import asyncio
import json
import os
import shutil
import subprocess
import urllib.parse
import urllib.request

from common.paths import DATA_DIR


def _ffmpeg():
    for p in (shutil.which("ffmpeg"), r"C:/ProgramData/chocolatey/bin/ffmpeg.exe"):
        if p and os.path.exists(p):
            return p
    return "ffmpeg"


def _cfg(camera=None):
    """解析 go2rtc 地址 + 摄像头流名：data/camera.json 全局 + 设备绑定合并。"""
    cfg = {"go2rtc_url": "http://localhost:1984", "src": ""}
    cfg_path = DATA_DIR / "camera.json"
    if cfg_path.exists():
        try:
            user = json.loads(cfg_path.read_text(encoding="utf-8"))
            for k in ("go2rtc_url", "src"):
                if user.get(k):
                    cfg[k] = user[k]
        except Exception:
            pass
    dev = None
    try:
        from control_plane import database as db
        if camera is not None:
            dev = db.get_camera(camera)
            if not dev:
                raise RuntimeError("找不到摄像头设备: %s" % camera)
        if not dev:
            for cam in db.list_cameras() or []:
                dev = cam
                break
    except RuntimeError:
        raise
    except Exception:
        dev = None
    if dev:
        if dev.get("src"):
            cfg["src"] = dev["src"]
        if dev.get("go2rtc_url"):
            cfg["go2rtc_url"] = dev["go2rtc_url"]
    if not cfg.get("src"):
        raise RuntimeError("未解析到摄像头流名(先登记摄像头设备)")
    return cfg


def _host(go2rtc_url):
    return urllib.parse.urlparse(go2rtc_url).hostname or "127.0.0.1"


def rtsp_url(camera=None):
    cfg = _cfg(camera)
    return "rtsp://%s:8554/%s" % (_host(cfg["go2rtc_url"]), urllib.parse.quote(cfg["src"]))


def capture_mic(seconds, out_wav, camera=None, sr=16000):
    """从摄像头麦克风录 seconds 秒到 out_wav(16k mono)。返回 out_wav。"""
    url = rtsp_url(camera)
    r = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
         "-i", url, "-vn", "-t", str(seconds), "-ar", str(sr), "-ac", "1", "-y", out_wav],
        capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_wav):
        raise RuntimeError("摄像头麦录音失败: %s" % (r.stderr or "")[:200])
    return out_wav


async def _push(wav_path, webrtc_url, seconds):
    from aiortc import RTCPeerConnection, RTCConfiguration, RTCSessionDescription
    from aiortc.contrib.media import MediaPlayer
    # 禁用默认 STUN：本机连 go2rtc 只需 host 候选，避免公网 STUN 往返卡 ~5s
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
    player = MediaPlayer(wav_path)
    pc.addTrack(player.audio)
    done = asyncio.Event()

    @pc.on("connectionstatechange")
    async def _():
        if pc.connectionState in ("failed", "closed"):
            done.set()

    await pc.setLocalDescription(await pc.createOffer())
    body = json.dumps({"type": "offer", "sdp": pc.localDescription.sdp}).encode()
    req = urllib.request.Request(webrtc_url, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    raw = urllib.request.urlopen(req, timeout=12).read().decode()
    try:
        ans = json.loads(raw)
        answer = ans.get("sdp") or ans.get("value") or raw
    except Exception:
        answer = raw
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
    try:
        await asyncio.wait_for(done.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    await pc.close()


def _wav_seconds(path):
    import wave
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate() or 16000)


def _to_48k(src):
    """转成 48kHz 单声道 s16 wav —— 与验证过能清晰播放的测试音完全同格式。
    aiortc 对非 48k 音频重采样会出问题(变'机器音')，立体声交织又会被误读成噪音，故用 48k 单声道。"""
    out = os.path.join(os.path.dirname(os.path.abspath(src)), "_spk48k.wav")
    subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", src,
                    "-ar", "48000", "-ac", "1", "-sample_fmt", "s16", "-y", out],
                   check=True, capture_output=True)
    return out


def speak(wav_path, camera=None, seconds=None):
    """把 wav 推到摄像头喇叭（先转 48k 立体声）。seconds 缺省按音频时长 + 0.8s 余量。"""
    cfg = _cfg(camera)
    webrtc_url = cfg["go2rtc_url"].rstrip("/") + "/api/webrtc?src=" + urllib.parse.quote(cfg["src"])
    wav48 = _to_48k(wav_path)
    if seconds is None:
        try:
            seconds = _wav_seconds(wav48) + 0.8
        except Exception:
            seconds = 6.0
    asyncio.run(_push(wav48, webrtc_url, seconds))
    return True
