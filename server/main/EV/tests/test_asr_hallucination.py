# -*- coding: utf-8 -*-
"""ASR 幻觉门槛：近乎静音里「听出」整句指令要被拦掉，真人说话不能误伤。

背景：火山流式 ASR 会在基本无声的音频段返回一整句通顺的中文命令，并被当成
真指令执行（实测 0.46s / RMS=13 的静音段识别出 34 字命令、还开了窗口）。
门槛取自真实录音统计：真人语音最轻 RMS≈628，静音地板设 60（约 10 倍余量）。
"""
from devices.voice.terminal_log import _asr_hallucination_reason, _speech_units


def test_silent_input_with_text_is_hallucination():
    """近乎无声却返回整句 → 判幻觉（真实复现过的三例）。"""
    assert _asr_hallucination_reason(
        "帮我生成一张图片，图片内容是帮我写一个关于孤勇者的故事，比例1:1。", 13, 0.46,
    ) == "silent_input"
    assert _asr_hallucination_reason(
        "帮我生成一张图片，图片风格为电影写真比例9:16", 10, 0.50,
    ) == "silent_input"
    assert _asr_hallucination_reason("帮我导航", 51, 0.48) == "silent_input"


def test_real_speech_passes():
    """正常音量的真人说话必须放行。"""
    assert _asr_hallucination_reason("把孤勇者的故事删掉。", 2174, 1.94) == ""
    assert _asr_hallucination_reason("把灯打开", 628, 1.2) == ""


def test_english_speech_not_flagged_by_rate():
    """英文按词计速：40 个字符只有 9 个词，属正常语速，不能误判超速。"""
    assert _asr_hallucination_reason(
        "I come to office a little bit late today", 2058, 2.66,
    ) == ""
    assert _asr_hallucination_reason(
        "Actually, it is not a little, like", 2560, 2.30,
    ) == ""


def test_impossible_rate_is_hallucination():
    """音量正常但语速物理不可能（中文 30 字挤进 1 秒）→ 判幻觉。"""
    assert _asr_hallucination_reason("帮我把所有窗口都关掉然后打开哔哩哔哩再放首歌听听", 1500, 1.0) == "impossible_rate"


def test_empty_text_is_not_hallucination():
    """空识别走既有的 asr_empty 路径，不归本门槛管。"""
    assert _asr_hallucination_reason("", 5, 0.4) == ""
    assert _asr_hallucination_reason("   ", 5, 0.4) == ""


def test_speech_units_counts_cjk_by_char_latin_by_word():
    assert _speech_units("把灯打开") == 4
    assert _speech_units("I come to office") == 4
    assert _speech_units("打开 bilibili") == 3  # 2 汉字 + 1 词
