# -*- coding: utf-8 -*-
"""Muse /api/tts/duplex：增量文本进、PCM 出，写入 PcmSink。"""

import json
import os
import queue
import threading
import time


def duplex_ws_url(muse_url):
    muse = (muse_url or "http://127.0.0.1:8002").rstrip("/")
    if muse.startswith("https://"):
        return "wss://" + muse[len("https://") :] + "/api/tts/duplex"
    if muse.startswith("http://"):
        return "ws://" + muse[len("http://") :] + "/api/tts/duplex"
    return "ws://" + muse + "/api/tts/duplex"


def speak_duplex_segments(
    segments,
    tts_provider,
    tts_overrides,
    sink,
    muse_url,
    cancel_event=None,
    turn_context=None,
    speak_lock=None,
    log=None,
    stage_log=None,
    on_error=None,
    on_done=None,
    fallback_speak=None,
    retry_count=0,
    first_audio_timeout=None,
    audio_idle_timeout=None,
):
    """消费 segments 队列（None 结束），经 duplex 合成，PCM 写入 sink。

    fallback_speak(remaining_segments) 在 duplex 失败且未打断时可选调用。
    """
    import websocket

    _log = log or (lambda *a: None)
    _stage = stage_log or (lambda *a, **k: None)
    cancel_event = cancel_event or threading.Event()
    lock = speak_lock or threading.Lock()
    first_audio_timeout = float(
        first_audio_timeout
        if first_audio_timeout is not None
        else os.environ.get("VOICE_CORE_TTS_FIRST_AUDIO_TIMEOUT",
                            os.environ.get("CAMERA_TTS_FIRST_AUDIO_TIMEOUT", "6"))
    )
    audio_idle_timeout = float(
        audio_idle_timeout
        if audio_idle_timeout is not None
        else os.environ.get("VOICE_CORE_TTS_AUDIO_IDLE_TIMEOUT",
                            os.environ.get("CAMERA_TTS_AUDIO_IDLE_TIMEOUT", "8"))
    )
    max_playback_seconds = float(os.environ.get(
        "VOICE_CORE_TTS_MAX_PLAYBACK_SECONDS", "120",
    ))
    playback_base_seconds = float(os.environ.get(
        "VOICE_CORE_TTS_PLAYBACK_BASE_SECONDS", "6",
    ))
    playback_seconds_per_char = float(os.environ.get(
        "VOICE_CORE_TTS_SECONDS_PER_CHAR", "0.35",
    ))

    started_at = time.perf_counter()
    consumed_segments = []
    completed_segments = 0
    first_audio_at = None
    first_text_submitted_at = None
    sender_thread = None
    sender_done = threading.Event()
    stop_sending = threading.Event()
    sender_error = []
    ws = None
    playback_started = False
    submitted_chars = 0
    playback_budget_exceeded = False
    turn_id = (turn_context or {}).get("id")

    try:
        # 等首句再开双工：LLM 偶发排队十几秒时，避免空闲 TTS WS 被上游掐掉
        # （表现为「双工 TTS 未返回音频」再重连，二次放大延迟感）
        boot_segment = None
        while True:
            if cancel_event.is_set():
                if on_done:
                    on_done(
                        elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                        completed_segments=0,
                        submitted_segments=0,
                        interrupted=True,
                        first_audio_received=False,
                    )
                return
            try:
                boot_segment = segments.get(timeout=0.15)
                break
            except queue.Empty:
                continue
        if boot_segment is None:
            if on_done:
                on_done(
                    elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                    completed_segments=0,
                    submitted_segments=0,
                    interrupted=cancel_event.is_set(),
                    first_audio_received=False,
                )
            return

        with lock:
            connect_started_at = time.perf_counter()
            ws = websocket.create_connection(
                duplex_ws_url(muse_url),
                timeout=20,
                enable_multithread=True,
            )
            ws.send(json.dumps({
                "event": "start",
                "provider": tts_provider,
                "overrides": tts_overrides or {},
            }, ensure_ascii=False))
            while True:
                ready_message = ws.recv()
                if not isinstance(ready_message, str):
                    continue
                ready_event = json.loads(ready_message)
                if ready_event.get("event") == "error":
                    raise RuntimeError(ready_event.get("error") or "双工 TTS 初始化失败")
                if ready_event.get("event") == "ready":
                    break
            ready_at = time.perf_counter()
            sample_rate = int(ready_event.get("sample_rate") or 24000)
            _stage(
                turn_context,
                "TTS双工就绪",
                "连接及准备=%.1fms；上游WS准备=%.1fms，重连=%s"
                % (
                    (ready_at - connect_started_at) * 1000,
                    float(ready_event.get("setup_ms") or 0),
                    "是" if ready_event.get("reconnected") else "否",
                ),
            )
            sink.start(sample_rate, turn_id=turn_id)
            playback_started = True

            def send_segments():
                nonlocal first_text_submitted_at, submitted_chars
                segment_index = 0
                pending = [boot_segment]
                try:
                    while True:
                        if pending:
                            segment = pending.pop(0)
                        else:
                            segment = segments.get()
                        if segment is None:
                            if not stop_sending.is_set():
                                ws.send(json.dumps({"event": "finish"}))
                            return
                        consumed_segments.append(segment)
                        submitted_chars += len(str(segment or ""))
                        segment_index += 1
                        if stop_sending.is_set():
                            continue
                        try:
                            ws.send(json.dumps({
                                "event": "text",
                                "text": segment,
                            }, ensure_ascii=False))
                            if first_text_submitted_at is None:
                                first_text_submitted_at = time.perf_counter()
                            _stage(
                                turn_context,
                                "TTS文本段%d提交" % segment_index,
                                "字符数=%d" % len(segment),
                            )
                        except Exception as error:
                            sender_error.append(error)
                            stop_sending.set()
                            try:
                                ws.close()
                            except Exception:
                                pass
                finally:
                    sender_done.set()

            sender_thread = threading.Thread(target=send_segments, daemon=True)
            sender_thread.start()
            ws.settimeout(0.1)
            tail = b""
            first_write_done_at = None
            last_message_at = time.perf_counter()
            while True:
                if first_audio_at is not None and sender_done.is_set():
                    # 预算墙钟只在「音频已停滞」时才兜底触发：
                    # 只要音频仍在持续流入（TTS 合成慢、长回复），就绝不因墙钟硬停，
                    # 否则长回复合成稍慢会被「说一半突然停住」。
                    audio_stalled = (
                        time.perf_counter() - last_message_at >= audio_idle_timeout
                    )
                    if audio_stalled:
                        playback_budget = min(
                            max_playback_seconds,
                            max(
                                8.0,
                                playback_base_seconds
                                + submitted_chars * playback_seconds_per_char,
                            ),
                        )
                        if time.perf_counter() - first_audio_at >= playback_budget:
                            playback_budget_exceeded = True
                            raise TimeoutError(
                                "TTS 播放超过文本预算 %.1fs（%d 字），已停止"
                                % (playback_budget, submitted_chars)
                            )
                if cancel_event.is_set():
                    _log("打断，停止双工 TTS")
                    stop_sending.set()
                    break
                if sender_error:
                    raise sender_error[0]
                try:
                    message = ws.recv()
                except websocket.WebSocketTimeoutException:
                    now = time.perf_counter()
                    if (
                        first_audio_at is None
                        and first_text_submitted_at is not None
                        and now - first_text_submitted_at >= first_audio_timeout
                    ):
                        raise TimeoutError(
                            "TTS 首音频等待超过 %.1fs" % first_audio_timeout
                        )
                    # 只有所有文本段都已提交给上游（sender 结束）后，才允许因
                    # 长时间无音频而切断。sender 未结束说明 LLM 仍在输出，
                    # 段与段之间可能有搜索/思考空隙（可达十几秒），不能掐断。
                    if (
                        first_audio_at is not None
                        and sender_done.is_set()
                        and now - last_message_at >= audio_idle_timeout
                    ):
                        raise TimeoutError(
                            "TTS 播放连续 %.1fs 无音频" % audio_idle_timeout
                        )
                    continue
                last_message_at = time.perf_counter()
                if isinstance(message, (bytes, bytearray)):
                    if first_audio_at is None:
                        first_audio_at = time.perf_counter()
                    chunk = tail + bytes(message)
                    even_length = len(chunk) - len(chunk) % 2
                    if even_length:
                        try:
                            sink.write(chunk[:even_length])
                        except Exception:
                            sink.stop()
                            sink.start(sample_rate, turn_id=turn_id)
                            sink.write(chunk[:even_length])
                        if first_write_done_at is None:
                            first_write_done_at = time.perf_counter()
                            _stage(
                                turn_context,
                                "首音频写入声卡",
                                "双工就绪→首PCM=%.1fms；首块写入=%.1fms；"
                                "首句提交→首PCM=%.1fms"
                                % (
                                    (first_audio_at - ready_at) * 1000,
                                    (first_write_done_at - first_audio_at) * 1000,
                                    (
                                        first_audio_at
                                        - (first_text_submitted_at or ready_at)
                                    ) * 1000,
                                ),
                            )
                    tail = chunk[even_length:]
                    continue
                event = json.loads(message)
                if event.get("event") == "segment_done":
                    completed_segments = max(
                        completed_segments,
                        int(event.get("index") or 0),
                    )
                elif event.get("event") == "error":
                    raise RuntimeError(event.get("error") or "双工 TTS 失败")
                elif event.get("event") == "done":
                    break
            if first_audio_at is None and consumed_segments:
                raise RuntimeError("双工 TTS 未返回音频")
            _log(
                "TTS 双工播放完成 %.3fs，共 %d 段"
                % (time.perf_counter() - started_at, completed_segments)
            )
            if on_done:
                on_done(
                    elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                    completed_segments=completed_segments,
                    submitted_segments=len(consumed_segments),
                    interrupted=cancel_event.is_set(),
                    first_audio_received=bool(first_audio_at is not None),
                )
    except Exception as error:
        if cancel_event.is_set():
            _log("双工 TTS 已因打断关闭")
        else:
            import traceback as _tb
            _log(
                "双工 TTS 回退:",
                repr(error),
                "|",
                _tb.format_exc().replace("\n", " ⏎ "),
            )
            if on_error:
                on_error(
                    error=str(error),
                    completed_segments=completed_segments,
                    submitted_segments=len(consumed_segments),
                    first_audio_received=bool(first_audio_at is not None),
                )
        stop_sending.set()
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if sender_thread is None:
            while True:
                segment = segments.get()
                if segment is None:
                    break
                consumed_segments.append(segment)
        else:
            sender_thread.join()
        # 已开始出声后出错（断连/超时）时，completed_segments 可能滞后于
        # 实际已播进度（segment_done 在音频发出后才上报，而 PCM 是边收边播）。
        # 若首音频已播但 completed 仍为 0，至少从第 1 段开始续播，避免把已
        # 播完的第一句重新合成播放一遍（用户听到「一句话回答两遍」）。
        resume_from = completed_segments
        if first_audio_at is not None and resume_from < 1:
            resume_from = 1
        remaining_segments = (
            [] if (cancel_event.is_set() or playback_budget_exceeded)
            else consumed_segments[resume_from:]
        )
        if remaining_segments and retry_count < 1 and fallback_speak is None:
            _log("双工 TTS 自动恢复：重试剩余 %d 段" % len(remaining_segments))
            stop_sending.set()
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
                ws = None
            if sender_thread is not None and not sender_done.is_set():
                sender_thread.join(timeout=0.5)
            if playback_started:
                try:
                    sink.stop()
                except Exception:
                    pass
                playback_started = False
            retry_segments = queue.Queue()
            for segment in remaining_segments:
                retry_segments.put(segment)
            retry_segments.put(None)
            return speak_duplex_segments(
                retry_segments,
                tts_provider,
                tts_overrides,
                sink,
                muse_url,
                cancel_event=cancel_event,
                turn_context=turn_context,
                speak_lock=speak_lock,
                log=log,
                stage_log=stage_log,
                on_error=on_error,
                on_done=on_done,
                fallback_speak=fallback_speak,
                retry_count=retry_count + 1,
                first_audio_timeout=first_audio_timeout,
                audio_idle_timeout=audio_idle_timeout,
            )
        if remaining_segments and fallback_speak and not cancel_event.is_set():
            with lock:
                fallback_speak(remaining_segments, completed_segments)
    finally:
        if playback_started:
            try:
                sink.stop()
            except Exception:
                pass
        stop_sending.set()
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if sender_thread is not None and not sender_done.is_set():
            sender_thread.join(timeout=0.5)
