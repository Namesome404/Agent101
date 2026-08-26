# -*- coding: utf-8 -*-
"""TTS 统一逻辑：音色清单 / 可用性 / 试听 / 预热 / 双工 PCM / 流式 / 克隆。

从 app.py 拆出的 APIRouter。TTS 共享件（MiniMax WS 会话、扬声器扇出、文本归一化、
provider 解析）在 app_shared，本模块只保留纯本组路由与随组辅助函数。
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Body, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse

from common.paths import MUSE_DIR, SERVER_DIR
from app_shared import (
    EDGE_VOICES,
    MINIMAX_VOICES,
    _MINIMAX_TTS_STREAM_WS,
    _MINIMAX_TTS_WS,
    _SPEAKERS,
    _normalize_tts_text,
    _resolved_tts,
    _tcp_open,
)
from control_plane import database as db

router = APIRouter()


# 本组私有：thread-local HTTP 会话（避免跨线程共享 requests.Session）
_TTS_HTTP_LOCAL = threading.local()


def _tts_http_session():
    import requests
    session = getattr(_TTS_HTTP_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
        session.mount("https://", adapter)
        _TTS_HTTP_LOCAL.session = session
    return session


def _parse_host_port(url, default_port):
    try:
        u = urlparse(url if "//" in url else "http://" + url)
        return (u.hostname or "127.0.0.1", u.port or default_port)
    except Exception:
        return ("127.0.0.1", default_port)


def _find_ffmpeg():
    for p in [shutil.which("ffmpeg"), r"C:/ProgramData/chocolatey/bin/ffmpeg.exe",
              "D:/AI/GPT-SoVITS-v3lora-20250228/ffmpeg.exe"]:
        if p and os.path.exists(p):
            return p
    return None


@router.get("/api/tts/voices")
def tts_voices(provider: str):
    blk = db.provider_catalog().get("TTS", {}).get(provider, {}) or {}
    ttype = blk.get("type", provider)
    if ttype in ("gpt_sovits_v2", "gpt_sovits_v3"):
        return {"mode": "refaudio",
                "ref_audio_path": blk.get("ref_audio_path", ""),
                "prompt_text": blk.get("prompt_text", "")}
    if ttype == "edge":
        return {"mode": "list", "voiceKey": "voice",
                "current": blk.get("voice", "zh-CN-XiaoxiaoNeural"),
                "voices": [{"value": v, "label": l} for v, l in EDGE_VOICES]}
    if ttype == "minimax_httpstream":
        return {"mode": "list", "voiceKey": "voice_id", "clone": True,
                "current": blk.get("voice_id", "female-shaonv"),
                "voices": [{"value": v, "label": l} for v, l in MINIMAX_VOICES]}
    from speech.tts.voice_catalog import catalog_for_tts
    voice_catalog = catalog_for_tts(ttype, blk)
    if voice_catalog:
        return {
            "mode": "list",
            "voiceKey": voice_catalog["voiceKey"],
            "current": voice_catalog["current"],
            "voices": [
                {"value": value, "label": label}
                for value, label in voice_catalog["voices"]
            ],
        }
    vkey = "speaker" if ("speaker" in blk and "voice" not in blk) else "voice"
    return {"mode": "field", "voiceKey": vkey, "current": blk.get(vkey, "")}


@router.post("/api/tts/check")
def tts_check(payload: dict = Body(...)):
    blk = _resolved_tts(payload.get("provider"), payload.get("overrides", {}))
    ttype = blk.get("type", payload.get("provider"))
    if ttype in ("gpt_sovits_v2", "gpt_sovits_v3", "fishspeech", "index_stream"):
        host, port = _parse_host_port(blk.get("url", ""), 9880)
        ok = _tcp_open(host, port)
        return {"state": "ok" if ok else "down",
                "detail": ("本地服务在线 %s:%d" % (host, port)) if ok else "本地服务未启动 %s:%d" % (host, port)}
    if ttype == "edge":
        return {"state": "ok", "detail": "微软在线服务"}
    ph = [k for k, v in blk.items() if isinstance(v, str) and ("你的" in v or "请替换" in v)]
    if ph:
        return {"state": "unconfigured", "detail": "未填写: " + ", ".join(ph)}
    return {"state": "ok", "detail": "已配置（试听可实测）"}


@router.post("/api/tts/preview")
def tts_preview(payload: dict = Body(...)):
    provider = payload.get("provider")
    text = (payload.get("text") or "你好，我是 EV，这是当前音色的试听。").strip()
    blk = _resolved_tts(provider, payload.get("overrides", {}))
    tmp = SERVER_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / "muse_preview.wav"
    if out.exists():
        try:
            out.unlink()
        except Exception:
            pass
    reqfile = tmp / "muse_preview_req.json"
    reqfile.write_text(json.dumps({"block": blk, "text": text, "out": str(out)},
                                  ensure_ascii=False), encoding="utf-8")
    env = os.environ.copy()
    venv_scripts = SERVER_DIR / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    env["PATH"] = str(venv_scripts) + os.pathsep + env.get("PATH", "")
    py = str(venv_scripts / ("python.exe" if os.name == "nt" else "python"))
    if not Path(py).exists():
        py = sys.executable
    helper = str(MUSE_DIR / "speech" / "tts" / "preview.py")
    try:
        r = subprocess.run([py, helper, str(reqfile)], cwd=str(SERVER_DIR), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=150)
    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error": "合成超时"}, status_code=500)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        return JSONResponse({"ok": False, "error": ((r.stdout or "") + (r.stderr or "")).strip()[-400:] or "失败"},
                            status_code=500)
    return FileResponse(str(out), media_type="audio/wav", filename="preview.wav")


@router.post("/api/tts/prewarm")
def tts_prewarm(payload: dict = Body(...)):
    provider = payload.get("provider")
    blk = _resolved_tts(provider, payload.get("overrides", {}))
    if blk.get("type", provider) != "minimax_httpstream":
        try:
            from speech.tts import duplex as generic_tts
            if generic_tts.is_streaming_type(blk.get("type", provider)):
                result = generic_tts.prewarm_generic(provider, blk)
                return {"ok": True, "result": result}
        except Exception as error:
            return JSONResponse(
                {"ok": False, "error": "流式 TTS 预热失败: %s" % error},
                status_code=502,
            )
        return {"ok": True, "result": {"skipped": True}}
    try:
        result = _MINIMAX_TTS_WS.prewarm(blk)
    except Exception as error:
        return JSONResponse(
            {"ok": False, "error": "MiniMax TTS 预热失败: %s" % error},
            status_code=502,
        )
    return {"ok": True, "result": result}


@router.websocket("/api/tts/duplex")
async def tts_duplex(websocket: WebSocket):
    """持续接收增量文本，并在同一 MiniMax 会话中持续回传裸 PCM。"""
    await websocket.accept()
    input_queue = queue.Queue()
    output_queue = queue.Queue()
    cancel_event = threading.Event()
    receiver_task = None
    try:
        start_payload = await websocket.receive_json()
        provider = start_payload.get("provider")
        blk = _resolved_tts(provider, start_payload.get("overrides", {}))
        _ttype = blk.get("type", provider)
        if _ttype != "minimax_httpstream":
            from speech.tts import duplex as _dup
            if _dup.is_streaming_type(_ttype):
                async def _recv_json():
                    try:
                        return await websocket.receive_json()
                    except Exception:
                        return None
                try:
                    await _dup.run_generic_duplex(
                        websocket, provider, blk,
                        websocket.send_json, websocket.send_bytes, _recv_json)
                except Exception as _e:
                    try:
                        await websocket.send_json({"event": "error", "error": str(_e)})
                    except Exception:
                        pass
                return
            await websocket.send_json({
                "event": "error",
                "error": "当前 TTS 不支持双工 PCM 流式播放",
            })
            return
        api_key = blk.get("api_key")
        if not api_key or "你的" in str(api_key) or "请替换" in str(api_key):
            await websocket.send_json({
                "event": "error",
                "error": "MiniMax 凭证未配置",
            })
            return

        def run_provider():
            try:
                for pcm_chunk in _MINIMAX_TTS_WS.duplex(
                    blk,
                    input_queue,
                    on_ready=lambda value: output_queue.put(("ready", value)),
                    on_segment_done=lambda value: output_queue.put(
                        ("segment_done", value)
                    ),
                    cancel_event=cancel_event,
                ):
                    output_queue.put(("audio", pcm_chunk))
                output_queue.put(("done", None))
            except Exception as error:
                output_queue.put(("error", str(error)))

        provider_thread = threading.Thread(target=run_provider, daemon=True)
        provider_thread.start()

        async def receive_text():
            try:
                while True:
                    message = await websocket.receive_json()
                    event = message.get("event")
                    if event == "text":
                        text = (message.get("text") or "").strip()
                        if text:
                            input_queue.put(text)
                    elif event == "finish":
                        input_queue.put(None)
                        return
            except WebSocketDisconnect:
                cancel_event.set()
                input_queue.put(None)
                output_queue.put(("disconnect", None))
            except Exception as error:
                cancel_event.set()
                input_queue.put(None)
                output_queue.put(("error", "双工 TTS 输入失败: %s" % error))

        receiver_task = asyncio.create_task(receive_text())
        while True:
            kind, value = await asyncio.to_thread(output_queue.get)
            if kind == "ready":
                await websocket.send_json({"event": "ready", **value})
                _SPEAKERS.start(int(value.get("sample_rate") or 24000))
            elif kind == "audio":
                await websocket.send_bytes(value)
                _SPEAKERS.pcm(value)
            elif kind == "segment_done":
                await websocket.send_json({
                    "event": "segment_done",
                    "index": value,
                })
            elif kind == "error":
                _SPEAKERS.end()
                await websocket.send_json({"event": "error", "error": value})
                return
            elif kind in ("done", "disconnect"):
                _SPEAKERS.end()
                if kind == "done":
                    await websocket.send_json({"event": "done"})
                return
    except WebSocketDisconnect:
        pass
    finally:
        cancel_event.set()
        input_queue.put(None)
        if receiver_task is not None and not receiver_task.done():
            receiver_task.cancel()
            try:
                await receiver_task
            except BaseException:
                pass


@router.post("/api/tts/stream")
def tts_stream(payload: dict = Body(...)):
    """将 MiniMax SSE 音频即时转换为可直接播放的 PCM 字节流。"""
    provider = payload.get("provider")
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "空文本"}, status_code=400)
    blk = _resolved_tts(provider, payload.get("overrides", {}))
    if blk.get("type", provider) != "minimax_httpstream":
        return JSONResponse(
            {"ok": False, "error": "当前 TTS 不支持 PCM 流式播放"},
            status_code=400,
        )
    text = _normalize_tts_text(text, blk.get("model"))
    if not text:
        return JSONResponse({"ok": False, "error": "无可朗读文本"}, status_code=400)
    group_id = blk.get("group_id")
    api_key = blk.get("api_key")
    if not group_id or not api_key or "你的" in str(api_key) or "请替换" in str(api_key):
        return JSONResponse({"ok": False, "error": "MiniMax 凭证未配置"}, status_code=400)

    voice_setting = {
        "voice_id": "female-shaonv",
        "speed": 1,
        "vol": 1,
        "pitch": 0,
    }
    voice_setting.update(blk.get("voice_setting") or {})
    voice_id = blk.get("private_voice") or blk.get("voice_id")
    if voice_id:
        voice_setting["voice_id"] = voice_id
    audio_setting = {
        "sample_rate": 24000,
        "bitrate": 128000,
        "format": "pcm",
        "channel": 1,
    }
    audio_setting.update(blk.get("audio_setting") or {})
    audio_setting["format"] = "pcm"
    audio_setting["channel"] = 1
    sample_rate = int(audio_setting.get("sample_rate") or 24000)
    request_payload = {
        "model": blk.get("model") or "speech-01-turbo",
        "text": text,
        "stream": True,
        "voice_setting": voice_setting,
        "pronunciation_dict": blk.get("pronunciation_dict") or {
            "tone": ["处理/(chu3)(li3)", "危险/dangerous"],
        },
        "audio_setting": audio_setting,
    }
    api_url = "https://api.minimaxi.com/v1/t2a_v2?GroupId=%s" % group_id
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % api_key,
    }
    try:
        websocket_setup = _MINIMAX_TTS_STREAM_WS.prewarm(blk)
    except Exception as websocket_error:
        websocket_setup = {
            "reconnected": False,
            "setup_ms": 0.0,
            "error": str(websocket_error),
        }

    def generate_http_pcm():
        with _tts_http_session().post(
            api_url,
            headers=headers,
            data=json.dumps(request_payload),
            timeout=(5, 30),
            stream=True,
        ) as upstream:
            upstream.raise_for_status()
            buffer = b""
            for chunk in upstream.iter_content(chunk_size=None):
                if not chunk:
                    continue
                buffer += chunk
                while True:
                    header_pos = buffer.find(b"data: ")
                    if header_pos < 0:
                        break
                    end_pos = buffer.find(b"\n\n", header_pos)
                    if end_pos < 0:
                        break
                    event = buffer[header_pos + 6:end_pos]
                    buffer = buffer[end_pos + 2:]
                    try:
                        data = json.loads(event.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    base_response = data.get("base_resp") or {}
                    if base_response.get("status_code", 0) != 0:
                        raise RuntimeError(base_response.get("status_msg") or "MiniMax TTS 失败")
                    stream_data = data.get("data") or {}
                    audio_hex = stream_data.get("audio")
                    if stream_data.get("status", 1) == 1 and audio_hex:
                        yield bytes.fromhex(audio_hex)

    def generate_pcm():
        emitted = False
        if websocket_setup.get("error"):
            yield from generate_http_pcm()
            return
        try:
            for pcm_chunk in _MINIMAX_TTS_STREAM_WS.stream(blk, text):
                emitted = True
                yield pcm_chunk
            if emitted:
                return
        except Exception as websocket_error:
            if emitted:
                raise
            print(
                "[muse] MiniMax 短句 WebSocket 回退 HTTP:",
                websocket_error,
                flush=True,
            )
        yield from generate_http_pcm()

    def generate_pcm_broadcast():
        # 同一段 PCM 既回给发起方(voice terminal 本机喇叭)，也扇出给网络扬声器(ESP32)。
        _SPEAKERS.start(sample_rate)
        try:
            for pcm_chunk in generate_pcm():
                _SPEAKERS.pcm(pcm_chunk)
                yield pcm_chunk
        finally:
            _SPEAKERS.end()

    return StreamingResponse(
        generate_pcm_broadcast(),
        media_type="audio/L16",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Audio-Sample-Rate": str(sample_rate),
            "X-Audio-Channels": "1",
            "X-Audio-Sample-Width": "2",
            "X-TTS-WS-Reconnected": (
                "1" if websocket_setup.get("reconnected") else "0"
            ),
            "X-TTS-WS-Setup-Ms": str(websocket_setup.get("setup_ms") or 0),
        },
    )


@router.post("/api/tts/minimax/clone")
def minimax_clone(payload: dict = Body(...)):
    import requests
    audio_path = (payload.get("audio_path") or "").strip().strip('"')
    voice_id = (payload.get("voice_id") or "").strip()
    ov = payload.get("overrides", {}) or {}
    if not audio_path or not os.path.exists(audio_path):
        return JSONResponse({"ok": False, "error": "音频不存在"}, status_code=400)
    if not re.match(r"^[A-Za-z][A-Za-z0-9]{7,}$", voice_id):
        return JSONResponse({"ok": False, "error": "音色名需字母开头、≥8位字母数字"}, status_code=400)
    blk = _resolved_tts("MinimaxTTSHTTPStream", ov)
    gid, key = blk.get("group_id"), blk.get("api_key")
    if not gid or not key or "你的" in str(key):
        return JSONResponse({"ok": False, "error": "MiniMax 凭证未配置"}, status_code=400)
    up_path = audio_path
    ff = _find_ffmpeg()
    if ff:
        tmp = SERVER_DIR / "tmp" / "clone_src.mp3"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([ff, "-y", "-i", audio_path, "-ac", "1", "-ar", "32000", "-b:a", "128k", str(tmp)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
            if tmp.exists() and tmp.stat().st_size > 0:
                up_path = str(tmp)
        except Exception:
            pass
    try:
        with open(up_path, "rb") as f:
            uj = requests.post("https://api.minimaxi.com/v1/files/upload?GroupId=%s" % gid,
                               headers={"Authorization": "Bearer %s" % key},
                               data={"purpose": "voice_clone"}, files={"file": f}, timeout=120).json()
        file_id = (uj.get("file") or {}).get("file_id")
        if not file_id:
            return JSONResponse({"ok": False, "error": "上传失败: %s" % uj}, status_code=500)
        cj = requests.post("https://api.minimaxi.com/v1/voice_clone?GroupId=%s" % gid,
                           headers={"Authorization": "Bearer %s" % key, "Content-Type": "application/json"},
                           json={"file_id": file_id, "voice_id": voice_id, "need_noise_reduction": True},
                           timeout=120).json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": "复刻异常: %s" % e}, status_code=500)
    if (cj.get("base_resp") or {}).get("status_code") != 0:
        return JSONResponse({"ok": False, "error": "复刻失败: %s" % cj.get("base_resp")}, status_code=500)
    return {"ok": True, "voice_id": voice_id}
