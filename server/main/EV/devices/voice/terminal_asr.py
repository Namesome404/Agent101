# -*- coding: utf-8 -*-
"""语音终端 ASR：火山流式 / MiMo 整句 / 提前收尾封装。

依赖 state 的 SR/CAMERA/_http 与 chat 的 _agent_module。
"""
from __future__ import annotations

import base64
import io
import threading
import time
import wave

from speech.asr.doubao_stream import DoubaoStreamingASR

from devices.voice.terminal_chat import _agent_module
from devices.voice.terminal_log import log
from devices.voice.terminal_state import CAMERA, SR, _http


def _split_asr_content(text):
    """移除短尖括号占位符并切出可见词元，不使用正则。"""
    cleaned = []
    tokens = []
    token = []
    tags = []
    index = 0

    def flush_token():
        if token:
            tokens.append("".join(token).lower())
            token.clear()

    while index < len(text):
        char = text[index]
        if char == "<":
            close = text.find(">", index + 1)
            if close >= 0:
                inner = text[index + 1:close]
                if 1 <= len(inner.strip()) <= 32 and "<" not in inner:
                    flush_token()
                    tag = text[index:close + 1]
                    tags.append(tag)
                    tokens.append(tag.lower())
                    index = close + 1
                    continue
        cleaned.append(char)
        if char.isalnum() or char == "_":
            token.append(char)
        else:
            flush_token()
        index += 1
    flush_token()
    return "".join(cleaned).strip(), tags, tokens


def _sanitize_asr_text(text):
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    cleaned, tags, tokens = _split_asr_content(raw)
    visible = "".join(char for char in cleaned if char.isalnum())
    if tags and not visible:
        return "", "仅包含模型占位标签"
    if len(tokens) >= 12:
        most_common = max(tokens.count(token) for token in set(tokens))
        if most_common / len(tokens) >= 0.75:
            return "", "识别结果出现异常重复"
    return cleaned, ""


def _camera_stream_asr(provider, overrides=None):
    normalized = "".join(
        char for char in str(provider or "").lower()
        if char.isascii() and char.isalnum()
    )
    if normalized in ("doubaostreamasr", "doubaostreamasrv2"):
        if overrides is None:
            _selected, overrides = _agent_module("ASR")
        return DoubaoStreamingASR.from_env(overrides=overrides)
    return DoubaoStreamingASR("")


def asr(pcm_bytes, mimo, return_metrics=False):
    encode_started_at = time.perf_counter()
    wav = io.BytesIO()
    with wave.open(wav, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm_bytes)
    wav_bytes = wav.getvalue()
    b64 = base64.b64encode(wav_bytes).decode()
    payload = {"model": mimo["model"],
               "messages": [{"role": "user", "content": [
                   {"type": "input_audio", "input_audio": {"data": "data:audio/wav;base64," + b64}}]}],
               "asr_options": {"language": mimo["language"]}}
    request_started_at = time.perf_counter()
    response = _http().post(
        mimo["url"],
        json=payload,
        headers={"api-key": mimo["key"], "Authorization": "Bearer " + mimo["key"]},
        timeout=(5, 30),
    )
    request_finished_at = time.perf_counter()
    response.raise_for_status()
    json_started_at = time.perf_counter()
    d = response.json()
    completed_at = time.perf_counter()
    text = (d.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    if not return_metrics:
        return text
    header_seconds = response.elapsed.total_seconds()
    request_seconds = request_finished_at - request_started_at
    return text, {
        "encode_ms": round((request_started_at - encode_started_at) * 1000, 1),
        "request_to_headers_ms": round(header_seconds * 1000, 1),
        "body_download_ms": round(max(0.0, request_seconds - header_seconds) * 1000, 1),
        "json_ms": round((completed_at - json_started_at) * 1000, 1),
        "total_ms": round((completed_at - encode_started_at) * 1000, 1),
        "wav_bytes": len(wav_bytes),
        "upload_json_bytes": len(b64),
        "response_bytes": len(response.content),
    }


def _finish_asr_async(stream_asr):
    state = {
        "asr": stream_asr,
        "started_at": time.perf_counter(),
        "done": threading.Event(),
        "text": "",
        "metrics": {},
    }

    def run():
        try:
            state["text"], state["metrics"] = stream_asr.finish(timeout=7)
        finally:
            state["done"].set()

    threading.Thread(target=run, daemon=True).start()
    return state


def _warm_producer(retries=8):
    """RTSP 连接前先用 frame.jpeg 触发 go2rtc 按需 producer 连上摄像头。
    否则冷启动时 RTSP DESCRIBE 会 404（producer 未就绪），麦克风一直取不到流。"""
    from devices.camera import audio as _audio
    for i in range(retries):
        try:
            cfg = _audio._cfg(CAMERA)
            src = cfg.get("src")
            if not src:
                raise RuntimeError("未解析到摄像头流名")
            url = cfg["go2rtc_url"].rstrip("/") + "/api/frame.jpeg?src=" + src
            import urllib.request
            with urllib.request.urlopen(url, timeout=10) as resp:
                if resp.read(3) != b"\xff\xd8\xff":
                    raise RuntimeError("go2rtc 未返回有效 JPEG")
            log("摄像头流已就绪（producer 已连）")
            return True
        except Exception as e:
            log("等待摄像头流就绪…(%d/%d) %s" % (i + 1, retries, str(e)[:60]))
            time.sleep(2)
    log("摄像头流迟迟未就绪，仍尝试连接麦克风")
    return False
