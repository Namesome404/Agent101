# -*- coding: utf-8 -*-
"""Stable built-in Tauri Surface Apps.

The model changes typed app state; it never rewrites the timer or notes UI.
"""
from __future__ import annotations

import json
import time

from devices.coding.scene_store import scene_store


APP_MANIFESTS = {
    "timer": {
        "surface_id": "app-timer",
        "title": "计时器",
        "window": {"width": 560, "height": 360, "position": "center"},
        "commands": {"open", "start", "pause", "resume", "add", "reset", "status"},
    },
    "notes": {
        "surface_id": "app-notes",
        "title": "随手记",
        "window": {"width": 620, "height": 380, "position": "center"},
        "commands": {"open", "append", "replace", "clear", "status"},
    },
}


def command_from_event(surface_id, payload):
    """Translate a trusted built-in UI event into typed app arguments."""
    data = dict(payload or {})
    app_id = str(data.get("app_id") or "").strip()
    command = str(data.get("command") or "").strip()
    manifest = APP_MANIFESTS.get(app_id)
    if not manifest or str(surface_id or "") != manifest["surface_id"]:
        return None
    # Only controls that are actually rendered by the built-in shell belong here.
    if app_id != "timer" or command not in {"pause", "resume", "add"}:
        return None
    arguments = {"app_id": app_id, "command": command}
    if command == "add":
        try:
            seconds = int(data.get("seconds") or 0)
        except (TypeError, ValueError):
            return None
        if seconds <= 0:
            return None
        arguments["duration_seconds"] = seconds
    return arguments


def _current_state(surface_id):
    current = scene_store.get(surface_id) or {}
    data = current.get("data") if isinstance(current.get("data"), dict) else {}
    app = data.get("app") if isinstance(data.get("app"), dict) else {}
    state = app.get("state") if isinstance(app.get("state"), dict) else {}
    return dict(state)


def live_state(app_id: str) -> dict:
    """内置应用的当前真实状态，供对象契约暴露。

    计时器在跑的时候，剩余秒数是算出来的而不是存下来的——契约里如果直接抄
    存量字段，模型看到的会是「开始时的那个数」。世界快照要的是「现在还剩多少」。
    """
    state = _current_state("app-%s" % str(app_id or "").strip())
    if not state:
        return {}
    out = dict(state)
    if str(out.get("status") or "") == "running" and out.get("ends_at"):
        out["remaining_seconds"] = max(0, int(round(float(out["ends_at"]) - time.time())))
        if out["remaining_seconds"] == 0:
            out["status"] = "finished"
    out.pop("ends_at", None)
    return out


def _timer_state(command, args, current):
    now = time.time()
    state = {
        "status": str(current.get("status") or "idle"),
        "duration_seconds": max(1, int(current.get("duration_seconds") or 600)),
        "remaining_seconds": max(0, int(current.get("remaining_seconds") or 0)),
        "ends_at": float(current.get("ends_at") or 0),
    }
    if state["status"] == "running" and state["ends_at"]:
        state["remaining_seconds"] = max(0, int(round(state["ends_at"] - now)))
        if state["remaining_seconds"] == 0:
            state["status"] = "finished"
    seconds = int(args.get("duration_seconds") or 0)
    if command == "start":
        if seconds <= 0:
            raise ValueError("开始计时必须提供大于 0 的 duration_seconds。")
        state.update({
            "status": "running",
            "duration_seconds": seconds,
            "remaining_seconds": seconds,
            "started_at": now,
            "ends_at": now + seconds,
        })
    elif command == "pause":
        if state["status"] != "running":
            raise ValueError("当前没有正在运行的计时器。")
        state["remaining_seconds"] = max(0, int(round(state["ends_at"] - now)))
        state["status"] = "paused"
        state["ends_at"] = 0
    elif command == "resume":
        if state["status"] != "paused":
            raise ValueError("当前计时器没有暂停。")
        state["status"] = "running"
        state["ends_at"] = now + state["remaining_seconds"]
    elif command == "add":
        if seconds <= 0:
            raise ValueError("增加时间必须提供大于 0 的 duration_seconds。")
        state["duration_seconds"] += seconds
        state["remaining_seconds"] += seconds
        if state["status"] == "running":
            state["ends_at"] = max(now, state["ends_at"]) + seconds
    elif command == "reset":
        state.update({"status": "idle", "remaining_seconds": state["duration_seconds"], "ends_at": 0})
    return state


