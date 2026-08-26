# -*- coding: utf-8 -*-
"""Streaming TTS bridge using a persistent provider worker."""

import asyncio
import atexit
import json
import os
import queue
import socket
import struct
import subprocess
import threading
import time

from common.paths import SERVER_DIR, TMP_DIR

_ROOT = os.path.dirname(os.path.abspath(__file__))
_SERVER = str(SERVER_DIR)
_SCRIPTS = os.path.join(
    _SERVER, ".venv", "Scripts" if os.name == "nt" else "bin"
)
_PYTHON = os.path.join(
    _SCRIPTS, "python.exe" if os.name == "nt" else "python"
)
if not os.path.isfile(_PYTHON):
    import sys as _sys
    _PYTHON = _sys.executable
_WORKER_SCRIPT = os.path.join(_ROOT, "worker.py")
_ERROR_LOG = str(TMP_DIR / "tts_worker.err")
_WORKERS = {}
_WORKERS_LOCK = threading.Lock()
_SESSION_LOCK_TIMEOUT = float(
    os.environ.get("MUSE_TTS_SESSION_LOCK_TIMEOUT", "7")
)
_WORKER_IDLE_TIMEOUT = float(
    os.environ.get("MUSE_TTS_WORKER_IDLE_TIMEOUT", "12")
)
_WORKER_POLL_INTERVAL = 0.25


def is_streaming_type(ttype):
    return ttype in (
        "huoshan_double_stream",
        "aliyun_stream",
        "alibl_stream",
        "index_stream",
        "xunfei_stream",
        "minimax_httpstream",
    )


