# -*- coding: utf-8 -*-
"""语音终端回声参考门控：_ECHO_GATE 单例。

负责打断判定（真人插话 vs 自己喇叭回声）。依赖 state 的
SR/FRAME_MS/_BARGE_IN_EVENT 和 log 的 log/_diag_event。
"""
from __future__ import annotations

from speech.echo.playback_gate import PlaybackEchoGate

from devices.voice.terminal_log import _diag_event, log
from devices.voice.terminal_state import (
    FRAME_MS,
    INPUT_MODE,
    OUTPUT,
    SR,
    _BARGE_IN_EVENT,
)

_ECHO_GATE = PlaybackEchoGate(
    SR,
    FRAME_MS,
    _BARGE_IN_EVENT,
    logger=log,
    event_logger=_diag_event,
    # 本机声卡链路不可能有秒级声学延迟；摄像头/网络链路保留配置值。
    max_delay_ms=400 if INPUT_MODE == "pc" and OUTPUT != "camera" else None,
)
