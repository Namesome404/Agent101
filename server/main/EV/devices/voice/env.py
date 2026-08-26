# -*- coding: utf-8 -*-
"""语音终端环境变量：优先 VOICE_*，兼容旧名 CAMERA_VOICE_*。"""
from __future__ import annotations

import os
from typing import Optional


def voice_getenv(name: str, default: Optional[str] = None) -> Optional[str]:
    """读取 VOICE_<name>；未设置时回退 CAMERA_VOICE_<name>。"""
    key = "VOICE_%s" % name
    if key in os.environ:
        return os.environ.get(key)
    legacy = "CAMERA_VOICE_%s" % name
    if legacy in os.environ:
        return os.environ.get(legacy)
    return default


def voice_env_bool(name: str, default: bool = True) -> bool:
    raw = voice_getenv(name, "1" if default else "0")
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("0", "", "off", "no", "false")


def migrate_camera_voice_environ() -> None:
    """把进程环境里残留的 CAMERA_VOICE_* 映射到 VOICE_*（不覆盖已有 VOICE_*）。"""
    for key, value in list(os.environ.items()):
        if not key.startswith("CAMERA_VOICE_"):
            continue
        new_key = "VOICE_" + key[len("CAMERA_VOICE_") :]
        if new_key not in os.environ:
            os.environ[new_key] = value
