# -*- coding: utf-8 -*-
"""一轮语音回复：LLM delta → 分句 → duplex → Sink。"""

import os
import queue
import threading
import time

from speech.voice_core.duplex_speak import speak_duplex_segments
from speech.voice_core.segments import split_ready_segments


# 仅用于普通对话等 LLM 首字；检索/工具等待默认静音（不插「嗯/啊」）
_FILLER_ENABLED = os.environ.get(
    "VOICE_LATENCY_FILLER",
    "0",
).strip().lower() not in ("0", "", "off", "no", "false")
_FILLER_FIRST_MS = int(os.environ.get("VOICE_LATENCY_FILLER_FIRST_MS", "1200"))
_FILLER_NEXT_MS = int(os.environ.get("VOICE_LATENCY_FILLER_NEXT_MS", "2000"))
_FILLER_PHRASES = (
    "嗯……",
    "啊……",
)


def run_voice_turn(
    command,
    delta_stream,
    tts_provider,
    tts_overrides,
    sink,
    muse_url,
    cancel_event=None,
    turn_context=None,
    speak_lock=None,
    log=None,
    stage_log=None,
    stage_log_at=None,
    on_tts_done=None,
    on_tts_error=None,
    fallback_speak=None,
    use_duplex=True,
    non_duplex_speak=None,
    llm_metrics=None,
    started_at=None,
    delta_stream_factory=None,
    on_tool_progress=None,
):
    """跑完一轮：边收 LLM 边播。返回 (outcome, reply_text)。

    outcome: completed | interrupted | llm_error | llm_empty_reply | not_addressed
    delta_stream_factory: 若提供，则在打开流前先启动 TTS 线程（避免工具阻塞时整段静音）。
    on_tool_progress: 慢工具开始语回调（text）-> None。走独立一次性 TTS，
        不进 duplex 队列——避免空闲超时回退时把已播过的开始语重放一遍。
    """
    _log = log or (lambda *a: None)
    _stage = stage_log or (lambda *a, **k: None)
    _stage_at = stage_log_at or (lambda *a, **k: None)
    cancel_event = cancel_event or threading.Event()
    llm_metrics = llm_metrics if llm_metrics is not None else {}
    started_at = started_at or time.perf_counter()
    if turn_context is None:
        turn_context = {
            "id": time.strftime("%H%M%S"),
            "origin": started_at,
            "stages": {},
        }

    segments = queue.Queue()
    if use_duplex:
        worker = threading.Thread(
            target=speak_duplex_segments,
            kwargs={
                "segments": segments,
                "tts_provider": tts_provider,
                "tts_overrides": tts_overrides,
                "sink": sink,
                "muse_url": muse_url,
                "cancel_event": cancel_event,
                "turn_context": turn_context,
                "speak_lock": speak_lock,
                "log": log,
                "stage_log": stage_log,
                "on_error": on_tts_error,
                "on_done": on_tts_done,
                "fallback_speak": fallback_speak,
            },
            daemon=True,
        )
    else:
        if non_duplex_speak is None:
            raise ValueError("use_duplex=False 时需要提供 non_duplex_speak")
        worker = threading.Thread(
            target=non_duplex_speak,
            args=(segments, tts_provider, tts_overrides),
            daemon=True,
        )
    worker.start()

    first_content = threading.Event()
    filler_stop = threading.Event()
    filler_anchor = {"t": time.perf_counter()}
    # 检索/工具等待：全程不插「嗯/啊」
    # A buffering phrase already acknowledges the turn. Stacking additional
    # filler segments behind it can keep streaming TTS sessions alive long
    # after the real answer is ready.
    suppress_filler = {"v": bool(turn_context.get("suppress_latency_filler"))}

    def _latency_filler_loop():
        if not _FILLER_ENABLED:
            return
        phrases = list(_FILLER_PHRASES)
        idx = int(time.time()) % max(1, len(phrases))
        spoken = 0
        while spoken < 2:
            if suppress_filler["v"]:
                # 工具等待中：挂起填充，直到正文到来或取消
                while suppress_filler["v"] and not (
                    cancel_event.is_set()
                    or filler_stop.is_set()
                    or first_content.is_set()
                ):
                    time.sleep(0.05)
                return
            delay_ms = _FILLER_FIRST_MS if spoken == 0 else _FILLER_NEXT_MS
            while True:
                if (
                    cancel_event.is_set()
                    or filler_stop.is_set()
                    or first_content.is_set()
                    or suppress_filler["v"]
                ):
                    return
                remain = (
                    filler_anchor["t"]
                    + max(0.25, delay_ms / 1000.0)
                    - time.perf_counter()
                )
                if remain <= 0:
                    break
                time.sleep(min(0.05, remain))
            if (
                cancel_event.is_set()
                or filler_stop.is_set()
                or first_content.is_set()
                or suppress_filler["v"]
            ):
                return
            if time.perf_counter() + 0.02 < (
                filler_anchor["t"] + max(0.25, delay_ms / 1000.0)
            ):
                continue
            phrase = phrases[idx % len(phrases)]
            idx += 1
            try:
                segments.put(phrase)
                filler_anchor["t"] = time.perf_counter()
                spoken += 1
                _stage(turn_context, "LLM等待填充", "文本=%s" % repr(phrase))
            except Exception:
                return

    filler_thread = threading.Thread(target=_latency_filler_loop, daemon=True)
    filler_thread.start()

    if delta_stream_factory is not None:
        delta_stream = delta_stream_factory()

    reply_parts = []
    pending = ""
    first_token_at = None
    first_segment = True
    # 播放前防重复：模型（尤其 flash 档）偶发把同一句话原样重复两遍。
    # prompt 已要求禁止重复，这里是对 TTS 播放质量的最后防线——只拦本轮内
    # 与最近已提交文本完全重复、或包含且长度相近（高度疑似复述）的段。
    committed_segments = []
    # tool_ack 已播报的工具结果语：二轮 LLM 若复述同一结果（如「好了，
    # 灯已经调成红色」）应拦截，避免用户听到两遍。
    spoken_speech = []
    _STRIP_PUNCT = str.maketrans(
        "", "", "，。！？；、,.:：;!?·—…\"'（）()《》<>【】[]{}「」''\"\""
    )

    def _dedup_segment(segment):
        norm = "".join(str(segment or "").split()).translate(_STRIP_PUNCT)
        if not norm:
            return False
        for speech in spoken_speech[-2:]:
            speech_norm = "".join(str(speech or "").split()).translate(_STRIP_PUNCT)
            if len(speech_norm) < 4 or not speech_norm:
                continue
            # speech「灯已调成红色」vs 复述「好了灯已经调成红色」：字符重叠
            # 高且长度相近即视为复述。只对工具播报语生效，普通对话去重不受影响。
            overlap = len(set(norm) & set(speech_norm)) / max(
                1, len(set(speech_norm))
            )
            if overlap >= 0.75 and abs(len(norm) - len(speech_norm)) <= max(
                6, len(speech_norm) // 3
            ):
                _log("丢弃工具复述段:", repr(segment))
                return False
        for prev in committed_segments[-3:]:
            prev_norm = "".join(str(prev or "").split()).translate(_STRIP_PUNCT)
            if not prev_norm:
                continue
            if norm == prev_norm:
                _log("丢弃重复段:", repr(segment))
                return False
            if (norm in prev_norm or prev_norm in norm) and abs(
                len(norm) - len(prev_norm)
            ) <= max(6, len(prev_norm) // 3):
                _log("丢弃近重复段:", repr(segment))
                return False
        committed_segments.append(segment)
        return True

    try:
        for item in delta_stream:
            if cancel_event.is_set():
                _log("用户打断，取消当前 LLM 输出")
                break
            if isinstance(item, dict) and item.get("kind") == "round_done":
                # 动作流轮次边界：中间工具轮的叙述已实时播报，从最终答复里
                # 丢弃本轮累积。去重基线保留，防止最终答复与中间轮近重复。
                reply_parts = []
                pending = ""
                first_segment = True
                continue
            if isinstance(item, dict) and item.get("kind") == "tool_wait":
                suppress_filler["v"] = True
                turn_context["suppress_latency_filler"] = True
                filler_stop.set()
                _stage(
                    turn_context,
                    "工具静默等待",
                    "tool=%s" % (item.get("tool") or ""),
                )
                continue
            # 工具垫场事件：只播，不进回复正文（默认已关闭）
            if isinstance(item, dict) and item.get("kind") in (
                "tool_ack",
                "tool_progress",
            ):
                text = str(item.get("text") or "").strip()
                kind = item.get("kind")
                if not text:
                    continue
                if kind == "tool_ack" and turn_context.get("tool_ack_spoken"):
                    continue
                if kind == "tool_ack":
                    turn_context["tool_ack_spoken"] = True
                suppress_filler["v"] = True
                turn_context["suppress_latency_filler"] = True
                # 慢工具开始语：独立一次性 TTS 直接播，不塞进 duplex 队列。
                # 搜索等慢工具会让 duplex 空闲超时回退，重试会重放已播段；
                # 独立播放保证开始语只播一遍、不与正文重试纠缠。
                if kind == "tool_progress" and on_tool_progress:
                    try:
                        on_tool_progress(text)
                    except Exception as error:
                        _log("工具开始语独立播放失败:", error)
                    _stage(
                        turn_context,
                        "工具进度语",
                        "文本=%s" % repr(text),
                    )
                    continue
                segments.put(text)
                # 已播报的工具结果进去重基线：二轮 LLM 若复述同样结果
                # （如「好的，灯已经调绿了」）会被 _dedup_segment 拦下，避免重复。
                committed_segments.append(text)
                spoken_speech.append(text)
                filler_anchor["t"] = time.perf_counter()
                _stage(
                    turn_context,
                    "工具进度语" if kind == "tool_progress" else "工具垫场语",
                    "文本=%s" % repr(text),
                )
                continue
            delta = item if isinstance(item, str) else str(item or "")
            if not delta:
                continue
            if first_token_at is None:
                first_content.set()
                first_token_at = time.perf_counter()
                _log("LLM 首字 %.3fs" % (first_token_at - started_at))
                _stage_at(
                    turn_context,
                    "LLM首字",
                    first_token_at,
                    "Muse响应头=%.1fms"
                    % float(llm_metrics.get("muse_headers_ms", 0.0)),
                )
            reply_parts.append(delta)
            pending += delta
            ready, pending = split_ready_segments(pending, first_segment)
            if ready and first_segment:
                _stage(
                    turn_context,
                    "首个可播分句",
                    "字符数=%d" % len(ready[0]),
                )
            for segment in ready:
                if _dedup_segment(segment):
                    segments.put(segment)
            if ready:
                first_segment = False
    except Exception as error:
        _log("流式试聊失败:", error)
        filler_stop.set()
        segments.put(None)
        worker.join()
        return "llm_error", "".join(reply_parts).strip()

    filler_stop.set()
    if pending.strip() and not cancel_event.is_set():
        _final_segment = pending.strip()
        if _dedup_segment(_final_segment):
            segments.put(_final_segment)
    segments.put(None)
    llm_done_at = time.perf_counter()
    worker.join()

    if cancel_event.is_set():
        _log("当前回复已被用户打断")
        return "interrupted", "".join(reply_parts).strip()

    reply = "".join(reply_parts).strip()
    _log("回复:", reply)
    upstream = llm_metrics.get("upstream") or {}
    if upstream:
        _stage(
            turn_context,
            "LLM完成",
            "上游总计=%.1fms；结束原因=%s"
            % (
                upstream.get("upstream_total_ms", 0.0),
                upstream.get("finish_reason", ""),
            ),
        )
    _log(
        "LLM 完成 %.3fs，整轮播放完成 %.3fs"
        % (llm_done_at - started_at, time.perf_counter() - started_at)
    )
    if reply:
        return "completed", reply
    not_addressed = (
        llm_metrics.get("addressed") is False
        or upstream.get("addressed") is False
        or upstream.get("finish_reason") == "not_addressed"
    )
    return ("not_addressed" if not_addressed else "llm_empty_reply"), reply