def _config_key(provider_name, block):
    return json.dumps(
        {
            "provider": provider_name,
            "block": block,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


class _WorkerProcess:
    def __init__(self, provider_name, block):
        self.provider_name = provider_name
        self.block = block
        self.process = None
        self.error_log = None
        self.frame_socket = None
        self.reader_thread = None
        self.write_lock = threading.Lock()
        self.route_lock = threading.Lock()
        self.session_lock = threading.Lock()
        self.active_queue = None
        self.sample_rate = 24000
        self.started_at = time.perf_counter()
        self.closed = False
        try:
            self._spawn()
        except Exception:
            self.close()
            raise

    @property
    def alive(self):
        return not self.closed and self.frame_socket is not None

    def _spawn(self):
        environment = os.environ.copy()
        environment["PATH"] = (
            _SCRIPTS + os.pathsep + environment.get("PATH", "")
        )
        self.error_log = open(_ERROR_LOG, "ab")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        environment["MUSE_TTS_FRAME_PORT"] = str(
            listener.getsockname()[1]
        )
        try:
            self.process = subprocess.Popen(
                [_PYTHON, "-X", "utf8", "-u", _WORKER_SCRIPT],
                cwd=_SERVER,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=self.error_log,
                stderr=self.error_log,
                bufsize=0,
            )
            self.frame_socket, _ = listener.accept()
            self.frame_socket.settimeout(None)
        finally:
            listener.close()
        startup_queue = queue.Queue()
        with self.route_lock:
            self.active_queue = startup_queue
        self.reader_thread = threading.Thread(
            target=self._reader,
            name="generic-tts-worker-reader",
            daemon=True,
        )
        self.reader_thread.start()
        self.send({
            "cmd": "start",
            "provider": self.provider_name,
            "block": self.block,
        })
        event = self._wait_control(
            startup_queue,
            expected="worker_ready",
            timeout=60,
        )
        self.sample_rate = int(event.get("sample_rate") or 24000)
        with self.route_lock:
            if self.active_queue is startup_queue:
                self.active_queue = None

    def send(self, value):
        if not self.alive:
            raise RuntimeError("TTS worker 已退出")
        payload = (
            json.dumps(value, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        with self.write_lock:
            self.process.stdin.write(payload)
            self.process.stdin.flush()

    def begin_session(self, timeout=5):
        session_queue = queue.Queue()
        with self.route_lock:
            self.active_queue = session_queue
        started_at = time.perf_counter()
        self.send({"cmd": "session"})
        event = self._wait_control(
            session_queue,
            expected="ready",
            timeout=timeout,
        )
        event["setup_ms"] = round(
            (time.perf_counter() - started_at) * 1000,
            1,
        )
        event["reconnected"] = False
        return session_queue, event

    def end_session(self, session_queue):
        with self.route_lock:
            if self.active_queue is session_queue:
                self.active_queue = None

    def _reader(self):
        failure = None
        try:
            while True:
                header = self._read_exact(5)
                if not header:
                    break
                frame_type = header[0]
                length = struct.unpack(">I", header[1:5])[0]
                payload = self._read_exact(length) if length else b""
                if payload is None:
                    break
                if frame_type == 1:
                    item = ("audio", payload)
                else:
                    try:
                        item = (
                            "control",
                            json.loads(payload.decode("utf-8")),
                        )
                    except Exception:
                        continue
                with self.route_lock:
                    target = self.active_queue
                if target is not None:
                    target.put(item)
        except Exception as error:
            failure = str(error)
        finally:
            self.closed = True
            message = failure or "TTS worker 管道已关闭"
            with self.route_lock:
                target = self.active_queue
            if target is not None:
                target.put((
                    "control",
                    {"event": "error", "error": message},
                ))

    def _read_exact(self, length):
        data = b""
        while len(data) < length:
            chunk = self.frame_socket.recv(length - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    @staticmethod
    def _wait_control(event_queue, expected, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "等待 TTS worker %s 超时" % expected
                )
            try:
                kind, value = event_queue.get(timeout=remaining)
            except queue.Empty as error:
                raise TimeoutError(
                    "等待 TTS worker %s 超时" % expected
                ) from error
            if kind != "control":
                continue
            event = value.get("event")
            if event == "error":
                raise RuntimeError(
                    value.get("error") or "TTS worker 初始化失败"
                )
            if event == expected:
                return value

    def close(self):
        self.closed = True
        process = self.process
        self.process = None
        if process is not None:
            try:
                process.stdin.close()
            except Exception:
                pass
            try:
                process.wait(timeout=2)
            except Exception:
                try:
                    if os.name == "nt":
                        subprocess.run(
                            [
                                "taskkill",
                                "/PID",
                                str(process.pid),
                                "/T",
                                "/F",
                            ],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                            check=False,
                        )
                    else:
                        process.terminate()
                except Exception:
                    pass
        frame_socket = self.frame_socket
        self.frame_socket = None
        if frame_socket is not None:
            try:
                frame_socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                frame_socket.close()
            except Exception:
                pass
        if self.error_log is not None:
            try:
                self.error_log.close()
            except Exception:
                pass
            self.error_log = None


def _get_worker(provider_name, block):
    key = _config_key(provider_name, block)
    with _WORKERS_LOCK:
        worker = _WORKERS.get(key)
        if worker is not None and worker.alive:
            return worker, True
        if worker is not None:
            worker.close()
            _WORKERS.pop(key, None)
        for old_key, old_worker in list(_WORKERS.items()):
            if old_worker.provider_name == provider_name:
                old_worker.close()
                _WORKERS.pop(old_key, None)
        worker = _WorkerProcess(provider_name, block)
        _WORKERS[key] = worker
        return worker, False


def _discard_worker(provider_name, block, worker):
    key = _config_key(provider_name, block)
    with _WORKERS_LOCK:
        if _WORKERS.get(key) is worker:
            _WORKERS.pop(key, None)
    worker.close()


def prewarm_generic(provider_name, block):
    started_at = time.perf_counter()
    worker, reused = _get_worker(provider_name, block)
    return {
        "reused": reused,
        "sample_rate": worker.sample_rate,
        "setup_ms": round(
            (time.perf_counter() - started_at) * 1000,
            1,
        ),
    }


async def run_generic_duplex(
    websocket,
    provider_name,
    block,
    send_json,
    send_bytes,
    recv_json,
):
    del websocket
    worker, _ = await asyncio.to_thread(
        _get_worker,
        provider_name,
        block,
    )
    lock_acquired = await asyncio.to_thread(
        worker.session_lock.acquire,
        True,
        _SESSION_LOCK_TIMEOUT,
    )
    if not lock_acquired:
        await asyncio.to_thread(
            _discard_worker,
            provider_name,
            block,
            worker,
        )
        raise TimeoutError("等待上一轮 TTS 会话释放超时，已重建连接")
    session_queue = None
    finish_sent = False
    pump_task = None
    completed_normally = False
    activity = {
        "pending_since": None,
        "last_worker_event": None,
        "submitted_segments": 0,
        "completed_segments": 0,
    }
    try:
        session_queue, ready = await asyncio.to_thread(
            worker.begin_session,
        )
        await send_json(ready)

        async def pump_input():
            nonlocal finish_sent
            while True:
                event = await recv_json()
                if event is None:
                    session_queue.put((
                        "control",
                        {"event": "disconnect"},
                    ))
                    return
                event_name = event.get("event")
                if event_name == "text" and event.get("text"):
                    worker.send({
                        "cmd": "text",
                        "text": event["text"],
                    })
                    if (
                        activity["submitted_segments"]
                        <= activity["completed_segments"]
                    ):
                        now = time.monotonic()
                        activity["pending_since"] = now
                        activity["last_worker_event"] = now
                    activity["submitted_segments"] += 1
                elif event_name == "finish":
                    worker.send({"cmd": "finish"})
                    finish_sent = True
                    if activity["pending_since"] is None:
                        now = time.monotonic()
                        activity["pending_since"] = now
                        activity["last_worker_event"] = now
                    return

        pump_task = asyncio.create_task(pump_input())
        while True:
            try:
                kind, value = await asyncio.to_thread(
                    session_queue.get,
                    True,
                    _WORKER_POLL_INTERVAL,
                )
            except queue.Empty:
                pending_since = activity["pending_since"]
                last_event = activity["last_worker_event"]
                # 只有客户端已发 finish（本轮所有段都已下发）后，才允许因
                # worker 长时间无音频而切断——此时没有更多段会来，若 worker
                # 卡住需要兜底重建。finish 之前 LLM 可能在思考/搜索，段与段
                # 之间天然有空隙（可达十几秒），绝不能因 idle 掐断中间。
                if (
                    finish_sent
                    and pending_since is not None
                    and last_event is not None
                    and time.monotonic() - last_event
                    >= _WORKER_IDLE_TIMEOUT
                ):
                    raise TimeoutError(
                        "TTS worker 连续 %.1fs 未返回音频，已重建连接"
                        % _WORKER_IDLE_TIMEOUT
                    )
                continue
            activity["last_worker_event"] = time.monotonic()
            if kind == "audio":
                await send_bytes(value)
                continue
            event_name = value.get("event")
            if event_name == "disconnect":
                return
            await send_json(value)
            if event_name == "segment_done":
                activity["completed_segments"] = max(
                    activity["completed_segments"],
                    int(value.get("index") or 0),
                )
                if (
                    not finish_sent
                    and activity["completed_segments"]
                    >= activity["submitted_segments"]
                ):
                    activity["pending_since"] = None
                    activity["last_worker_event"] = None
            if event_name == "done":
                completed_normally = True
                return
            if event_name == "error":
                return
    finally:
        if pump_task is not None:
            pump_task.cancel()
            try:
                await pump_task
            except BaseException:
                pass
        if session_queue is not None:
            if completed_normally and not finish_sent and worker.alive:
                try:
                    worker.send({"cmd": "finish"})
                except Exception:
                    pass
            worker.end_session(session_queue)
        worker.session_lock.release()
        if not completed_normally:
            await asyncio.to_thread(
                _discard_worker,
                provider_name,
                block,
                worker,
            )


def close_all_workers():
    with _WORKERS_LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for worker in workers:
        worker.close()


atexit.register(close_all_workers)
