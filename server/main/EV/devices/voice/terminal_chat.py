# -*- coding: utf-8 -*-
"""语音终端外部能力：Muse 会话 / 实时事件 / 智能体模块 / 预热 / 单例锁。

与 Muse(:8002) 的 HTTP 交互全在这里；依赖 state 的 MUSE/AGENT_ID/_http
与共享锁，依赖 log 的 log/_diag_event/_stage_log。
"""
from __future__ import annotations

import collections
import os
import threading
import time

import requests

from common.paths import SERVER_DIR
from speech.voice_core.dialog import MuseDialogClient

from devices.voice.terminal_log import _diag_event, _stage_log, log
from devices.voice.terminal_state import (
    AGENT_ID,
    MUSE,
    TMP,
    _http,
)

_CONVERSATION_HISTORY = collections.deque(maxlen=16)
_CONVERSATION_LOCK = threading.Lock()
_LLM_PREWARM_LOCK = threading.Lock()
_LLM_PREWARM_AT = 0.0


def _mimo_cfg():
    from ruamel.yaml import YAML
    path = str(SERVER_DIR / "config.yaml")
    with open(path, encoding="utf-8") as f:
        cfg = YAML(typ="safe").load(f)
    blk = ((cfg.get("ASR") or {}).get("MiMoASR")) or {}
    return {"url": blk.get("base_url", "https://api.xiaomimimo.com/v1/chat/completions"),
            "key": blk.get("api_key", ""), "model": blk.get("model_name", "mimo-v2.5-asr"),
            "language": blk.get("language", "auto")}


def _agent_module(module_type):
    """返回智能体模块的 (provider, overrides)。HTTP 失败时回退本地 DB。"""
    try:
        response = _http().get(MUSE + "/api/agents/%d" % AGENT_ID, timeout=10)
        response.raise_for_status()
        d = response.json()
        # /api/agents/{id} 可能包一层 agent
        modules = d.get("modules") or (d.get("agent") or {}).get("modules") or {}
        node = modules.get(module_type) or {}
        selected = node.get("selected")
        if selected:
            return selected, (node.get("overrides") or {})
    except Exception:
        pass
    try:
        from control_plane import database as db
        agent = db.get_agent(AGENT_ID) or {}
        node = (agent.get("modules") or {}).get(module_type) or {}
        return node.get("selected"), (node.get("overrides") or {})
    except Exception:
        return None, {}


def _agent_tts():
    return _agent_module("TTS")


def chat(text):
    # 语音场景要短：一句话口语回答，砍掉长篇大论(长回复=长TTS+长播放，延迟感全在这)
    text = text + "\n\n（这是语音对话，请用口语、一句话简短回答，最多两句，别啰嗦。）"
    response = _http().post(
        MUSE + "/api/agents/%d/chat" % AGENT_ID,
        json={"message": text},
        timeout=(5, 120),
    )
    response.raise_for_status()
    return (response.json().get("reply") or "").strip()


def chat_stream(
    text,
    metrics=None,
    history=None,
    cancel_event=None,
    addressed_hint="conversation_window",
    speaker_name=None,
    speaker_score=None,
    speaker_status=None,
    known_speakers=None,
):
    """按增量文本读取 Muse（VoiceCore MuseDialogClient）。"""
    client = MuseDialogClient(MUSE, AGENT_ID, http_session=_http())
    yield from client.chat_stream(
        text,
        metrics=metrics,
        history=history,
        cancel_event=cancel_event,
        addressed_hint=addressed_hint,
        speaker_name=speaker_name,
        speaker_score=speaker_score,
        speaker_status=speaker_status,
        known_speakers=known_speakers,
    )


def _shared_conversation_history():
    """取最近对话上下文：服务端最近消息 + 本机尚未同步的尾部。"""
    server = []
    try:
        response = _http().get(
            MUSE + "/api/agents/%d/conversation" % AGENT_ID,
            params={"limit": 40},
            timeout=(2, 3),
        )
        response.raise_for_status()
        server = [
            {"role": item["role"], "content": item["content"]}
            for item in response.json().get("messages", [])
            if item.get("role") in ("user", "assistant") and item.get("content")
        ][-16:]
    except Exception:
        server = []
    with _CONVERSATION_LOCK:
        local = list(_CONVERSATION_HISTORY)
    if not local:
        return server
    if not server:
        return local

    def _key(item):
        return (item.get("role"), str(item.get("content") or "").strip())

    seen = {_key(item) for item in server}
    merged = list(server)
    for item in local:
        key = _key(item)
        if key in seen:
            continue
        merged.append({"role": item["role"], "content": item["content"]})
        seen.add(key)
    return merged[-16:]


def _publish_shared_message(role, content, *, turn_id="", final=True):
    try:
        _http().post(
            MUSE + "/api/agents/%d/conversation" % AGENT_ID,
            json={"role": role, "content": content, "source": "camera"},
            timeout=(2, 3),
        ).raise_for_status()
    except Exception as error:
        log("共享会话写入失败:", error)
    _publish_live_event({
        "type": "utterance",
        "role": role,
        "text": content,
        "turn_id": str(turn_id or ""),
        "final": bool(final),
    })


def _publish_live_event(payload):
    try:
        _http().post(
            MUSE + "/api/agents/%d/live" % AGENT_ID,
            json=payload or {},
            timeout=(1.2, 2),
        )
    except Exception:
        pass


def _publish_status(status, detail="", *, turn_id=""):
    """推送一轮的阶段性状态，供实时状态窗口时间线渲染。"""
    try:
        _http().post(
            MUSE + "/api/agents/%d/live" % AGENT_ID,
            json={
                "type": "status",
                "status": status,
                "detail": detail,
                "turn_id": turn_id,
            },
            timeout=(1.2, 2),
        )
    except Exception:
        pass


