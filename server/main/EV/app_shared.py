# -*- coding: utf-8 -*-
"""app 级共享常量与辅助函数。

app.py 拆 APIRouter 时，各路由模块（routes_admin 等）与 app.py 共用同一批
全局：路径常量、音色清单、provider 配置状态判定、头像目录等。全部收在这里，
路由模块和 app.py 都从本模块 import，避免循环依赖。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import re
import socket
import threading
import time
import uuid
from functools import lru_cache
from urllib.parse import urlparse

import httpx
from fastapi import Request

from common.paths import (
    DIGITAL_HUMAN_DIR,
    SERVER_DIR,
    VENDOR_DIR,
)
from control_plane import database as db

PLUGINS_DIR = SERVER_DIR / "plugins_func" / "functions"
DH_DIR = DIGITAL_HUMAN_DIR
ESP_CLAW_FLASH_DIR = VENDOR_DIR / "esp-claw-flash"
AVATAR_VISUALIZER = "visualizer"

EDGE_VOICES = [
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女 · 温柔"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女 · 活泼"),
    ("zh-CN-YunxiNeural", "云希 · 男 · 阳光"),
    ("zh-CN-YunyangNeural", "云扬 · 男 · 播音"),
    ("zh-CN-YunjianNeural", "云健 · 男 · 浑厚"),
    ("zh-CN-XiaomengNeural", "晓梦 · 女 · 甜美"),
    ("zh-CN-liaoning-XiaobeiNeural", "晓北 · 女 · 东北话"),
    ("zh-HK-HiuGaaiNeural", "曉佳 · 女 · 粤语"),
]
MINIMAX_VOICES = [
    ("female-shaonv", "少女 · 女 · 清甜"), ("female-yujie", "御姐 · 女 · 成熟"),
    ("female-chengshu", "成熟女性 · 女 · 知性"), ("female-tianmei", "甜美女性 · 女"),
    ("presenter_female", "女主持 · 女"), ("audiobook_female_1", "有声书女声 · 女"),
    ("diadia_xuemei", "嗲嗲学妹 · 女"), ("wumei_yujie", "妩媚御姐 · 女"),
    ("male-qn-qingse", "青涩青年 · 男"), ("male-qn-jingying", "精英青年 · 男"),
    ("male-qn-badao", "霸道青年 · 男"), ("presenter_male", "男主持 · 男"),
    ("audiobook_male_1", "有声书男声 · 男"), ("junlang_nanyou", "俊朗男友 · 男"),
    ("lengdan_xiongzhang", "冷淡学长 · 男"),
]


def _tcp_open(host, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex((host, int(port))) == 0
    except Exception:
        return False


def _external_base_url(request: Request) -> str:
    """对外访问根 URL（Tailscale HTTPS / 反向代理 / 局域网直连）。"""
    try:
        h = request.headers
        host = (h.get("x-forwarded-host") or h.get("host") or "").split(",")[0].strip()
        proto = (h.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if not proto and host.endswith(".ts.net"):
            proto = "https"
        if not proto:
            proto = request.url.scheme or "http"
        if host:
            return "%s://%s" % (proto, host)
    except Exception:
        pass
    return str(request.base_url).rstrip("/")


# ==================== 头像目录 ====================
def _live2d_avatars():
    items = []
    res = DH_DIR / "resources"
    if not res.exists():
        return items
    for d in sorted(res.iterdir()):
        if not d.is_dir():
            continue
        m = sorted((d / "runtime").glob("*.model3.json"))
        if m:
            items.append({
                "name": d.name,
                "label": "%s · Live2D" % d.name,
                "type": "live2d",
                "model": "/avatar-res/%s/runtime/%s" % (d.name, m[0].name),
            })
    return items


def _avatar_catalog():
    return [{"name": AVATAR_VISUALIZER, "label": "声波可视化（默认）", "type": "visualizer", "model": ""}] + _live2d_avatars()


def _resolve_avatar_model(name):
    if not name or name in (AVATAR_VISUALIZER, "default", "sound_visualizer"):
        return ""
    for item in _live2d_avatars():
        if item["name"] == name:
            return item["model"]
    return ""


# ==================== provider 配置状态判定 ====================
_CREDENTIAL_FIELDS = {
    "api_key",
    "access_token",
    "token",
    "secret_key",
    "secret_id",
    "api_secret",
    "access_key_id",
    "access_key_secret",
    "personal_access_token",
}
_LOCAL_PROVIDER_TYPES = {
    "edge",
    "fun_local",
    "sherpa_onnx_local",
    "vosk",
    "silero",
    "nomem",
    "mem_local_short",
    "nointent",
    "intent_llm",
    "function_call",
}
_PLACEHOLDER_MARKERS = (
    "你的",
    "请替换",
    "待填写",
    "填入",
    "placeholder",
    "changeme",
    "your-api",
    "your_api",
    "sk-xxx",
)


def _configured_text(value):
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return not any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _configured_credential(value):
    text = str(value or "").strip()
    return (
        _configured_text(text)
        and len(text) >= 8
        and text.isascii()
        and not any(char.isspace() for char in text)
    )


def _provider_config_state(module_type, provider, overrides=None, provider_configs=None):
    from control_plane import database as db

    catalog = db.provider_catalog()
    base = dict((catalog.get(module_type) or {}).get(provider) or {})
    profiles = provider_configs if provider_configs is not None else db.get_provider_configs()
    profile = dict((profiles.get(module_type) or {}).get(provider) or {})
    merged = {**base, **profile, **(overrides or {})}
    provider_type = str(merged.get("type") or provider or "").lower()
    missing = []

    def require(field, credential=False):
        checker = _configured_credential if credential else _configured_text
        if not checker(merged.get(field)):
            missing.append(field)

    def require_one(label, alternatives):
        for fields in alternatives:
            if all(
                (_configured_credential(merged.get(field))
                 if field in _CREDENTIAL_FIELDS
                 else _configured_text(merged.get(field)))
                for field in fields
            ):
                return
        missing.append(label)

    if provider_type in _LOCAL_PROVIDER_TYPES:
        pass
    elif module_type in ("LLM", "VLLM") and provider_type == "openai":
        require("api_key", credential=True)
        require("model_name")
        require_one("url/base_url", (("url",), ("base_url",)))
    elif provider_type in ("doubao", "doubao_stream"):
        require("appid")
        require("access_token", credential=True)
    elif provider_type == "huoshan_double_stream":
        if str(merged.get("api_key") or "").strip():
            require("api_key", credential=True)
        else:
            require("appid")
            require("access_token", credential=True)
    elif provider_type == "minimax_httpstream":
        require("group_id")
        require("api_key", credential=True)
    elif provider_type in ("aliyun", "aliyun_stream"):
        require("appkey")
        require_one(
            "token 或 access_key_id/access_key_secret",
            (("token",), ("access_key_id", "access_key_secret")),
        )
    elif provider_type in ("tencent",):
        require("appid")
        require("secret_id", credential=True)
        require("secret_key", credential=True)
    elif provider_type in ("xunfei_stream",):
        require("app_id")
        require("api_key", credential=True)
        require("api_secret", credential=True)
    else:
        credential_fields = [
            field for field in _CREDENTIAL_FIELDS
            if field in base or field in profile or field in (overrides or {})
        ]
        for field in credential_fields:
            require(field, credential=True)

    configured = not missing
    return {
        "configured": configured,
        "state": "configured" if configured else "unconfigured",
        "missing": missing,
        "detail": "已配置" if configured else "请先配置：" + "、".join(missing),
    }


def _provider_status_catalog(catalog, provider_configs):
    return {
        module_type: {
            provider: _provider_config_state(
                module_type,
                provider,
                provider_configs=provider_configs,
            )
            for provider in providers
        }
        for module_type, providers in catalog.items()
    }


# ==================== LLM 客户端 ====================
@lru_cache(maxsize=8)
def _openai_client(base_url, api_key):
    from openai import OpenAI
    # keepalive 不宜过长：上游 LB 常在 ~60s 掐空闲连接，180s 易踩到半死连接
    http_client = httpx.Client(
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=45.0,
        ),
        timeout=httpx.Timeout(60.0, connect=8.0),
    )
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        http_client=http_client,
        max_retries=0,
    )


# ==================== TTS 共享件 ====================
def _resolved_tts(provider, overrides):
    blk = dict(db.provider_catalog().get("TTS", {}).get(provider, {}) or {})
    profile = (
        db.get_provider_configs().get("TTS", {}).get(provider, {})
        or {}
    )
    blk.update(profile)
    if isinstance(overrides, dict):
        blk.update({k: v for k, v in overrides.items() if v is not None})
    return blk


_TTS_BRACKET_RE = re.compile(
    r"（([^（）]{1,24})）|\(([^()]{1,24})\)|"
    r"【([^【】]{1,24})】|\[([^\[\]]{1,24})\]"
)
_TTS_STAGE_CUE_RE = re.compile(
    r"(笑|叹|哭|抽泣|咳|清嗓|吸气|呼气|喘气|"
    r"轻声|低声|小声|温柔地|开心地|难过地|生气地|惊讶地|无奈地|"
    r"沉默|停顿|想了想|点头|摇头|皱眉|眨眼)"
)
_TTS_28_TAGS = {
    "laughs", "chuckle", "coughs", "clear-throat", "groans", "breath",
    "pant", "inhale", "exhale", "gasps", "sniffs", "sighs", "snorts",
    "burps", "lip-smacking", "humming", "hissing", "emm", "whistles",
    "sneezes", "crying", "applause",
}


def _normalize_tts_text(text, model=""):
    supports_tags = str(model or "").lower().startswith("speech-2.8-")

    def replace(match):
        body = next(
            (group for group in match.groups() if group is not None),
            "",
        ).strip()
        lowered = body.lower()
        if lowered in _TTS_28_TAGS:
            return "(%s)" % lowered if supports_tags else ""
        if not _TTS_STAGE_CUE_RE.search(body):
            return match.group(0)
        if not supports_tags:
            return ""
        if re.search(r"(大笑|笑出声|哈哈)", body):
            return "(laughs)"
        if re.search(r"(笑|微笑|轻笑)", body):
            return "(chuckle)"
        if "叹" in body:
            return "(sighs)"
        if re.search(r"(哭|抽泣)", body):
            return "(crying)"
        if "清嗓" in body:
            return "(clear-throat)"
        if "咳" in body:
            return "(coughs)"
        if "吸气" in body:
            return "(inhale)"
        if "呼气" in body:
            return "(exhale)"
        if "喘气" in body:
            return "(pant)"
        return ""

    normalized = _TTS_BRACKET_RE.sub(replace, str(text or ""))
    normalized = re.sub(r"\s+([，。！？!?；;：:,])", r"\1", normalized)
    return normalized.strip()


class _MinimaxTTSWebSocket:
    def __init__(self):
        self.lock = threading.RLock()
        self.ws = None
        self.config_key = None
        self.last_activity = 0.0

    @staticmethod
    def _settings(blk):
        voice_setting = {
            "voice_id": "female-shaonv",
            "speed": 1,
            "vol": 1,
            "pitch": 0,
        }
        voice_setting.update(blk.get("voice_setting") or {})
        voice_id = blk.get("private_voice") or blk.get("voice_id")
        if voice_id:
            voice_setting["voice_id"] = voice_id
        audio_setting = {
            "sample_rate": 24000,
            "bitrate": 128000,
            "format": "pcm",
            "channel": 1,
        }
        audio_setting.update(blk.get("audio_setting") or {})
        audio_setting["format"] = "pcm"
        audio_setting["channel"] = 1
        return voice_setting, audio_setting

    def _key(self, blk):
        voice_setting, audio_setting = self._settings(blk)
        return json.dumps({
            "api_key": blk.get("api_key"),
            "model": blk.get("model"),
            "voice_setting": voice_setting,
            "audio_setting": audio_setting,
            "language_boost": blk.get("language_boost"),
            "pronunciation_dict": blk.get("pronunciation_dict"),
        }, sort_keys=True, ensure_ascii=False)

    @contextlib.contextmanager
    def _session_lock(self, timeout=3):
        acquired = self.lock.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError("MiniMax TTS 会话繁忙，改走 HTTP")
        try:
            yield
        finally:
            self.lock.release()

    def _close_locked(self):
        if self.ws is not None:
            try:
                self.ws.send(json.dumps({"event": "task_finish"}))
            except Exception:
                pass
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None
        self.config_key = None
        self.last_activity = 0.0

    def _connect_locked(self, blk):
        import websocket
        self._close_locked()
        api_key = blk.get("api_key")
        self.ws = websocket.create_connection(
            "wss://api.minimaxi.com/ws/v1/t2a_v2",
            header=["Authorization: Bearer %s" % api_key],
            timeout=20,
            enable_multithread=True,
        )
        connected = json.loads(self.ws.recv())
        if connected.get("event") != "connected_success":
            raise RuntimeError("MiniMax WebSocket 连接失败: %s" % connected)
        voice_setting, audio_setting = self._settings(blk)
        start_payload = {
            "event": "task_start",
            "model": blk.get("model") or "speech-01-turbo",
            "language_boost": blk.get("language_boost") or "Chinese",
            "voice_setting": voice_setting,
            "audio_setting": audio_setting,
        }
        if blk.get("pronunciation_dict"):
            start_payload["pronunciation_dict"] = blk["pronunciation_dict"]
        self.ws.send(json.dumps(start_payload, ensure_ascii=False))
        started = json.loads(self.ws.recv())
        if started.get("event") != "task_started":
            raise RuntimeError("MiniMax WebSocket 任务启动失败: %s" % started)
        self.config_key = self._key(blk)
        self.last_activity = time.monotonic()

    def _ensure_locked(self, blk):
        stale = time.monotonic() - self.last_activity > 90
        if self.ws is None or self.config_key != self._key(blk) or stale:
            self._connect_locked(blk)
            return True
        return False

    def prewarm(self, blk):
        started_at = time.perf_counter()
        with self._session_lock(timeout=0.25):
            reconnected = self._ensure_locked(blk)
        return {
            "reconnected": reconnected,
            "setup_ms": round((time.perf_counter() - started_at) * 1000, 1),
        }

    def stream(self, blk, text):
        import websocket

        text = _normalize_tts_text(text, blk.get("model"))
        if not text:
            return
        with self._session_lock(timeout=0.25):
            for attempt in range(2):
                emitted = False
                try:
                    self._ensure_locked(blk)
                    self.ws.settimeout(0.25)
                    self.ws.send(json.dumps(
                        {"event": "task_continue", "text": text},
                        ensure_ascii=False,
                    ))
                    last_provider_event_at = time.monotonic()
                    while True:
                        try:
                            raw_event = self.ws.recv()
                        except websocket.WebSocketTimeoutException:
                            if (
                                time.monotonic() - last_provider_event_at
                                > 15
                            ):
                                raise TimeoutError(
                                    "MiniMax TTS 15 秒未返回音频"
                                )
                            continue
                        last_provider_event_at = time.monotonic()
                        event = json.loads(raw_event)
                        base_response = event.get("base_resp") or {}
                        if base_response.get("status_code", 0) != 0:
                            raise RuntimeError(
                                base_response.get("status_msg") or "MiniMax TTS 失败"
                            )
                        audio_hex = (event.get("data") or {}).get("audio")
                        if audio_hex:
                            emitted = True
                            yield bytes.fromhex(audio_hex)
                        if event.get("is_final"):
                            self.last_activity = time.monotonic()
                            self.ws.settimeout(20)
                            return
                        if event.get("event") == "task_failed":
                            raise RuntimeError("MiniMax TTS 任务失败")
                except Exception:
                    self._close_locked()
                    if emitted or attempt:
                        raise

    def duplex(
        self,
        blk,
        text_queue,
        on_ready=None,
        on_segment_done=None,
        cancel_event=None,
    ):
        import websocket

        with self._session_lock():
            sender_thread = None
            cancel_watchdog = None
            cancel_watchdog_stop = threading.Event()
            provider_ws = None
            input_done = threading.Event()
            duplex_stopped = threading.Event()
            sender_error = []
            counters = {
                "sent": 0,
                "last_input_at": time.monotonic(),
            }
            counters_lock = threading.Lock()
            try:
                setup_started_at = time.perf_counter()
                reconnected = self._ensure_locked(blk)
                setup_ms = (time.perf_counter() - setup_started_at) * 1000
                provider_ws = self.ws
                provider_ws.settimeout(0.25)
                _, audio_setting = self._settings(blk)
                if on_ready:
                    on_ready({
                        "sample_rate": int(audio_setting.get("sample_rate") or 24000),
                        "reconnected": reconnected,
                        "setup_ms": round(setup_ms, 1),
                    })

                def watch_cancel():
                    while not cancel_watchdog_stop.wait(0.05):
                        if cancel_event and cancel_event.is_set():
                            try:
                                provider_ws.close()
                            except Exception:
                                pass
                            return

                cancel_watchdog = threading.Thread(
                    target=watch_cancel,
                    daemon=True,
                )
                cancel_watchdog.start()

                def send_text():
                    try:
                        while (
                            not duplex_stopped.is_set()
                            and not (cancel_event and cancel_event.is_set())
                        ):
                            try:
                                text = text_queue.get(timeout=0.25)
                            except queue.Empty:
                                continue
                            with counters_lock:
                                counters["last_input_at"] = time.monotonic()
                            if text is None:
                                input_done.set()
                                return
                            text = str(text).strip()
                            text = _normalize_tts_text(
                                text,
                                blk.get("model"),
                            )
                            if not text:
                                continue
                            with counters_lock:
                                counters["sent"] += 1
                            provider_ws.send(json.dumps(
                                {"event": "task_continue", "text": text},
                                ensure_ascii=False,
                            ))
                    except Exception as error:
                        sender_error.append(error)
                        input_done.set()
                        try:
                            provider_ws.close()
                        except Exception:
                            pass

                sender_thread = threading.Thread(target=send_text, daemon=True)
                sender_thread.start()
                completed = 0
                last_provider_event_at = time.monotonic()
                while True:
                    if cancel_event and cancel_event.is_set():
                        self._close_locked()
                        return
                    if sender_error:
                        raise sender_error[0]
                    with counters_lock:
                        sent = counters["sent"]
                        last_input_at = counters["last_input_at"]
                    if input_done.is_set() and completed >= sent:
                        self.last_activity = time.monotonic()
                        return
                    try:
                        raw_event = provider_ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if (
                            sent > completed
                            and time.monotonic() - last_provider_event_at > 15
                        ):
                            raise TimeoutError("MiniMax TTS 15 秒未返回音频")
                        if (
                            sent == completed
                            and time.monotonic() - last_input_at > 10
                        ):
                            return
                        continue
                    last_provider_event_at = time.monotonic()
                    event = json.loads(raw_event)
                    base_response = event.get("base_resp") or {}
                    if base_response.get("status_code", 0) != 0:
                        raise RuntimeError(
                            base_response.get("status_msg") or "MiniMax TTS 失败"
                        )
                    audio_hex = (event.get("data") or {}).get("audio")
                    if audio_hex:
                        yield bytes.fromhex(audio_hex)
                    if event.get("is_final"):
                        completed += 1
                        self.last_activity = time.monotonic()
                        if on_segment_done:
                            on_segment_done(completed)
                    if event.get("event") == "task_failed":
                        raise RuntimeError("MiniMax TTS 任务失败")
            except Exception:
                self._close_locked()
                raise
            finally:
                duplex_stopped.set()
                cancel_watchdog_stop.set()
                if provider_ws is not None and self.ws is provider_ws:
                    try:
                        provider_ws.settimeout(20)
                    except Exception:
                        pass
                if sender_thread is not None:
                    sender_thread.join(timeout=0.5)
                if cancel_watchdog is not None:
                    cancel_watchdog.join(timeout=0.2)


_MINIMAX_TTS_WS = _MinimaxTTSWebSocket()
_MINIMAX_TTS_STREAM_WS = _MinimaxTTSWebSocket()


# ==================== 网络扬声器（ESP32 等经 WS 接入，接收裸 PCM 播放）====================
# ESP32 作 WS 客户端连到 /api/speaker/ws；Muse 生成 TTS PCM 时顺带扇出给已启用的扬声器。
# 启用状态存 settings：speaker.enabled:<mac>（默认开）；音量存 speaker.gain:<mac>（0~200，默认100）。
_SPEAKER_STATE = {"t": 0.0, "en": {}, "gain": {}}
_SPEAKER_STATE_LOCK = threading.Lock()


def _speaker_flags(mac):
    """(enabled, gain_percent)。2 秒缓存，避免逐 PCM 块查库。"""
    now = time.time()
    with _SPEAKER_STATE_LOCK:
        if now - _SPEAKER_STATE["t"] > 2:
            _SPEAKER_STATE["en"] = {}
            _SPEAKER_STATE["gain"] = {}
            _SPEAKER_STATE["t"] = now
        if mac not in _SPEAKER_STATE["en"]:
            _SPEAKER_STATE["en"][mac] = db.get_setting("speaker.enabled:%s" % mac, "1") == "1"
            try:
                _SPEAKER_STATE["gain"][mac] = int(db.get_setting("speaker.gain:%s" % mac, "100"))
            except (TypeError, ValueError):
                _SPEAKER_STATE["gain"][mac] = 100
        return _SPEAKER_STATE["en"][mac], _SPEAKER_STATE["gain"][mac]


def _speaker_bust_cache():
    with _SPEAKER_STATE_LOCK:
        _SPEAKER_STATE["t"] = 0.0


class _SpeakerHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._speakers = {}  # sid -> {id, mac, name, addr, queue, loop, connected_at}

    def add(self, mac, name, addr, out_queue, loop):
        sid = uuid.uuid4().hex
        with self._lock:
            self._speakers[sid] = {
                "id": sid, "mac": mac, "name": name, "addr": addr,
                "queue": out_queue, "loop": loop, "connected_at": time.time(),
            }
        return sid

    def remove(self, sid):
        with self._lock:
            self._speakers.pop(sid, None)

    def snapshot(self):
        with self._lock:
            rows = list(self._speakers.values())
        return [{"id": s["id"], "mac": s["mac"], "name": s["name"],
                 "addr": s["addr"], "connected_at": s["connected_at"]} for s in rows]

    def _targets(self):
        with self._lock:
            return list(self._speakers.values())

    @staticmethod
    def _enqueue(out_queue, item):
        try:
            out_queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                out_queue.get_nowait()
                out_queue.put_nowait(item)
            except Exception:
                pass

    def _send(self, item_for):
        """item_for(gain)->item：按每台扬声器的音量生成要发的载荷。"""
        for s in self._targets():
            enabled, gain = _speaker_flags(s["mac"])
            if not enabled:
                continue
            item = item_for(gain)
            if item is None:
                continue
            try:
                s["loop"].call_soon_threadsafe(self._enqueue, s["queue"], item)
            except Exception:
                pass

    def start(self, sample_rate):
        frame = {"type": "start", "sample_rate": int(sample_rate)}
        self._send(lambda gain: frame)

    def pcm(self, data):
        if not data:
            return
        raw = bytes(data)
        self._send(lambda gain: raw if gain == 100 else _scale_pcm16(raw, gain))

    def end(self):
        frame = {"type": "end"}
        self._send(lambda gain: frame)


def _scale_pcm16(data, gain_percent):
    """按百分比软增益缩放 16-bit LE 单声道 PCM（带截幅）。gain 100=原样。"""
    if gain_percent == 100 or not data:
        return data
    try:
        import audioop
        factor = max(0, min(400, gain_percent)) / 100.0
        return audioop.mul(data, 2, factor)
    except Exception:
        return data


_SPEAKERS = _SpeakerHub()


# ==================== Claude Code 基础 URL ====================
def _claude_code_base_url(request: Request = None) -> str:
    if request is not None:
        try:
            return str(request.base_url).rstrip("/")
        except Exception:
            pass
    return os.environ.get("MUSE_PUBLIC_URL", "http://127.0.0.1:8002").rstrip("/")


# ==================== ESP-Claw 运行时配置 ====================
def _clean_http_url(value, fallback):
    text = str(value or "").strip().rstrip("/")
    try:
        parsed = urlparse(text)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return text
    except Exception:
        pass
    return fallback


def _esp_claw_runtime_config():
    return {
        "versions_url": _clean_http_url(
            db.get_setting("esp_claw.versions_url"), "https://esp-claw.com/versions"),
        "firmware_origin": _clean_http_url(
            db.get_setting("esp_claw.firmware_origin"), "https://esp-claw.com"),
    }
