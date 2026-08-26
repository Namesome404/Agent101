# -*- coding: utf-8 -*-
"""VAD 起始门控：用环境噪声底压住蓝牙全双工产生的伪语音。"""
from __future__ import annotations

import collections
import os
import statistics


class AdaptiveVadStartGate:
    """只约束一句话的起点，不参与已经开始后的判停。

    WebRTC VAD 会把部分窄带底噪和短促高频脉冲标成 speech。蓝牙耳麦同时
    收放音时这种情况尤其常见。这里保留一段空闲期能量历史，用动态噪声底
    再确认 VAD 起点；真正触发后完全交还给声学 VAD，避免吃掉轻声词尾。
    """

    def __init__(
        self,
        *,
        history_frames: int = 150,
        minimum_rms: int | None = None,
        noise_multiplier: float | None = None,
    ):
        self.history = collections.deque(maxlen=max(12, int(history_frames)))
        self.minimum_rms = max(
            0,
            int(
                minimum_rms
                if minimum_rms is not None
                else os.environ.get("VOICE_VAD_START_RMS_FLOOR", "60")
            ),
        )
        self.noise_multiplier = max(
            1.0,
            float(
                noise_multiplier
                if noise_multiplier is not None
                else os.environ.get("VOICE_VAD_NOISE_MULTIPLIER", "2.5")
            ),
        )

    @property
    def noise_rms(self) -> int:
        if not self.history:
            return 0
        return int(statistics.median(self.history))

    @property
    def threshold(self) -> int:
        return max(
            self.minimum_rms,
            int(round(self.noise_rms * self.noise_multiplier)),
        )

    def accept(self, frame_rms: int, vad_speech: bool) -> bool:
        """返回该空闲帧能否计入起始 speech，并持续学习非语音能量。"""
        rms = max(0, int(frame_rms or 0))
        accepted = bool(vad_speech) and rms >= self.threshold
        if not accepted:
            self.history.append(rms)
        return accepted

    def reset(self) -> None:
        self.history.clear()
