# -*- coding: utf-8 -*-
"""出声端接口：VoiceCore 只产出 PCM，由 Sink 决定播到哪。"""

from typing import Optional, Protocol


class PcmSink(Protocol):
    """PCM 播放/转发端。浏览器可转 Opus，ESP32 可扇出编码，PC 可直写声卡。"""

    def start(self, sample_rate: int, *, turn_id: Optional[str] = None) -> None:
        """开始一轮播放（采样率由 duplex ready 决定）。"""

    def write(self, pcm: bytes) -> None:
        """写入 16-bit LE 单声道 PCM。"""

    def stop(self) -> None:
        """结束本轮（打断或正常结束都调用）。"""
