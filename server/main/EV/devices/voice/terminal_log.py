# -*- coding: utf-8 -*-
"""语音终端日志与诊断：log / 结构化诊断事件 / 阶段计时 / 回合汇总。

只依赖 terminal_state（SR/TMP 常量），被其余模块共享。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import threading
import time
import wave

from devices.voice.terminal_state import SR, TMP


def log(*a):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    try:
        print("[%s] [voice]" % timestamp, *a, flush=True)
    except UnicodeEncodeError:
        message = " ".join(str(item) for item in a)
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_message = message.encode(encoding, errors="replace").decode(
            encoding, errors="replace"
        )
        print("[%s] [voice]" % timestamp, safe_message, flush=True)


_DIAG_ENABLED = os.environ.get(
    "VOICE_DIAGNOSTICS",
    "1",
).lower() not in ("0", "", "off", "no", "false")
_DIAG_PATH = os.path.join(TMP, "voice_terminal_diagnostics.jsonl")
_DIAG_AUDIO_DIR = os.path.join(TMP, "voice_terminal_audio")
_DIAG_AUDIO_LIMIT = max(
    10,
    int(os.environ.get("VOICE_DIAG_AUDIO_LIMIT", "50")),
)
_DIAG_LOCK = threading.Lock()
_DIAG_IMPORTANT_EVENTS = {
    "barge_in_triggered",
    "barge_in_candidate_rejected",
    "dialog_suppressed",
    "asr_result",
    "turn_summary",
    "tts_error",
}


def _diag_event(event, turn_id=None, **fields):
    if not _DIAG_ENABLED:
        return
    record = {
        "time": datetime.datetime.now().astimezone().isoformat(
            timespec="milliseconds",
        ),
        "monotonic": round(time.perf_counter(), 6),
        "event": event,
    }
    if turn_id:
        record["turn_id"] = turn_id
    record.update(fields)
    line = json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    try:
        with _DIAG_LOCK:
            os.makedirs(os.path.dirname(_DIAG_PATH), exist_ok=True)
            with open(_DIAG_PATH, "a", encoding="utf-8") as diag_file:
                diag_file.write(line + "\n")
    except Exception as error:
        log("诊断日志写入失败:", error)
        return
    if event in _DIAG_IMPORTANT_EVENTS:
        compact = {
            key: value
            for key, value in fields.items()
            if key not in ("stages", "metrics")
        }
        log("[diag]", event, "turn=%s" % (turn_id or "-"), compact)


def _save_diag_audio(turn_id, pcm_bytes):
    if not _DIAG_ENABLED or not pcm_bytes:
        return ""
    try:
        os.makedirs(_DIAG_AUDIO_DIR, exist_ok=True)
        path = os.path.join(_DIAG_AUDIO_DIR, "%s.wav" % turn_id)
        with wave.open(path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SR)
            wav_file.writeframes(pcm_bytes)
        files = sorted(
            (
                os.path.join(_DIAG_AUDIO_DIR, name)
                for name in os.listdir(_DIAG_AUDIO_DIR)
                if name.lower().endswith(".wav")
            ),
            key=os.path.getmtime,
        )
        for stale_path in files[:-_DIAG_AUDIO_LIMIT]:
            try:
                os.remove(stale_path)
            except OSError:
                pass
        return path
    except Exception as error:
        log("诊断音频保存失败:", error)
        return ""


# ASR 幻觉门槛：火山/豆包流式偶尔会在近乎无声的音频里「听出」一整句通顺指令
# （实测：0.46s、RMS=13 的静音段被识别成 30 字的完整命令并被执行）。
# 这里不恢复「送 ASR 前按 RMS 一刀切」那道旧门槛——它会误伤轻声说话；
# 只在拿到文本后用两条物理判据兜底，任一命中即判定为幻觉：
#   1) 能量地板：整段基本无声却返回了文字；
#   2) 语速不可能：语速单位/秒 超过人类上限（中文按字、英文按词，正常 4~6）。
# 阈值取得很保守，真实轻声说话（RMS 数百、语速个位数）不会被误杀。
SILENT_RMS_FLOOR = int(os.environ.get("VOICE_ASR_SILENT_RMS", "60"))
MAX_UNITS_PER_SEC = float(os.environ.get("VOICE_ASR_MAX_UPS", "12"))


def _speech_units(text):
    """把文本折算成「语速单位」：中日韩按字计，拉丁按词计。

    直接用 len() 会把英文冤枉死——"I come to office a little bit late today"
    有 40 个字符却只有 9 个词，正常语速会被误判成超速。
    """
    value = str(text or "")
    cjk = sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
    words = len(re.findall(r"[A-Za-z]+|\d+", value))
    return cjk + words


def _asr_hallucination_reason(text, rms, duration_s):
    """返回幻觉原因；不是幻觉则返回空串。"""
    value = str(text or "").strip()
    if not value:
        return ""
    try:
        rms = float(rms or 0)
        duration_s = float(duration_s or 0)
    except (TypeError, ValueError):
        return ""
    if rms <= SILENT_RMS_FLOOR:
        return "silent_input"
    units = _speech_units(value)
    if duration_s > 0 and units and units / duration_s > MAX_UNITS_PER_SEC:
        return "impossible_rate"
    return ""


def _asr_quality_flags(text, provider, rms, forced_cutoff):
    value = str(text or "").strip()
    flags = []
    if not value:
        flags.append("empty")
    if value in {"嗯", "嗯。", "啊", "啊。", "哦", "哦。", "对", "对。"}:
        flags.append("filler_only")
    if value and len(value) <= 2:
        flags.append("very_short")
    if provider == "mimo":
        flags.append("fallback_provider")
    # RMS 只作为观测值写入 asr_result，不再用固定阈值给真实话音贴
    # “低质量”标签；是否有有效语音由 VAD + ASR 结果决定。
    if forced_cutoff:
        flags.append("forced_cutoff")
    if value and value[-1:] in {
        "把", "被", "给", "跟", "和", "但", "在", "是", "让", "自",
    }:
        flags.append("possibly_truncated")
    return flags


def _stage_log(turn_context, stage, detail=""):
    _stage_log_at(turn_context, stage, time.perf_counter(), detail)


def _stage_log_at(turn_context, stage, happened_at, detail=""):
    if not turn_context:
        return
    elapsed = happened_at - turn_context["origin"]
    turn_context.setdefault("stages", {})[stage] = round(elapsed * 1000, 1)
    suffix = (" | " + detail) if detail else ""
    log("[turn %s] %s +%.3fs%s" % (
        turn_context["id"],
        stage,
        elapsed,
        suffix,
    ))
    _diag_event(
        "turn_stage",
        turn_id=turn_context["id"],
        stage=stage,
        elapsed_ms=round(elapsed * 1000, 1),
        detail=detail,
    )


def _emit_turn_summary(
    turn_context,
    outcome,
    command="",
    reply="",
    llm_metrics=None,
):
    if not turn_context:
        return
    total_ms = round(
        (time.perf_counter() - turn_context["origin"]) * 1000,
        1,
    )
    stages = dict(turn_context.get("stages") or {})
    vad_ms = float(stages.get("VAD判停") or 0.0)
    asr_ms = float(stages.get("ASR完成") or vad_ms)
    llm_first_ms = float(stages.get("LLM首字") or asr_ms)
    sentence_ms = float(stages.get("首个可播分句") or llm_first_ms)
    first_audio_ms = float(stages.get("首音频写入声卡") or sentence_ms)
    components = {
        "vad_endpoint_ms": round(max(0.0, vad_ms), 1),
        "asr_after_vad_ms": round(max(0.0, asr_ms - vad_ms), 1),
        "llm_first_token_ms": round(max(0.0, llm_first_ms - asr_ms), 1),
        "text_buffering_ms": round(
            max(0.0, sentence_ms - llm_first_ms),
            1,
        ),
        "tts_first_audio_ms": round(
            max(0.0, first_audio_ms - sentence_ms),
            1,
        ),
        "playback_after_first_audio_ms": round(
            max(0.0, total_ms - first_audio_ms),
            1,
        ),
    }
    slowest_stage = max(components, key=components.get)
    _diag_event(
        "turn_summary",
        turn_id=turn_context["id"],
        outcome=outcome,
        total_ms=total_ms,
        first_audible_ms=(
            round(first_audio_ms, 1)
            if "首音频写入声卡" in stages
            else None
        ),
        slowest_stage=slowest_stage,
        slowest_stage_ms=components[slowest_stage],
        components=components,
        stages=stages,
        command=command,
        reply_chars=len(reply or ""),
        upstream=(llm_metrics or {}).get("upstream") or {},
    )
