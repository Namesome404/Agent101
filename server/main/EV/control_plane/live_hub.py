# -*- coding: utf-8 -*-
"""本机语音 → 浏览器终端旁路：实时对话事件 + 声波电平 + 浮窗（面板不被 stage 冲掉）。"""
from __future__ import annotations

import collections
import os
import threading
import time

_LOCK = threading.Lock()
_STATE = {}  # agent_id -> state
_STREAM_ID = "%d-%d" % (os.getpid(), int(time.time() * 1000))


def _agent(agent_id: int) -> dict:
    aid = int(agent_id or 0)
    st = _STATE.get(aid)
    if st is None:
        st = {
            "seq": 0,
            "events": collections.deque(maxlen=200),
            "speaking": False,
            "level": 0.0,
            "listening": False,
            "standby": False,
            "updated_at": 0.0,
            "turn_id": "",
            # 最近一条用户转写是粘性快照。事件仍用于增量动画，但窗口重载、
            # 渲染线程短暂停顿或游标越过事件时，可以从这里无损恢复当前文字。
            "latest_user_utterance": {},
            # 浮窗粘性状态：按 window_id 保留，snapshot 每次带回，避免被 stage 冲掉
            "panels": {},
            "local_voice": {
                "running": False,
                "pid": 0,
                "listening": False,
                "seen_at": 0.0,
            },
        }
        _STATE[aid] = st
    st.setdefault("panels", {})
    return st


_LOCAL_VOICE_STALE_SEC = 4.5


def heartbeat(agent_id: int, *, pid: int = 0, listening: bool = None, standby: bool = None):
    """本机语音进程心跳：终端据此显示「本机语音在线」。"""
    with _LOCK:
        st = _agent(agent_id)
        lv = st.setdefault("local_voice", {})
        lv["running"] = True
        lv["pid"] = int(pid or 0)
        lv["seen_at"] = time.time()
        if listening is not None:
            lv["listening"] = bool(listening)
            st["listening"] = bool(listening)
        if standby is not None:
            lv["standby"] = bool(standby)
            st["standby"] = bool(standby)
        st["updated_at"] = lv["seen_at"]


def local_voice_status(agent_id: int) -> dict:
    with _LOCK:
        st = _agent(agent_id)
        lv = dict(st.get("local_voice") or {})
    seen = float(lv.get("seen_at") or 0)
    age = (time.time() - seen) if seen else 9999.0
    alive = bool(lv.get("running")) and age <= _LOCAL_VOICE_STALE_SEC
    return {
        "running": alive,
        "pid": int(lv.get("pid") or 0) if alive else 0,
        "listening": bool(lv.get("listening")) if alive else False,
        "standby": bool(lv.get("standby")) if alive else False,
        "age_ms": int(age * 1000) if seen else None,
        "stale_after_ms": int(_LOCAL_VOICE_STALE_SEC * 1000),
    }


def mark_voice_stopped(agent_id: int):
    """立即将本机语音运行时标记为已停止。

    不等心跳的 4.5s 过期窗口，让状态面板的「停止」回执与真实子进程保持一致。
    """
    with _LOCK:
        st = _agent(agent_id)
        lv = st.setdefault("local_voice", {})
        lv.update({
            "running": False,
            "pid": 0,
            "listening": False,
            "standby": False,
            "seen_at": time.time(),
        })
        st["speaking"] = False
        st["listening"] = False
        st["standby"] = False
        st["level"] = 0.0
        st["updated_at"] = lv["seen_at"]
        st["seq"] += 1
        st["events"].append({
            "seq": st["seq"],
            "type": "stage",
            "speaking": False,
            "listening": False,
            "standby": False,
            "level": 0.0,
            "turn_id": st.get("turn_id") or "",
            "t": st["updated_at"],
        })


def push_utterance(agent_id: int, role: str, text: str, *, turn_id: str = "", final: bool = True):
    text = (text or "").strip()
    if not text or role not in ("user", "assistant"):
        return
    with _LOCK:
        st = _agent(agent_id)
        st["seq"] += 1
        st["updated_at"] = time.time()
        if turn_id:
            st["turn_id"] = turn_id
        event = {
            "seq": st["seq"],
            "type": "utterance",
            "role": role,
            "text": text,
            "turn_id": turn_id or st.get("turn_id") or "",
            "final": bool(final),
            "t": st["updated_at"],
        }
        st["events"].append(event)
        if role == "user":
            st["latest_user_utterance"] = dict(event)


def push_status(agent_id: int, status: str, detail: str = "", *, turn_id: str = ""):
    """推送一轮语音回合的阶段状态（识别到/思考中/调用工具/回复中…）。

    供实时状态窗口渲染时间线；与 stage 电平事件互不干扰。
    """
    with _LOCK:
        st = _agent(agent_id)
        st["seq"] += 1
        st["updated_at"] = time.time()
        if turn_id:
            st["turn_id"] = turn_id
        st["events"].append({
            "seq": st["seq"],
            "type": "status",
            "status": str(status or ""),
            "detail": str(detail or ""),
            "turn_id": turn_id or st.get("turn_id") or "",
            "t": st["updated_at"],
        })


