# -*- coding: utf-8 -*-
"""Desk StyleSpec：白名单样式旋钮。"""
from __future__ import annotations

from typing import Any, Dict, Tuple

DEFAULT = {
    "theme": "dark",
    "density": "comfortable",
    "accent": "amber",
    "fontScale": 1.0,
    "preset": "default",
}

_THEMES = {"light", "dark", "system"}
_DENSITY = {"comfortable", "compact"}
_ACCENT = {"ink", "amber", "teal", "rose"}
_PRESET = {"default", "studio", "board"}


def validate(raw: Any) -> Tuple[bool, str, Dict[str, Any]]:
    base = dict(DEFAULT)
    if not isinstance(raw, dict):
        return True, "", base
    theme = str(raw.get("theme") or base["theme"]).strip().lower()
    if theme not in _THEMES:
        return False, "不支持的 theme", base
    density = str(raw.get("density") or base["density"]).strip().lower()
    if density not in _DENSITY:
        return False, "不支持的 density", base
    accent = str(raw.get("accent") or base["accent"]).strip().lower()
    if accent not in _ACCENT:
        return False, "不支持的 accent", base
    try:
        scale = float(raw.get("fontScale", base["fontScale"]))
    except Exception:
        scale = 1.0
    scale = max(0.85, min(1.4, scale))
    preset = str(raw.get("preset") or base["preset"]).strip().lower()
    if preset not in _PRESET:
        preset = "default"
    return True, "", {
        "theme": theme,
        "density": density,
        "accent": accent,
        "fontScale": scale,
        "preset": preset,
    }


def merge(current: Dict[str, Any], patch: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    cur = dict(DEFAULT)
    cur.update(current or {})
    cur.update(patch or {})
    return validate(cur)


def to_css_vars(style: Dict[str, Any]) -> Dict[str, str]:
    ok, _, s = validate(style)
    s = s if ok else dict(DEFAULT)
    theme = s["theme"]
    if theme == "system":
        theme = "dark"
    accents = {
        "ink": "#d6d2ca",
        "amber": "#e6b35a",
        "teal": "#6ec8b8",
        "rose": "#e0899a",
    }
    if theme == "light":
        return {
            "--desk-bg": "#f3f1ec",
            "--desk-panel": "#ffffff",
            "--desk-ink": "#1a1a1a",
            "--desk-muted": "#6a6560",
            "--desk-line": "rgba(0,0,0,.12)",
            "--desk-accent": accents.get(s["accent"], accents["amber"]),
            "--desk-font-scale": str(s["fontScale"]),
            "--desk-pad": "14px" if s["density"] == "comfortable" else "8px",
        }
    return {
        "--desk-bg": "#171717",
        "--desk-panel": "#121212",
        "--desk-ink": "#e8e4dc",
        "--desk-muted": "#a8a49c",
        "--desk-line": "rgba(214,210,202,.16)",
        "--desk-accent": accents.get(s["accent"], accents["amber"]),
        "--desk-font-scale": str(s["fontScale"]),
        "--desk-pad": "14px" if s["density"] == "comfortable" else "8px",
    }
