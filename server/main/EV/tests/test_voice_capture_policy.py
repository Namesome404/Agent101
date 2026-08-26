# -*- coding: utf-8 -*-
"""语音采集策略回归：不再使用固定 RMS 门槛或词表正则判停。"""

from devices.voice import terminal_state


def test_no_fixed_rms_suppression_setting():
    assert not hasattr(terminal_state, "MIN_RMS")


def test_long_turn_default_is_not_eight_seconds():
    assert terminal_state.MAX_UTT_SECONDS >= 30


def test_command_normalization_does_not_use_regex():
    assert terminal_state._normalized_command(" 调 成 绿 色。") == "调成绿色"
