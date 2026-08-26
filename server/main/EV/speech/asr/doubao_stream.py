"""Volcengine/Doubao streaming ASR adapter."""

import gzip
import json
import os
import queue
import re
import threading
import time
import uuid
from collections import deque

import websocket
from common.paths import ENV_PATH, MUSE_DIR


def _load_local_env():
    candidates = [
        str(ENV_PATH),
        str(MUSE_DIR.parents[2] / ".env"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name = name.strip()
                value = value.strip().strip("\"'")
                if name:
                    os.environ.setdefault(name, value)


_load_local_env()


class DoubaoStreamingASR:
    BIDIRECTIONAL_URL = (
        "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    )
    MULTILINGUAL_URL = (
        "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
    )
    URL = BIDIRECTIONAL_URL
    AUDIO_CHUNK_BYTES = 3200
    SUCCESS_CODES = {0, 1000, 20000000}
    # 预连接可复用的最大空闲时长(秒)：超过则视为可能被服务端回收，弃用重建
    WARM_MAX_AGE = 25.0
    # 预连接的重建阈值(秒)：warm socket 超过此龄就在后台换新，避免临用时已过期
    WARM_REFRESH_AGE = 15.0
    MAX_CONTEXT_HOTWORDS = 96
    _CONTEXT_STOPWORDS = {
        "about", "after", "again", "also", "and", "are", "close", "could",
        "find", "from", "have", "help", "into", "just", "like", "mean",
        "page", "please", "recently", "that", "the", "this", "what", "with",
        "would", "you", "your",
    }

    def __init__(
        self,
        api_key,
        resource_ids=None,
        hotwords=None,
        enable_multilingual=False,
        language=None,
        end_window_size=200,
    ):
        self.api_key = api_key
        self.resource_ids = resource_ids or [
            "volc.seedasr.sauc.duration",
            "volc.seedasr.sauc.concurrent",
            "volc.bigasr.sauc.duration",
            "volc.bigasr.sauc.concurrent",
        ]
        self.hotwords = list(hotwords or [])
        self.enable_multilingual = _as_bool(enable_multilingual)
        self.language = (
            str(language or "").strip() if self.enable_multilingual else ""
        )
        self.end_window_size = _positive_int(end_window_size, default=200)
        self.url = (
            self.MULTILINGUAL_URL
            if self.enable_multilingual
            else self.BIDIRECTIONAL_URL
        )
        self._recent_hotwords = deque(maxlen=self.MAX_CONTEXT_HOTWORDS)
        self.audio_queue = None
        self.worker = None
        self.socket = None
        self.done = threading.Event()
        self.final_response = threading.Event()
        self.finish_sent = threading.Event()
        self.server_endpoint = threading.Event()
        self.lock = threading.Lock()
        self.text = ""
        self.last_partial_at = 0.0
        self.definite_text = ""
        self.definite_version = 0
        self.definite_at = None
        self.error = ""
        self.metrics = {}
        # 预连接：空闲时后台先把 WS 握手做好(不发配置)，start() 直接复用，隐藏建连延迟
        self._warm_sock = None
        self._warm_resource = None
        self._warm_at = 0.0
        self._warm_lock = threading.Lock()
        self._warm_thread = None

    @classmethod
    def _terms_from_text(cls, text):
        """抽取最近对话里的领域词，不维护设备或产品名白名单。"""
        value = str(text or "").strip()
        if not value:
            return []
        terms = []
        latin = re.findall(r"[A-Za-z][A-Za-z0-9_.+#-]{1,31}", value)
        useful_latin = [
            word for word in latin
            if word.lower() not in cls._CONTEXT_STOPWORDS
            and (
                len(word) >= 4
                or any(char.isupper() for char in word[1:])
                or any(char.isdigit() for char in word)
            )
        ]
        for left, right in zip(useful_latin, useful_latin[1:]):
            terms.append(left + " " + right)
        terms.extend(useful_latin)
        terms.extend(re.findall(r"[\u4e00-\u9fff]{2,12}", value))
        return list(dict.fromkeys(terms))

    def remember_text(self, text):
        """把已通过音量/幻觉校验的上一轮文本作为下一轮 ASR 热词上下文。"""
        for term in self._terms_from_text(text):
            try:
                self._recent_hotwords.remove(term)
            except ValueError:
                pass
            self._recent_hotwords.append(term)

    def _context_hotwords(self):
        combined = list(self.hotwords) + list(self._recent_hotwords)
        cleaned = [str(word).strip() for word in combined if str(word).strip()]
        return list(dict.fromkeys(cleaned))[-self.MAX_CONTEXT_HOTWORDS:]

    @classmethod
    def from_env(cls, overrides=None):
        """用环境变量提供凭据，同时让智能体覆盖项控制识别模式。"""
        config = dict(overrides or {})
        configured_key = str(
            config.get("access_token") or config.get("api_key") or ""
        ).strip()
        if "你的" in configured_key:
            configured_key = ""
        # EV 直连接口使用 X-Api-Key；智能体里的 access_token 属于核心服务
        # 的另一套鉴权。环境变量有专用 API key 时必须优先，覆盖项只补缺。
        api_key = os.environ.get("VOLC_ASR_API_KEY", "").strip() or configured_key
        configured_resource = str(config.get("resource_id") or "").strip()
        resource_ids = (
            [configured_resource]
            if configured_resource
            else [
                item.strip()
                for item in os.environ.get(
                    "VOLC_ASR_RESOURCE_IDS",
                    "volc.seedasr.sauc.duration,volc.seedasr.sauc.concurrent,"
                    "volc.bigasr.sauc.duration,volc.bigasr.sauc.concurrent",
                ).split(",")
                if item.strip()
            ]
        )
        configured_hotwords = [
            item.strip()
            for item in os.environ.get(
                "VOLC_ASR_HOTWORDS",
                "",
            ).split(",")
            if item.strip()
        ]
        return cls(
            api_key,
            resource_ids=resource_ids,
            hotwords=list(dict.fromkeys(configured_hotwords)),
            enable_multilingual=config.get(
                "enable_multilingual",
                os.environ.get("VOLC_ASR_ENABLE_MULTILINGUAL", "false"),
            ),
            language=config.get(
                "language",
                os.environ.get("VOLC_ASR_LANGUAGE", ""),
            ),
            end_window_size=config.get(
                "end_window_size",
                os.environ.get("VOLC_ASR_END_WINDOW_SIZE", "200"),
            ),
        )

    @property
    def enabled(self):
        return bool(self.api_key)

    @property
    def mode(self):
        return "multilingual_nostream" if self.enable_multilingual else "bilingual_async"

    def config_signature(self):
        """用于检测同一供应商内的参数热更新；不会写入日志。"""
        return (
            self.api_key,
            tuple(self.resource_ids),
            tuple(self.hotwords),
            self.mode,
            self.language,
            self.end_window_size,
        )

    def start(self, frames=()):
        if not self.enabled:
            return False
        # 只收尾上一轮会话，保留 warm socket，供本轮 _connect_and_initialize 复用
        self.close(discard_warm=False)
        self.audio_queue = queue.Queue(maxsize=2000)
        self.done.clear()
        self.final_response.clear()
        self.finish_sent.clear()
        self.server_endpoint.clear()
        self.text = ""
        self.last_partial_at = 0.0
        self.definite_text = ""
        self.definite_version = 0
        self.definite_at = None
        self.error = ""
        self.metrics = {
            "started_at": time.perf_counter(),
            "audio_bytes": 0,
            "resource_id": "",
        }
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()
        for frame in frames:
            self.feed(frame)
        return True

    def feed(self, frame):
        if not frame or self.audio_queue is None or self.done.is_set():
            return
        try:
            self.audio_queue.put_nowait(bytes(frame))
            self.metrics["audio_bytes"] += len(frame)
        except queue.Full:
            self.error = "流式 ASR 音频队列已满"

    def has_server_endpoint(self):
        return self.server_endpoint.is_set()

    def partial_snapshot(self):
        """线程安全读取当前中间识别文本（边说边出，未定稿）。"""
        with self.lock:
            return {
                "text": self.text,
                "updated_at": self.last_partial_at or self.metrics.get("first_partial_at"),
            }

    def definite_snapshot(self):
        with self.lock:
            return {
                "text": self.definite_text,
                "version": self.definite_version,
                "at": self.definite_at,
            }

    def finish(self, timeout=8):
        if self.audio_queue is None:
            return "", {}
        self.metrics["finish_called_at"] = time.perf_counter()
        try:
            self.audio_queue.put(None, timeout=0.2)
        except queue.Full:
            self.error = "流式 ASR 无法提交结束帧"
        if not self.done.wait(timeout):
            self.error = self.error or "流式 ASR 等待最终结果超时"
            self._close_socket()
            self.done.wait(1)
        metrics = self._public_metrics()
        text = self.text.strip()
        self.audio_queue = None
        # 回合结束立刻预建下一轮，避免长 TTS 期间 warm 过期后冷建连
        self.preconnect()
        return text, metrics

    def close(self, discard_warm=True):
        if self.worker and self.worker.is_alive():
            if self.audio_queue is not None:
                try:
                    self.audio_queue.put_nowait(None)
                except queue.Full:
                    pass
            self._close_socket()
            self.done.wait(1)
        self.worker = None
        self.audio_queue = None
        if discard_warm:
            self._discard_warm()

    def _discard_warm(self):
        with self._warm_lock:
            sock = self._warm_sock
            self._warm_sock = None
            self._warm_resource = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _run(self):
        receiver = None
        try:
            self.socket = self._connect_and_initialize()
            receiver = threading.Thread(target=self._receive, daemon=True)
            receiver.start()

            pending = bytearray()
            while True:
                frame = self.audio_queue.get()
                if frame is None:
                    break
                pending.extend(frame)
                while len(pending) >= self.AUDIO_CHUNK_BYTES:
                    chunk = bytes(pending[:self.AUDIO_CHUNK_BYTES])
                    del pending[:self.AUDIO_CHUNK_BYTES]
                    self._send_audio(chunk, last=False)

            self.finish_sent.set()
            self._send_audio(bytes(pending), last=True)
            if not self.final_response.wait(6):
                raise TimeoutError("火山 ASR 未返回最终帧")
            self.metrics["completed_at"] = time.perf_counter()
        except Exception as error:
            self.error = str(error)
            self.metrics["completed_at"] = time.perf_counter()
        finally:
            self._close_socket()
            if receiver and receiver.is_alive():
                receiver.join(timeout=0.5)
            self.done.set()

    def _raw_connect(self, resource_id):
        """只做 WS 握手，不发配置。返回已连接的 socket。"""
        return websocket.create_connection(
            self.url,
            header=[
                "X-Api-Key: " + self.api_key,
                "X-Api-Resource-Id: " + resource_id,
                "X-Api-Connect-Id: " + str(uuid.uuid4()),
            ],
            timeout=6,
            enable_multithread=True,
        )

    def preconnect(self):
        """空闲时后台预建一个 WS 连接(仅握手)，隐藏 start() 时的建连延迟。非阻塞。
        已有较新的 warm 连接则跳过；旧到接近过期则在后台换新。"""
        if not self.enabled:
            return
        with self._warm_lock:
            if self._warm_thread is not None and self._warm_thread.is_alive():
                return
            if (
                self._warm_sock is not None
                and (time.monotonic() - self._warm_at) < self.WARM_REFRESH_AGE
            ):
                return
            self._warm_thread = threading.Thread(target=self._do_warm, daemon=True)
            self._warm_thread.start()

    def _do_warm(self):
        resource_id = self.resource_ids[0]
        try:
            sock = self._raw_connect(resource_id)
        except Exception:
            return
        old = None
        with self._warm_lock:
            old = self._warm_sock
            self._warm_sock = sock
            self._warm_resource = resource_id
            self._warm_at = time.monotonic()
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def _take_warm(self):
        """取走当前 warm 连接(若存在且未过期)。返回 (sock, resource_id) 或 (None, None)。"""
        with self._warm_lock:
            sock = self._warm_sock
            resource_id = self._warm_resource
            age = time.monotonic() - self._warm_at
            self._warm_sock = None
            self._warm_resource = None
        if sock is None:
            return None, None
        if age >= self.WARM_MAX_AGE:
            try:
                sock.close()
            except Exception:
                pass
            return None, None
        return sock, resource_id

    def _connect_and_initialize(self):
        # 优先复用预建好的 warm 连接，省掉建连往返
        warm_sock, warm_resource = self._take_warm()
        if warm_sock is not None:
            try:
                self.socket = warm_sock
                self.metrics["resource_id"] = warm_resource
                self.metrics["connected_at"] = time.perf_counter()
                self.metrics["warm_reused"] = True
                self._send_initial_request()
                initial = self._parse_response(warm_sock.recv())
                self._raise_for_error(initial)
                self.metrics["initialized_at"] = time.perf_counter()
                self.preconnect()  # 顺手预热下一个
                return warm_sock
            except Exception:
                # warm 连接失效(可能已被服务端回收) → 落回常规建连
                try:
                    warm_sock.close()
                except Exception:
                    pass
                self.socket = None

        errors = []
        for resource_id in self.resource_ids:
            connect_id = str(uuid.uuid4())
            headers = [
                "X-Api-Key: " + self.api_key,
                "X-Api-Resource-Id: " + resource_id,
                "X-Api-Connect-Id: " + connect_id,
            ]
            try:
                sock = websocket.create_connection(
                    self.url,
                    header=headers,
                    timeout=6,
                    enable_multithread=True,
                )
                self.socket = sock
                self.metrics["resource_id"] = resource_id
                self.metrics["connected_at"] = time.perf_counter()
                self._send_initial_request()
                initial = self._parse_response(sock.recv())
                self._raise_for_error(initial)
                self.metrics["initialized_at"] = time.perf_counter()
                self.preconnect()  # 建连成功后预热下一个
                return sock
            except Exception as error:
                errors.append("%s: %s" % (resource_id, _error_summary(error)))
                self._close_socket()
        raise ConnectionError("；".join(errors))

    def _send_initial_request(self):
        context_hotwords = self._context_hotwords()
        hotword_context = (
            json.dumps(
                {"hotwords": [{"word": word} for word in context_hotwords]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if context_hotwords
            else ""
        )
        payload = {
            "user": {"uid": "camera-voice"},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": "bigmodel",
                "show_utterances": True,
                "result_type": "full",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": False,
                "end_window_size": self.end_window_size,
                "force_to_speech_time": 600,
            },
        }
        if self.enable_multilingual:
            if self.language:
                payload["audio"]["language"] = self.language
        else:
            # 二遍非流式识别是 bigmodel_async 的能力；nostream 不传此开关。
            payload["request"]["enable_nonstream"] = True
        if hotword_context:
            # 火山接口把直传热词 JSON 字符串放在 request.context；corpus
            # 只用于 boosting_table_id。放进 corpus.context 会被服务端忽略。
            payload["request"]["context"] = hotword_context
        compressed = gzip.compress(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        packet = self._header(0x01, 0x00, 0x01, 0x01)
        packet.extend(len(compressed).to_bytes(4, "big"))
        packet.extend(compressed)
        self.socket.send_binary(bytes(packet))

    def _send_audio(self, audio, last):
        compressed = gzip.compress(audio)
        packet = self._header(0x02, 0x02 if last else 0x00, 0x00, 0x01)
        packet.extend(len(compressed).to_bytes(4, "big"))
        packet.extend(compressed)
        self.socket.send_binary(bytes(packet))

    def _receive(self):
        while self.socket:
            try:
                response = self.socket.recv()
                if not isinstance(response, bytes):
                    continue
                parsed = self._parse_response(response)
                self._raise_for_error(parsed)
                payload = parsed.get("payload") or {}
                result = payload.get("result") or {}
                text = result.get("text") or ""
                if text:
                    with self.lock:
                        self.text = text
                        self.last_partial_at = time.time()
                    if "first_partial_at" not in self.metrics:
                        self.metrics["first_partial_at"] = time.perf_counter()
                utterances = result.get("utterances") or []
                latest_utterance = next(
                    (
                        utterance
                        for utterance in reversed(utterances)
                        if isinstance(utterance, dict)
                    ),
                    None,
                )
                latest_definite = (
                    latest_utterance
                    and latest_utterance.get("definite") in (True, 1, "true")
                )
                if latest_definite:
                    definite_at = time.perf_counter()
                    if text:
                        with self.lock:
                            if text != self.definite_text:
                                self.definite_text = text
                                self.definite_version += 1
                                self.definite_at = definite_at
                    if not self.server_endpoint.is_set():
                        self.metrics["server_endpoint_at"] = definite_at
                    self.server_endpoint.set()
                elif latest_utterance is not None:
                    self.server_endpoint.clear()
                    self.metrics.pop("server_endpoint_at", None)
                if self.finish_sent.is_set() and parsed.get("flags") in (0x02, 0x03):
                    self.final_response.set()
                    return
            except Exception as error:
                if not self.done.is_set():
                    self.error = str(error)
                self.final_response.set()
                return

    @staticmethod
    def _header(message_type, flags, serialization, compression):
        return bytearray([
            0x11,
            (message_type << 4) | flags,
            (serialization << 4) | compression,
            0x00,
        ])

    @classmethod
    def _parse_response(cls, response):
        if len(response) < 8:
            raise ValueError("火山 ASR 响应过短")
        header_size = (response[0] & 0x0F) * 4
        message_type = response[1] >> 4
        flags = response[1] & 0x0F
        serialization = response[2] >> 4
        compression = response[2] & 0x0F
        offset = header_size
        parsed = {"message_type": message_type, "flags": flags}

        if message_type == 0x0F:
            parsed["code"] = int.from_bytes(response[offset:offset + 4], "big")
            offset += 4
        elif message_type in (0x09, 0x0B) and flags in (0x01, 0x03):
            parsed["sequence"] = int.from_bytes(
                response[offset:offset + 4], "big", signed=True
            )
            offset += 4

        if len(response) < offset + 4:
            return parsed
        payload_size = int.from_bytes(response[offset:offset + 4], "big")
        offset += 4
        payload = response[offset:offset + payload_size]
        if compression == 0x01 and payload:
            payload = gzip.decompress(payload)
        if serialization == 0x01 and payload:
            parsed["payload"] = json.loads(payload.decode("utf-8"))
        elif payload:
            parsed["payload"] = payload.decode("utf-8", errors="replace")
        return parsed

    @classmethod
    def _raise_for_error(cls, parsed):
        code = parsed.get("code")
        payload = parsed.get("payload")
        if isinstance(payload, dict):
            code = payload.get("code", code)
            message = payload.get("message") or payload.get("error") or ""
        else:
            message = str(payload or "")
        if code is not None and code not in cls.SUCCESS_CODES:
            raise RuntimeError("火山 ASR 错误 %s: %s" % (code, message))

    def _close_socket(self):
        sock = self.socket
        self.socket = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _public_metrics(self):
        started_at = self.metrics.get("started_at")
        connected_at = self.metrics.get("connected_at")
        initialized_at = self.metrics.get("initialized_at")
        finish_called_at = self.metrics.get("finish_called_at")
        first_partial_at = self.metrics.get("first_partial_at")
        server_endpoint_at = self.metrics.get("server_endpoint_at")
        completed_at = self.metrics.get("completed_at") or time.perf_counter()
        return {
            "provider": "doubao_stream",
            "resource_id": self.metrics.get("resource_id", ""),
            "mode": self.mode,
            "language": self.language,
            "warm_reused": bool(self.metrics.get("warm_reused")),
            "connect_ms": _elapsed_ms(started_at, connected_at),
            "initialize_ms": _elapsed_ms(connected_at, initialized_at),
            "first_partial_ms": _elapsed_ms(started_at, first_partial_at),
            "server_endpoint_ms": _elapsed_ms(started_at, server_endpoint_at),
            "server_endpoint_lead_ms": _elapsed_ms(
                server_endpoint_at,
                finish_called_at,
            ),
            "final_after_vad_ms": _elapsed_ms(finish_called_at, completed_at),
            "total_stream_ms": _elapsed_ms(started_at, completed_at),
            "audio_ms": round(self.metrics.get("audio_bytes", 0) / 32.0, 1),
            "error": self.error,
        }


def _elapsed_ms(started_at, ended_at):
    if started_at is None or ended_at is None:
        return 0.0
    return round(max(0.0, ended_at - started_at) * 1000, 1)


def _error_summary(error):
    message = str(error)
    start = message.find('{"error":')
    if start >= 0:
        try:
            payload, _end = json.JSONDecoder().raw_decode(message[start:])
            if isinstance(payload, dict) and payload.get("error"):
                return str(payload["error"])
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return message[:240]


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value, default):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