def _notes_state(command, args, current):
    items = [str(item) for item in list(current.get("items") or [])]
    text = str(args.get("text") or "").strip()
    if command == "append":
        if not text:
            raise ValueError("追加笔记必须提供 text。")
        items.append(text)
    elif command == "replace":
        if not text:
            raise ValueError("替换笔记必须提供 text。")
        items = [text]
    elif command == "clear":
        items = []
    return {
        "items": items,
        "text": "\n".join(items),
        "preview": False,
        "updated_at": time.time(),
    }


# 唯一保留的固定话术：计时器暂停/恢复。这两个动作用户按得最频繁、要的就是
# 一声确认，每次让模型现编反而啰嗦。其余全部交回模型自己组织——「计时器已打开」
# 「已经加上时间了」「窗口已关闭」这些都是同一个模子刻出来的，听着像念回执。
_FIXED_SPEECH = {
    ("timer", "pause"): {"zh": "暂停了。", "en": "Paused."},
    ("timer", "resume"): {"zh": "继续了。", "en": "Resumed."},
}


def _speech(app_id, command, lang="zh"):
    """只返回有意保留的固定话术；其余返回空串，由模型看着回执自己说。"""
    fixed = _FIXED_SPEECH.get((str(app_id), str(command)))
    if not fixed:
        return ""
    return fixed.get(str(lang), fixed["zh"])


def execute(arguments=None, *, aid=None):
    del aid
    args = dict(arguments or {})
    app_id = str(args.get("app_id") or "").strip()
    command = str(args.get("command") or "open").strip()
    manifest = APP_MANIFESTS.get(app_id)
    if not manifest:
        meta = {"ok": False, "action": "app", "reason": "unknown_surface_app", "app_id": app_id}
        return json.dumps(meta, ensure_ascii=False), meta
    if command not in manifest["commands"]:
        meta = {
            "ok": False, "action": "app", "reason": "unsupported_app_command",
            "app_id": app_id, "command": command,
        }
        return json.dumps(meta, ensure_ascii=False), meta
    surface_id = manifest["surface_id"]
    current = _current_state(surface_id)
    try:
        state = (
            _timer_state(command, args, current)
            if app_id == "timer"
            else _notes_state(command, args, current)
        )
    except (TypeError, ValueError) as error:
        meta = {
            "ok": False, "action": "app", "app_id": app_id, "command": command,
            "reason": "invalid_app_state", "detail": str(error),
        }
        return json.dumps(meta, ensure_ascii=False), meta
    data = {
        "title": manifest["title"],
        "window": dict(manifest["window"]),
        "app": {"id": app_id, "version": 1, "state": state},
        "content": {"type": "app", "app_id": app_id},
    }
    result = scene_store.upsert(
        surface_id, kind="surface-app", data=data, intent="inform",
        focus=True, visible=True,
    )
    rev = int(result.get("rev") or scene_store.rev)
    rendered = scene_store.wait_surface_ready(surface_id, min_rev=rev, timeout=1.2)
    meta = {
        "ok": bool(rendered),
        "accepted": True,
        "action": "app",
        "operation": command,
        "app_id": app_id,
        "surface_id": surface_id,
        "state": state,
        "rev": rev,
        "rendered": bool(rendered),
    }
    fixed = _speech(app_id, command, str(args.get("lang") or "zh"))
    if fixed:
        # 有意保留的那两句（暂停/恢复）：标记出来，object_control 才不会把它当
        # 成模子话抹掉。除此之外一律不往回执里塞话。
        meta["speech"] = fixed
        meta["speech_fixed"] = True
    # 模型自己写的那句优先播报。
    natural_reply = str(args.get("reply") or "").strip()[:240]
    if natural_reply:
        meta["direct_reply"] = natural_reply
    if not rendered:
        meta["reason"] = "surface_not_rendered"
        meta["detail"] = "窗口状态已提交，但桌面壳没有返回渲染回执。"
    return json.dumps(meta, ensure_ascii=False), meta
