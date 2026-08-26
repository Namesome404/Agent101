# -*- coding: utf-8 -*-
"""最近一次写码结果（供语音诚实 prompt 读取，避免编造）。"""
from __future__ import annotations

import time
from typing import Any, Dict

_LAST: Dict[int, Dict[str, Any]] = {}


def remember(aid: int, result: dict, *, task: str = "") -> None:
    try:
        _LAST[int(aid) or 0] = {
            "ok": bool((result or {}).get("ok")),
            "at": time.time(),
            "summary": str((result or {}).get("summary") or "")[:800],
            "preview_url": str((result or {}).get("preview_url") or ""),
            "error": str((result or {}).get("error") or ""),
            "task": (task or "")[:400],
            "verified_changes": bool((result or {}).get("verified_changes")),
            "files": [
                a.get("path") for a in ((result or {}).get("artifacts") or [])[:12]
                if isinstance(a, dict)
            ],
        }
    except Exception:
        pass


def get(aid: int) -> Dict[str, Any]:
    return dict(_LAST.get(int(aid) or 0) or {})
