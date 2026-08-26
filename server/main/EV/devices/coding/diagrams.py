# -*- coding: utf-8 -*-
"""Mermaid 逻辑图：校验与轻度清理。"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple


def strip_fence(text: str) -> str:
    s = (text or "").strip()
    m = re.match(r"^```(?:mermaid)?\s*([\s\S]*?)```$", s, re.I)
    if m:
        return m.group(1).strip()
    return s


def validate_mermaid(text: str) -> Tuple[bool, str]:
    s = strip_fence(text)
    if not s:
        return False, "空图"
    head = s.splitlines()[0].strip().lower()
    if not (
        head.startswith("flowchart")
        or head.startswith("graph ")
        or head.startswith("statediagram")
        or head.startswith("stateDiagram".lower())
        or "statediagram" in head
    ):
        # stateDiagram-v2
        if not re.match(r"^(flowchart|graph|statediagram(-v2)?)\b", head, re.I):
            return False, "需要 flowchart / stateDiagram 开头"
    if len(s) > 20000:
        return False, "图过长"
    return True, s


def make_diagram(title: str, mermaid: str, dtype: str = "") -> Optional[Dict[str, Any]]:
    ok, cleaned = validate_mermaid(mermaid)
    if not ok:
        return None
    kind = dtype or ("stateDiagram-v2" if "state" in cleaned.splitlines()[0].lower() else "flowchart")
    return {"title": (title or "逻辑图")[:80], "type": kind, "mermaid": cleaned}
