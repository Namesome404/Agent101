# -*- coding: utf-8 -*-
"""可复用语音核：LLM 流式分句 → Muse duplex TTS → PCM Sink。

设备只提供 Source / Sink；中间链路不按设备重写。
摄像头终端已迁到此核；ESP32 / 浏览器后续接同一套。
"""

from speech.voice_core.dialog import MuseDialogClient
from speech.voice_core.duplex_speak import duplex_ws_url, speak_duplex_segments
from speech.voice_core.segments import split_ready_segments
from speech.voice_core.sinks import PcmSink
from speech.voice_core.turn import run_voice_turn

__all__ = [
    "MuseDialogClient",
    "PcmSink",
    "duplex_ws_url",
    "run_voice_turn",
    "speak_duplex_segments",
    "split_ready_segments",
]
