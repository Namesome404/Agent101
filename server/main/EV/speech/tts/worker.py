# -*- coding: utf-8 -*-
"""Streaming TTS worker running in the voice-core (server) provider context.

由 tts_duplex.py 派生一次并常驻复用（避免每轮 spawn+import+连火山 的 ~1.2s 冷启动）。
用 conn 垫片驱动任意核心流式 provider；provider 内部把音频转 opus，这里 opus→PCM。

stdin(JSON行)：
  {cmd:start, block}   首次：建 provider+开通道，并开始第一个会话
  {cmd:session}        新一轮会话（复用 provider 与火山 WS）
  {cmd:text, text}     下发文本
  {cmd:finish}         本轮结束
stdout(帧)：1=音频PCM(s16le@sr)  2=控制JSON(ready/segment_done/done/error)
"""
import asyncio
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
import uuid
from pathlib import Path

_FRAME_SOCKET = socket.create_connection(
    (
        "127.0.0.1",
        int(os.environ["MUSE_TTS_FRAME_PORT"]),
    ),
    timeout=10,
)
_FRAME_SOCKET.settimeout(None)

_SERVER = str(Path(__file__).resolve().parents[3] / "server")
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType
from core.utils import tts as tts_factory
import opuslib

SAMPLE_RATE = 24000
_out_lock = threading.Lock()


def log_stage(name, started_at):
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    print(
        "[tts-worker] %s %.1fms" % (name, elapsed_ms),
        file=sys.stderr,
        flush=True,
    )


def _frame(typ, payload):
    with _out_lock:
        _FRAME_SOCKET.sendall(
            bytes([typ]) + struct.pack(">I", len(payload)) + payload
        )


def send_audio(pcm):
    _frame(1, pcm)


def send_ctrl(obj):
    _frame(2, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


class _ConnShim:
    def __init__(self, loop):
        self.stop_event = threading.Event()
        self.client_abort = False
        self.sentence_id = uuid.uuid4().hex
        self.sample_rate = SAMPLE_RATE
        self.audio_format = "opus"
        self.loop = loop
        self.headers = {}
        self.max_output_size = 0


async def main(provider, provider_type, initialized_at):
    loop = asyncio.get_running_loop()
    cmd_q = asyncio.Queue()

    def stdin_reader():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except Exception:
                continue
            loop.call_soon_threadsafe(cmd_q.put_nowait, cmd)
        loop.call_soon_threadsafe(cmd_q.put_nowait, None)  # stdin 关闭

    threading.Thread(target=stdin_reader, daemon=True).start()

    conn = _ConnShim(loop)
    provider.conn = conn
    provider._audio_play_priority_thread = lambda: None
    if provider_type == "huoshan_double_stream":
        playback_chunk_bytes = max(
            2,
            int(conn.sample_rate * 0.02) * 2,
        )

        def forward_raw_pcm(
            raw_audio,
            is_end=False,
            callback=None,
        ):
            del is_end, callback
            if raw_audio:
                pcm = bytes(raw_audio)
                for offset in range(0, len(pcm), playback_chunk_bytes):
                    chunk = pcm[offset:offset + playback_chunk_bytes]
                    if len(chunk) % 2:
                        chunk = chunk[:-1]
                    if chunk:
                        send_audio(chunk)

        provider.wav_to_opus_data_audio_raw_stream = forward_raw_pcm
    decoder = opuslib.Decoder(SAMPLE_RATE, 1)
    frame_samples = int(SAMPLE_RATE * 0.06)
    started = {"f": False}
    session_state = {"segments": 0}

    def begin_session():
        conn.sentence_id = uuid.uuid4().hex
        conn.client_abort = False
        started["f"] = False
        session_state["segments"] = 0
        ensure_first()
        send_ctrl({"event": "ready", "sample_rate": conn.sample_rate})

    def ensure_first():
        if not started["f"]:
            started["f"] = True
            provider.tts_text_queue.put(TTSMessageDTO(
                sentence_id=conn.sentence_id, sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION, content_detail=""))

    def feed_text(text):
        ensure_first()
        provider.tts_text_queue.put(TTSMessageDTO(
            sentence_id=conn.sentence_id, sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT, content_detail=text))

    def feed_last():
        ensure_first()
        provider.tts_text_queue.put(TTSMessageDTO(
            sentence_id=conn.sentence_id, sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION, content_detail=""))

    def drain():
        while True:
            try:
                item = provider.tts_audio_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            stype = item[0]
            audio = item[1] if len(item) > 1 else None
            if isinstance(audio, (bytes, bytearray)) and len(audio) > 0:
                try:
                    send_audio(decoder.decode(bytes(audio), frame_samples))
                except Exception as error:
                    print(
                        "[tts-worker] opus_decode_error %s: %s"
                        % (type(error).__name__, error),
                        file=sys.stderr,
                        flush=True,
                    )
            if stype == SentenceType.FIRST:
                session_state["segments"] += 1
                send_ctrl({
                    "event": "segment_done",
                    "index": session_state["segments"],
                })
            if stype == SentenceType.LAST:
                send_ctrl({"event": "done"})

    await provider.open_audio_channels(conn)
    log_stage("channels_opened", initialized_at)
    threading.Thread(target=drain, daemon=True).start()
    send_ctrl({
        "event": "worker_ready",
        "sample_rate": conn.sample_rate,
    })
    log_stage("worker_ready_sent", initialized_at)
    ensure_connection = getattr(provider, "_ensure_connection", None)
    if callable(ensure_connection):
        async def preconnect():
            try:
                await ensure_connection()
                send_ctrl({"event": "cloud_ready"})
            except Exception as error:
                send_ctrl({
                    "event": "cloud_error",
                    "error": "%s: %s" % (
                        type(error).__name__,
                        error,
                    ),
                })

        asyncio.create_task(preconnect())

    async def forward_provider_errors():
        delivered = ""
        while not conn.stop_event.is_set():
            provider_error = str(
                getattr(provider, "last_error", "") or ""
            )
            if provider_error and provider_error != delivered:
                delivered = provider_error
                send_ctrl({
                    "event": "error",
                    "error": provider_error,
                })
            await asyncio.sleep(0.05)

    asyncio.create_task(forward_provider_errors())

    while True:
        cmd = await cmd_q.get()
        if cmd is None:
            break
        c = cmd.get("cmd")
        if c == "session":
            begin_session()
        elif c == "text" and cmd.get("text"):
            feed_text(cmd["text"])
        elif c == "finish":
            feed_last()

    conn.stop_event.set()
    try:
        await provider.close()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        initialized_at = time.perf_counter()
        start = json.loads(sys.stdin.readline())
        log_stage("command_received", initialized_at)
        block = start["block"]
        provider_type = block.get("type", start.get("provider"))
        provider = tts_factory.create_instance(
            provider_type,
            block,
            False,
        )
        log_stage("provider_created", initialized_at)
        asyncio.run(main(provider, provider_type, initialized_at))
    except Exception as e:
        import traceback
        send_ctrl({"event": "error", "error": "%s: %s" % (type(e).__name__, e)})
        traceback.print_exc(file=sys.stderr)
