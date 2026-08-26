# -*- coding: utf-8 -*-
"""Desk 窗口注册、状态、SSE 事件、文本流。"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from typing import Any, Dict, Generator, List, Optional

from devices.desk import schema as schema_mod
from devices.desk import style_spec

_LOCK = threading.RLock()
_WINDOWS: Dict[str, Dict[str, Any]] = {}
_SUBSCRIBERS: Dict[str, List[queue.Queue]] = {}


def _now() -> float:
    return time.time()


def list_windows() -> List[Dict[str, Any]]:
    with _LOCK:
        return [
            {
                "id": w["id"],
                "title": w.get("title"),
                "style": w.get("style"),
                "updated_at": w.get("updated_at"),
                "preview_locked": w.get("preview_locked"),
            }
            for w in _WINDOWS.values()
        ]


def get_window(window_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        w = _WINDOWS.get(window_id)
        return json.loads(json.dumps(w)) if w else None


def _broadcast(window_id: str, event: Dict[str, Any]) -> None:
    with _LOCK:
        subs = list(_SUBSCRIBERS.get(window_id) or [])
        # also broadcast to "*"
        subs += list(_SUBSCRIBERS.get("*") or [])
    dead = []
    for q in subs:
        try:
            q.put_nowait(event)
        except Exception:
            dead.append(q)
    if dead:
        with _LOCK:
            for key in (window_id, "*"):
                _SUBSCRIBERS[key] = [q for q in (_SUBSCRIBERS.get(key) or []) if q not in dead]


def upsert_window(raw_schema: Dict[str, Any], *, data: Optional[Dict[str, Any]] = None, replace: bool = False) -> Dict[str, Any]:
    ok, err, sch = schema_mod.validate_schema(raw_schema)
    if not ok:
        raise ValueError(err or "invalid schema")
    wid = sch["id"]
    ok_s, _, style = style_spec.merge(sch.get("style") or {}, {})
    with _LOCK:
        existing = _WINDOWS.get(wid)
        created = existing is None
        if existing and not replace:
            # patch: keep style_rev / scroll hints
            style = style_spec.merge(existing.get("style") or {}, sch.get("style") or {})[2]
            win = existing
            win["title"] = sch["title"]
            win["sections"] = sch["sections"]
            win["preset"] = sch.get("preset") or win.get("preset")
            win["style"] = style
            win["css_vars"] = style_spec.to_css_vars(style)
            if data is not None:
                win["data"] = data
            win["updated_at"] = _now()
            win["style_rev"] = int(win.get("style_rev") or 0) + 1
        else:
            win = {
                "id": wid,
                "title": sch["title"],
                "sections": sch["sections"],
                "preset": sch.get("preset") or "default",
                "style": style,
                "css_vars": style_spec.to_css_vars(style),
                "data": data or {},
                "text_buffers": {},
                "logs": [],
                "preview_locked": False,
                "preview_url": "",
                "preview_path": "",
                "created_at": _now(),
                "updated_at": _now(),
                "style_rev": 1,
            }
            _WINDOWS[wid] = win
        snapshot = json.loads(json.dumps(win))
    _broadcast(wid, {
        "type": "schema" if created or replace else "state",
        "window_id": wid,
        "window": snapshot,
        "created": created,
    })
    return snapshot


def patch_style(window_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    with _LOCK:
        win = _WINDOWS.get(window_id)
        if not win:
            raise KeyError("window not found")
        ok, err, style = style_spec.merge(win.get("style") or {}, patch or {})
        if not ok:
            raise ValueError(err or "invalid style")
        win["style"] = style
        win["css_vars"] = style_spec.to_css_vars(style)
        win["style_rev"] = int(win.get("style_rev") or 0) + 1
        win["updated_at"] = _now()
        snap = json.loads(json.dumps(win))
    _broadcast(window_id, {"type": "style", "window_id": window_id, "style": style, "css_vars": snap["css_vars"]})
    return snap


def reset_style(window_id: str) -> Dict[str, Any]:
    return patch_style(window_id, dict(style_spec.DEFAULT))


def set_preview(window_id: str, *, url: str = "", path: str = "", locked: Optional[bool] = None) -> Dict[str, Any]:
    with _LOCK:
        win = _WINDOWS.get(window_id)
        if not win:
            # auto create preview shell
            pass
    if window_id not in _WINDOWS:
        upsert_window({
            "id": window_id,
            "title": "网站预览",
            "preset": "studio",
            "sections": [{
                "title": "预览",
                "blocks": [{"type": "iframe", "id": "preview", "src": url or ""}],
            }],
        })
    with _LOCK:
        win = _WINDOWS[window_id]
        if url:
            win["preview_url"] = url
            # patch iframe src in schema
            for sec in win.get("sections") or []:
                for blk in sec.get("blocks") or []:
                    if blk.get("type") == "iframe":
                        blk["src"] = url
        if path:
            win["preview_path"] = path
        if locked is not None:
            win["preview_locked"] = bool(locked)
        win["updated_at"] = _now()
        snap = json.loads(json.dumps(win))
    _broadcast(window_id, {
        "type": "preview",
        "window_id": window_id,
        "preview_url": snap.get("preview_url"),
        "preview_locked": snap.get("preview_locked"),
        "reload_token": int(_now() * 1000),
    })
    return snap


def append_log(window_id: str, line: str, *, level: str = "info") -> None:
    entry = {"ts": _now(), "level": level, "text": (line or "")[:500]}
    with _LOCK:
        win = _WINDOWS.get(window_id)
        if not win:
            return
        logs = win.setdefault("logs", [])
        logs.append(entry)
        if len(logs) > 300:
            del logs[: len(logs) - 300]
    _broadcast(window_id, {"type": "log_event", "window_id": window_id, "entry": entry})


def push_text_delta(window_id: str, block_id: str, delta: str) -> None:
    with _LOCK:
        win = _WINDOWS.get(window_id)
        if not win:
            return
        bufs = win.setdefault("text_buffers", {})
        bufs[block_id] = (bufs.get(block_id) or "") + (delta or "")
        full = bufs[block_id]
    _broadcast(window_id, {
        "type": "text_delta",
        "window_id": window_id,
        "block_id": block_id,
        "delta": delta or "",
        "text": full,
    })


def finish_text(window_id: str, block_id: str) -> None:
    with _LOCK:
        win = _WINDOWS.get(window_id)
        text = ""
        if win:
            text = (win.get("text_buffers") or {}).get(block_id) or ""
            # sync into markdown blocks
            for sec in win.get("sections") or []:
                for blk in sec.get("blocks") or []:
                    if blk.get("id") == block_id and blk.get("type") in ("markdown", "log"):
                        blk["text"] = text
    _broadcast(window_id, {
        "type": "text_done",
        "window_id": window_id,
        "block_id": block_id,
        "text": text,
    })


def stream_text(window_id: str, block_id: str, full_text: str, *, chunk: int = 28, delay_s: float = 0.02) -> None:
    """非流式上游时切片推送，避免整块闪现。"""
    text = full_text or ""
    with _LOCK:
        win = _WINDOWS.get(window_id)
        if win:
            win.setdefault("text_buffers", {})[block_id] = ""
    i = 0
    while i < len(text):
        push_text_delta(window_id, block_id, text[i: i + chunk])
        i += chunk
        time.sleep(max(0.0, delay_s))
    finish_text(window_id, block_id)


def close_window(window_id: str) -> bool:
    with _LOCK:
        existed = window_id in _WINDOWS
        _WINDOWS.pop(window_id, None)
    if existed:
        _broadcast(window_id, {"type": "closed", "window_id": window_id})
    return existed


def subscribe(window_id: str = "*") -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=200)
    with _LOCK:
        _SUBSCRIBERS.setdefault(window_id or "*", []).append(q)
    return q


def unsubscribe(q: queue.Queue, window_id: str = "*") -> None:
    with _LOCK:
        arr = _SUBSCRIBERS.get(window_id or "*") or []
        _SUBSCRIBERS[window_id or "*"] = [x for x in arr if x is not q]


def sse_events(window_id: str = "*", *, heartbeat_s: float = 15.0) -> Generator[str, None, None]:
    q = subscribe(window_id)
    # initial snapshot
    if window_id and window_id != "*":
        w = get_window(window_id)
        if w:
            yield _sse({"type": "schema", "window_id": window_id, "window": w, "created": False})
    else:
        yield _sse({"type": "windows", "windows": list_windows()})
    try:
        last_beat = time.time()
        while True:
            try:
                ev = q.get(timeout=1.0)
                yield _sse(ev)
            except queue.Empty:
                if time.time() - last_beat >= heartbeat_s:
                    yield ": ping\n\n"
                    last_beat = time.time()
    finally:
        unsubscribe(q, window_id)


def _sse(data: Dict[str, Any]) -> str:
    return "data: %s\n\n" % json.dumps(data, ensure_ascii=False)


def new_window_id(prefix: str = "win") -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:8])
