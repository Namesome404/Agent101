# -*- coding: utf-8 -*-
"""WindowSchema 校验。"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

BLOCK_TYPES = {
    "section", "kv", "table", "list", "toggle", "button",
    "mermaid", "log", "iframe", "status", "markdown",
}

ALLOWED_ACTIONS = {
    "window.close", "window.refresh", "window.set_style", "window.reset_style",
    "venv.set_active", "prereq.install", "job.cancel",
    "coding.confirm_write", "coding.revert_last", "coding.cancel",
    "preview.lock", "preview.unlock", "preview.reload", "open_in_browser",
}


def validate_schema(raw: Any) -> Tuple[bool, str, Dict[str, Any]]:
    if not isinstance(raw, dict):
        return False, "schema 必须是对象", {}
    wid = str(raw.get("id") or "").strip() or "desk-window"
    title = str(raw.get("title") or "EV Desk").strip()[:80]
    style = raw.get("style") if isinstance(raw.get("style"), dict) else {}
    sections_in = raw.get("sections")
    if not isinstance(sections_in, list):
        sections_in = []
    sections: List[Dict[str, Any]] = []
    for sec in sections_in[:24]:
        if not isinstance(sec, dict):
            continue
        blocks_out = []
        for blk in (sec.get("blocks") or [])[:40]:
            if not isinstance(blk, dict):
                continue
            btype = str(blk.get("type") or "").strip()
            if btype not in BLOCK_TYPES:
                continue
            clean = {"type": btype}
            for key in ("title", "text", "provider", "action", "label", "src", "mermaid", "id"):
                if key in blk and blk[key] is not None:
                    clean[key] = blk[key]
            if "columns" in blk and isinstance(blk["columns"], list):
                clean["columns"] = [str(c)[:40] for c in blk["columns"][:12]]
            if "rows" in blk and isinstance(blk["rows"], list):
                clean["rows"] = blk["rows"][:200]
            if "items" in blk and isinstance(blk["items"], list):
                clean["items"] = blk["items"][:200]
            if "entries" in blk and isinstance(blk["entries"], list):
                clean["entries"] = blk["entries"][:200]
            if "row_actions" in blk and isinstance(blk["row_actions"], list):
                ras = []
                for ra in blk["row_actions"][:6]:
                    if not isinstance(ra, dict):
                        continue
                    act = str(ra.get("action") or "")
                    if act and act not in ALLOWED_ACTIONS:
                        continue
                    ras.append({
                        "type": str(ra.get("type") or "button"),
                        "action": act,
                        "label": str(ra.get("label") or act)[:40],
                    })
                clean["row_actions"] = ras
            if clean.get("action") and clean["action"] not in ALLOWED_ACTIONS:
                clean.pop("action", None)
            blocks_out.append(clean)
        sections.append({
            "title": str(sec.get("title") or "")[:80],
            "blocks": blocks_out,
        })
    out = {
        "id": wid[:80],
        "title": title,
        "style": style,
        "sections": sections,
        "preset": str(raw.get("preset") or "default")[:40],
    }
    return True, "", out


def markdown_fallback_schema(title: str, facts_text: str, window_id: str = "compose-fallback") -> Dict[str, Any]:
    return {
        "id": window_id,
        "title": title or "采集结果",
        "preset": "board",
        "style": {},
        "sections": [{
            "title": "原始数据",
            "blocks": [{
                "type": "markdown",
                "id": "facts",
                "text": (facts_text or "（无数据）")[:12000],
            }],
        }],
    }