def push_panel(agent_id: int, panel: dict, *, turn_id: str = ""):
    """向 Muse 终端推送浮窗。写入粘性 panels + 事件，保证客户端一定能看到。"""
    if not isinstance(panel, dict) or not panel:
        return
    with _LOCK:
        st = _agent(agent_id)
        st["seq"] += 1
        st["updated_at"] = time.time()
        payload = dict(panel)
        wid = str(
            payload.get("window_id")
            or payload.get("windowId")
            or payload.get("panel")
            or "custom"
        )
        payload["window_id"] = wid
        payload["_rev"] = st["seq"]
        st["panels"][wid] = payload
        st["events"].append({
            "seq": st["seq"],
            "type": "panel",
            "panel": payload,
            "turn_id": turn_id or st.get("turn_id") or "",
            "t": st["updated_at"],
        })


def clear_panel(agent_id: int, window_id: str = ""):
    with _LOCK:
        st = _agent(agent_id)
        if window_id:
            st["panels"].pop(str(window_id), None)
        else:
            st["panels"].clear()


def clear_panels(agent_id: int):
    with _LOCK:
        st = _agent(agent_id)
        st["panels"].clear()


def reset(agent_id: int):
    """清空事件流与面板（不重置 speaking/listening/standby 实时态）。"""
    with _LOCK:
        st = _agent(agent_id)
        st["events"].clear()
        st["panels"].clear()
        st["turn_id"] = ""
        st["latest_user_utterance"] = {}


def set_stage(
    agent_id: int,
    *,
    speaking: bool = None,
    level: float = None,
    listening: bool = None,
    turn_id: str = None,
    standby: bool = None,
):
    """更新声波/说话状态。电平只写当前值，不再塞进 events（避免冲掉 panel）。"""
    with _LOCK:
        st = _agent(agent_id)
        edge = False
        if speaking is not None and st["speaking"] != bool(speaking):
            st["speaking"] = bool(speaking)
            edge = True
        if listening is not None and st["listening"] != bool(listening):
            st["listening"] = bool(listening)
            edge = True
        if standby is not None and st["standby"] != bool(standby):
            st["standby"] = bool(standby)
            if st["standby"]:
                st["listening"] = False
            edge = True
        if level is not None:
            lv = max(0.0, min(1.0, float(level)))
            st["level"] = st["level"] * 0.55 + lv * 0.45
        if turn_id is not None:
            st["turn_id"] = str(turn_id or "")
        if not st["speaking"] and level is None:
            st["level"] *= 0.82
        st["updated_at"] = time.time()
        # 仅在说话/聆听边沿记一条，供需要事件流的客户端；电平靠 snapshot 根字段
        if edge:
            st["seq"] += 1
            st["events"].append({
                "seq": st["seq"],
                "type": "stage",
                "speaking": st["speaking"],
                "listening": st["listening"],
                "standby": st["standby"],
                "level": round(st["level"], 4),
                "turn_id": st.get("turn_id") or "",
                "t": st["updated_at"],
            })


def snapshot(agent_id: int, after_seq: int = 0) -> dict:
    with _LOCK:
        st = _agent(agent_id)
        after = int(after_seq or 0)
        events = [e for e in st["events"] if e["seq"] > after]
        # 截断时优先保留 panel / utterance，别只剩 stage
        if len(events) > 40:
            important = [e for e in events if e.get("type") in ("panel", "utterance")]
            rest = [e for e in events if e.get("type") not in ("panel", "utterance")]
            budget = max(0, 40 - len(important))
            events = important[-40:] + rest[-budget:]
            events.sort(key=lambda e: int(e.get("seq") or 0))
        lv = dict(st.get("local_voice") or {})
        seen = float(lv.get("seen_at") or 0)
        age = (time.time() - seen) if seen else 9999.0
        local_alive = bool(lv.get("running")) and age <= _LOCAL_VOICE_STALE_SEC
        panels = [dict(p) for p in (st.get("panels") or {}).values()]
        return {
            "ok": True,
            "agent_id": int(agent_id),
            "stream_id": _STREAM_ID,
            "seq": st["seq"],
            "speaking": st["speaking"],
            "listening": st["listening"] if local_alive else False,
            "standby": st["standby"] if local_alive else False,
            "level": round(st["level"], 4),
            "turn_id": st.get("turn_id") or "",
            "latest_user_utterance": dict(st.get("latest_user_utterance") or {}),
            "updated_at": st["updated_at"],
            "local_voice": {
                "running": local_alive,
                "pid": int(lv.get("pid") or 0) if local_alive else 0,
                "listening": bool(lv.get("listening")) if local_alive else False,
                "standby": bool(lv.get("standby")) if local_alive else False,
                "age_ms": int(age * 1000) if seen else None,
            },
            "panels": panels,
            "events": list(events),
        }
