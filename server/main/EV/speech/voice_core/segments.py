# -*- coding: utf-8 -*-
"""LLM 增量文本 → 可播分句（低延迟首句）。"""

import os

try:
    from devices.voice.env import migrate_camera_voice_environ as _migrate_voice_env
    _migrate_voice_env()
except Exception:
    pass
import re

FIRST_SEGMENT_CHARS = int(os.environ.get("VOICE_CORE_FIRST_SEGMENT_CHARS",
                          os.environ.get("VOICE_FIRST_SEGMENT_CHARS", "18")))
NEXT_SEGMENT_CHARS = int(os.environ.get("VOICE_CORE_NEXT_SEGMENT_CHARS",
                         os.environ.get("VOICE_NEXT_SEGMENT_CHARS", "42")))

_ENGLISH_ABBREVIATIONS = {
    "dr", "etc", "jr", "mr", "mrs", "ms", "prof", "sr", "st", "vs",
}


def _sentence_break_ends(text):
    """Return sentence-boundary offsets that are safe during streaming.

    An ASCII period is only committed after following text arrives. This keeps
    decimals such as ``21.6`` intact and avoids treating a trailing, still
    ambiguous period as a complete sentence too early.
    """
    for match in re.finditer(r"[。！？!?；;.]", text):
        punctuation = match.group(0)
        if punctuation != ".":
            yield match.end()
            continue
        index = match.start()
        before = text[index - 1] if index > 0 else ""
        after = text[index + 1] if index + 1 < len(text) else ""
        if before.isdigit() and after.isdigit():
            continue
        if not re.match(r"\s+\S", text[index + 1 :]):
            continue
        token_match = re.search(r"([A-Za-z]+)$", text[:index])
        token = (token_match.group(1).lower() if token_match else "")
        if len(token) == 1 or token in _ENGLISH_ABBREVIATIONS:
            continue
        yield match.end()


def split_ready_segments(text, first_segment=True,
                         first_chars=None, next_chars=None):
    """从流式文本中切出已可送 TTS 的完整分句，返回 (ready_list, remaining)。"""
    ready = []
    start = 0
    for end in _sentence_break_ends(text):
        segment = text[start:end].strip()
        if segment:
            ready.append(segment)
            start = end
    remaining = text[start:]
    threshold = (
        (first_chars if first_chars is not None else FIRST_SEGMENT_CHARS)
        if first_segment
        else (next_chars if next_chars is not None else NEXT_SEGMENT_CHARS)
    )
    if not ready and len(remaining.strip()) >= threshold:
        stripped = remaining.strip()
        natural_end = max(
            stripped.rfind("，"),
            stripped.rfind(","),
            stripped.rfind("："),
            stripped.rfind(":"),
            stripped.rfind("、"),
        )
        if natural_end + 1 >= max(10, threshold // 2):
            ready.append(stripped[: natural_end + 1])
            remaining = stripped[natural_end + 1 :]
        elif len(stripped) >= threshold and not re.search(r"[A-Za-z]", stripped):
            # 首段无标点时按阈值硬切，让 TTS 提前出声
            # （整句一次性到达会等完整句子才出首 PCM，实测慢 ~4 倍）。
            # 拉丁文本不能这样切，否则会把 Understood 切成 Understo + od，
            # 每个碎片都会让流式 TTS 重新起调；它应等待句号或自然标点。
            ready.append(stripped[:threshold])
            remaining = stripped[threshold:]
    return ready, remaining