_STAGE_PUSH = {"t": 0.0, "speaking": None, "level": 0.0}


def _publish_voice_stage(
    *, speaking=None, level=None, listening=None, standby=None, turn_id=None,
):
    now = time.time()
    # 电平最多 ~20Hz；speaking 边沿立刻推
    edge = speaking is not None and speaking != _STAGE_PUSH["speaking"]
    if level is not None and not edge and now - _STAGE_PUSH["t"] < 0.05:
        return
    body = {"type": "stage"}
    if speaking is not None:
        body["speaking"] = bool(speaking)
        _STAGE_PUSH["speaking"] = bool(speaking)
    if listening is not None:
        body["listening"] = bool(listening)
    if standby is not None:
        body["standby"] = bool(standby)
    if level is not None:
        body["level"] = float(level)
        _STAGE_PUSH["level"] = float(level)
    if turn_id is not None:
        body["turn_id"] = str(turn_id)
    _STAGE_PUSH["t"] = now
    _publish_live_event(body)


_LOCK_FD = None


def _acquire_singleton():
    """防止本机语音双开：第二进程立刻退出，避免同一句话两个 TTS。"""
    global _LOCK_FD
    path = os.path.join(TMP, "voice_terminal.lock")
    try:
        os.makedirs(TMP, exist_ok=True)
        fd = open(path, "a+", encoding="utf-8")
    except Exception as error:
        log("单实例锁文件无法创建，继续启动:", error)
        return None
    try:
        import fcntl
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("已有本机语音进程在运行（单实例），本次退出")
        try:
            fd.close()
        except Exception:
            pass
        raise SystemExit(0)
    except Exception as error:
        log("单实例锁不可用，继续启动:", error)
    try:
        fd.seek(0)
        fd.truncate()
        fd.write("%d\n" % os.getpid())
        fd.flush()
    except Exception:
        pass
    _LOCK_FD = fd
    return fd


def _start_local_voice_heartbeat(stop_event):
    """每 2s 上报心跳，终端据此显示本机语音链路是否在线。"""

    def _voice_enabled():
        try:
            from control_plane import database as db
            raw = db.get_setting("feat.voice", None)
            if raw is None:
                raw = db.get_setting("feat.camera_voice", "1")
            return str(raw or "1") == "1"
        except Exception:
            return True

    def _loop():
        while not stop_event.is_set():
            enabled = _voice_enabled()
            # 心跳要说的是「我还在听」，不只是「我这个进程还在」。
            # 这两件事分开过一次代价：麦克风停摆 34 分钟，心跳线程照跳 listening=true，
            # 控制面和监管线程都看不出异常。把距离上一帧的秒数一起报上去。
            try:
                from devices.voice import terminal_audio
                silent = round(terminal_audio.mic_silent_seconds(), 1)
            except Exception:
                silent = None
            _publish_live_event({
                "type": "heartbeat",
                "pid": os.getpid(),
                "listening": enabled,
                "standby": not enabled,
                "mic_silent_s": silent,
            })
            stop_event.wait(2.0)

    threading.Thread(target=_loop, name="local-voice-hb", daemon=True).start()


def _wait_muse(timeout=40):
    """等 Muse 就绪再启动，避免拿到 tts=None（启动顺序无关紧要）。"""
    t = time.time()
    while time.time() - t < timeout:
        try:
            response = _http().get(MUSE + "/api/status", timeout=2)
            response.raise_for_status()
            return True
        except Exception:
            time.sleep(2)
    return False


def _prewarm_latency():
    global _LLM_PREWARM_AT
    started_at = time.perf_counter()
    try:
        response = _http().post(
            MUSE + "/api/latency/prewarm",
            json={"agent_id": AGENT_ID},
            timeout=(5, 40),
        )
        response.raise_for_status()
        result = response.json().get("result") or {}
        if result.get("llm") == "ready":
            with _LLM_PREWARM_LOCK:
                _LLM_PREWARM_AT = time.monotonic()
        log("连接预热完成 %.3fs：%s" % (
            time.perf_counter() - started_at,
            result,
        ))
    except Exception as error:
        log("连接预热失败，将按需连接:", error)


def _prewarm_llm_turn():
    global _LLM_PREWARM_AT
    with _LLM_PREWARM_LOCK:
        now = time.monotonic()
        if now - _LLM_PREWARM_AT < 5:
            return
        _LLM_PREWARM_AT = now
    started_at = time.perf_counter()
    try:
        response = requests.post(
            MUSE + "/api/llm/prewarm",
            json={"agent_id": AGENT_ID},
            timeout=(3, 12),
        )
        response.raise_for_status()
        result = response.json().get("result") or {}
        log(
            "LLM连接预热 %.3fs（上游 %.1fms）"
            % (
                time.perf_counter() - started_at,
                float(result.get("elapsed_ms") or 0),
            )
        )
    except Exception as error:
        log("LLM连接预热失败:", error)


def _prewarm_tts_turn(tts_provider, tts_overrides, turn_context):
    started_at = time.perf_counter()
    try:
        response = _http().post(
            MUSE + "/api/tts/prewarm",
            json={
                "provider": tts_provider,
                "overrides": tts_overrides or {},
            },
            timeout=(5, 20),
        )
        response.raise_for_status()
        result = response.json().get("result") or {}
        _stage_log(
            turn_context,
            "TTS后台预热",
            "摄像头进程→Muse=%.1fms；WS准备=%.1fms；重连=%s"
            % (
                (time.perf_counter() - started_at) * 1000,
                float(result.get("setup_ms") or 0),
                "是" if result.get("reconnected") else "否",
            ),
        )
    except Exception as error:
        log("TTS 后台预热失败，将由首句请求重连:", error)
