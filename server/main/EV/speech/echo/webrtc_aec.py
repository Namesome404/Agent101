# -*- coding: utf-8 -*-
"""WebRTC AEC3 wrapper — best open-source echo canceller for local duplex voice."""
from __future__ import annotations

import os

try:
    from devices.voice.env import migrate_camera_voice_environ as _migrate_voice_env
    _migrate_voice_env()
except Exception:
    pass
import threading

import numpy as np

_AEC_MODE = (os.environ.get("VOICE_AEC", "webrtc") or "webrtc").strip().lower()


class WebrtcAecEngine:
    """Per-playback AEC3 instance. Not thread-safe across calls without external lock."""

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = int(sample_rate)
        self._ec = None
        self._error = ""
        self.available = False
        if _AEC_MODE in ("0", "off", "false", "none", "disable", "disabled"):
            self._error = "disabled"
            return
        try:
            from pywebrtc_audio import EchoCanceller

            delay0 = int(float(os.environ.get("VOICE_AEC_DELAY_MS", "0") or 0))
            self._ec = EchoCanceller(
                sample_rate=self.sample_rate,
                num_channels=1,
                stream_delay_ms=max(0, delay0),
            )
            self.available = True
        except Exception as error:
            self._error = str(error)
            self._ec = None
            self.available = False

    @property
    def backend(self) -> str:
        if not self.available:
            return "none"
        return "webrtc_aec3"

    @property
    def error(self) -> str:
        return self._error

    def reset(self):
        if self._ec is None:
            return
        try:
            self._ec.reset()
        except Exception:
            pass

    def set_delay_ms(self, delay_ms: int):
        if self._ec is None:
            return
        try:
            self._ec.stream_delay_ms = max(0, int(delay_ms))
        except Exception:
            pass

    def process(self, near_pcm: bytes, far_pcm: bytes) -> bytes | None:
        """Cancel far-end echo from near-end frame. Returns cleaned int16 PCM or None."""
        if self._ec is None or not near_pcm or not far_pcm:
            return None
        if len(near_pcm) != len(far_pcm):
            return None
        try:
            near = np.frombuffer(near_pcm, dtype="<i2")
            far = np.frombuffer(far_pcm, dtype="<i2")
            if near.size == 0 or far.size != near.size:
                return None
            out = self._ec.process(near, far)
            if out is None:
                return None
            return np.asarray(out, dtype="<i2").tobytes()
        except Exception as error:
            self._error = str(error)
            return None


_ENGINE_LOCK = threading.Lock()
_ENGINE: WebrtcAecEngine | None = None


def get_webrtc_aec(sample_rate: int = 16000) -> WebrtcAecEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None or _ENGINE.sample_rate != int(sample_rate):
            _ENGINE = WebrtcAecEngine(sample_rate=sample_rate)
        return _ENGINE
