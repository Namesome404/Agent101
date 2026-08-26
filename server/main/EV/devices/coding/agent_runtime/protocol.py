# -*- coding: utf-8 -*-
"""Provider-neutral events exposed to EV and the compact work HUD."""
from __future__ import annotations

import time
from typing import Any, Dict


TERMINAL_STATUSES = frozenset({"completed", "needs_input", "cancelled", "failed"})

_PHASE_LABELS = {
    "starting": "准备",
    "planning": "计划",
    "reading": "查看",
    "editing": "修改",
    "checking": "检查",
    "working": "处理",
    "completed": "完成",
    "needs_input": "待补充",
    "cancelled": "已停止",
    "failed": "失败",
}


def phase_label(phase: str) -> str:
    return _PHASE_LABELS.get(str(phase or "working"), "处理")


def event(
    kind: str,
    *,
    phase: str = "working",
    detail: str = "",
    path: str = "",
    ok: Any = None,
    **extra,
) -> Dict[str, Any]:
    """Create the small, stable event contract consumed by every UI."""
    item: Dict[str, Any] = {
        "kind": str(kind or "activity"),
        "phase": str(phase or "working"),
        "label": phase_label(phase),
        "detail": str(detail or "")[:500],
        "path": str(path or "")[:500],
        "ts": time.time(),
    }
    if ok is not None:
        item["ok"] = bool(ok)
    for key, value in extra.items():
        if value is not None:
            item[key] = value
    return item


def public_event(item: Dict[str, Any]) -> Dict[str, Any]:
    """Remove provider internals and any accidental reasoning payload."""
    allowed = {
        "seq", "kind", "phase", "label", "detail", "path", "ok", "ts",
        "files", "checks", "added", "removed", "status", "command",
    }
    return {key: value for key, value in dict(item or {}).items() if key in allowed}
