# -*- coding: utf-8 -*-
"""语音终端共享运行时：配置常量 + 跨模块可变状态 + 纯文本工具。

所有依赖此文件的模块只从这里读取状态，不在模块间互相引用，
从根上避免拆分后出现循环导入。加载顺序与拆分前 terminal.py 顶部完全一致：
先 _load_ev_env()，再按序定义常量与状态。
"""
from __future__ import annotations

import os
import threading
import time

import requests

from common.paths import SERVER_DIR, TMP_DIR


def _load_ev_env():
    """加载 EV/.env；打断相关键以文件为准，避免旧 shell 环境把误打断参数锁死。

    新前缀 VOICE_*；仍接受文件/环境中的旧名 CAMERA_VOICE_*（自动映射）。
    """
    try:
        from pathlib import Path as _Path
        env_path = _Path(__file__).resolve().parents[2] / ".env"
        if not env_path.is_file():
            return
        force_prefix = (
            "VOICE_BARGE_",
            "VOICE_ECHO_",
            "VOICE_MUTE_",
            "VOICE_INPUT_",
            "CAMERA_VOICE_BARGE_",
            "CAMERA_VOICE_ECHO_",
            "CAMERA_VOICE_MUTE_",
            "CAMERA_VOICE_INPUT_",
        )
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip("\"'")
            if not name:
                continue
            if name.startswith(force_prefix) or name not in os.environ:
                os.environ[name] = value
            # 旧键写入时同步到 VOICE_*（不覆盖已有 VOICE_*）
            if name.startswith("CAMERA_VOICE_"):
                new_name = "VOICE_" + name[len("CAMERA_VOICE_"):]
                if new_name.startswith(force_prefix) or new_name not in os.environ:
                    os.environ[new_name] = value
    except Exception:
        pass
    try:
        from devices.voice.env import migrate_camera_voice_environ
        migrate_camera_voice_environ()
    except Exception:
        pass


_load_ev_env()

SR = 16000
FRAME_MS = 20
FRAME_BYTES = int(SR * FRAME_MS / 1000) * 2  # 640
PADDING = int(os.environ.get("VOICE_PADDING_FRAMES", "12"))
VAD_CONFIRM_FRAMES = int(PADDING * 0.9) + 1
VAD_TAIL_SECONDS = VAD_CONFIRM_FRAMES * FRAME_MS / 1000.0
VAD_TRIGGER_RATIO = float(os.environ.get(
    "VOICE_VAD_TRIGGER_RATIO",
    "0.60",
))
ASR_EARLY_FINISH_MS = int(os.environ.get(
    "VOICE_ASR_EARLY_FINISH_MS",
    "180",
))
ASR_EARLY_FINISH_FRAMES = (
    max(1, min(VAD_CONFIRM_FRAMES - 1, ASR_EARLY_FINISH_MS // FRAME_MS))
    if ASR_EARLY_FINISH_MS > 0
    else 0
)
MIN_VOICED = int(os.environ.get("VOICE_MIN_FRAMES", "6"))
VAD_MODE = int(os.environ.get("VOICE_VAD_MODE", "2"))
INPUT_GAIN = float(os.environ.get("VOICE_INPUT_GAIN", "1.5"))
MAX_UTT_SECONDS = float(os.environ.get("VOICE_MAX_UTT_SECONDS", "30"))
MAX_UTT_FRAMES = max(1, int(MAX_UTT_SECONDS * 1000 / FRAME_MS))
GREET = os.environ.get("VOICE_GREET", "1").lower() not in ("0", "", "off", "no", "false")
# 短陈述，避免被麦拾回后当成用户提问
GREET_TEXT = os.environ.get("CAMERA_GREET_TEXT", "在呢。")
GREET_COOLDOWN = float(os.environ.get("CAMERA_GREET_COOLDOWN", "20"))
_LISTEN_MUTE_AFTER_PLAY = float(os.environ.get("VOICE_MUTE_AFTER_PLAY", "0.25"))
_speak_lock = threading.Lock()  # 保证问候与应答不同时出声
_MIC_Q = None  # 麦克风帧队列(全局，供问候线程播完后清空防回环)
_BARGE_IN_EVENT = threading.Event()
MUSE = os.environ.get("MUSE_URL", "http://127.0.0.1:8002")
AGENT_ID = int(os.environ.get("VOICE_AGENT", "1"))
CAMERA = os.environ.get("VOICE_CAMERA") or None
# 语音输出位置：pc=本机喇叭(默认)，camera=摄像头喇叭。
# 注：这台小米 chuangmi.camera.039a01 的喇叭 backchannel 在 go2rtc 里是坏的(出噪声，逐型号未支持)，故默认走 PC。
OUTPUT = os.environ.get("VOICE_OUTPUT", "pc").lower()
INPUT_PREF = os.environ.get("VOICE_INPUT", "auto").strip().lower() or "auto"
RTSP_LOW_LATENCY = os.environ.get(
    "VOICE_RTSP_LOW_LATENCY",
    "1",
).lower() not in ("0", "", "off", "no", "false")
TMP = str(TMP_DIR)
os.makedirs(TMP, exist_ok=True)
_HTTP_LOCAL = threading.local()


def _pc_mic_available():
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        return any(int(d.get("max_input_channels") or 0) > 0 for d in devices)
    except Exception:
        return False


def _resolve_input_mode():
    if INPUT_PREF in ("pc", "host", "local", "mic"):
        return "pc"
    if INPUT_PREF in ("camera", "rtsp", "cam"):
        return "camera"
    # auto：本机有麦就用本机（项目启动即语音）；否则退回摄像头麦
    return "pc" if _pc_mic_available() else "camera"


INPUT_MODE = _resolve_input_mode()

# Speculative 预取对话已彻底删除（2026-08-12）：草稿基于半句话生成，意图
# 判断不稳定，动作指令常被截胡「说行不做事」，且休眠代码占着主循环判断。
# 所有轮次统一走正式带工具轮，动作执行可靠；纯对话延迟靠连接池预热 + 低
# 延迟 TTS 掩盖，不再做提前生成。
FIRST_SEGMENT_CHARS = int(os.environ.get("VOICE_FIRST_SEGMENT_CHARS", "18"))
NEXT_SEGMENT_CHARS = int(os.environ.get("VOICE_NEXT_SEGMENT_CHARS", "42"))

_COMMAND_IGNORED_CHARS = str.maketrans(
    "",
    "",
    " \t\r\n,，。.!！?？、;；:：~～",
)


def _normalized_command(text):
    return str(text or "").translate(_COMMAND_IGNORED_CHARS).lower()


def _same_command(left, right):
    return bool(left and right) and _normalized_command(left) == _normalized_command(right)


def _tool_ack_key(command):
    return _normalized_command(command)[:64]


# 同一轮搜索只垫场一次（防抢跑流被替换后「稍等」说两遍）
_TOOL_ACK_GUARD = {"key": "", "at": 0.0, "text": "", "progress_key": ""}
_TOOL_ACK_GUARD_LOCK = threading.Lock()


def _claim_tool_progress(command, text):
    """慢工具开始语：同一指令 45s 内只播一次（独立于 tool_ack guard）。"""
    key = _tool_ack_key(command)
    text = str(text or "").strip()
    if not key or not text:
        return False
    now = time.time()
    with _TOOL_ACK_GUARD_LOCK:
        if (
            _TOOL_ACK_GUARD["progress_key"]
            and _same_command(_TOOL_ACK_GUARD["progress_key"], key)
            and now - _TOOL_ACK_GUARD["at"] < 45
        ):
            return False
        _TOOL_ACK_GUARD["progress_key"] = key
        _TOOL_ACK_GUARD["at"] = now
        return True


def _claim_tool_ack(command, text):
    """同一指令 45s 内只允许播一次工具垫场。"""
    key = _tool_ack_key(command)
    text = str(text or "").strip()
    if not key or not text:
        return False
    now = time.time()
    with _TOOL_ACK_GUARD_LOCK:
        if (
            _TOOL_ACK_GUARD["key"]
            and _same_command(_TOOL_ACK_GUARD["key"], key)
            and now - _TOOL_ACK_GUARD["at"] < 45
        ):
            return False
        _TOOL_ACK_GUARD["key"] = key
        _TOOL_ACK_GUARD["at"] = now
        _TOOL_ACK_GUARD["text"] = text
        return True


def _tool_ack_already_claimed(command):
    key = _tool_ack_key(command)
    now = time.time()
    with _TOOL_ACK_GUARD_LOCK:
        return bool(
            key
            and _TOOL_ACK_GUARD["key"]
            and _same_command(_TOOL_ACK_GUARD["key"], key)
            and now - _TOOL_ACK_GUARD["at"] < 45
        )


def _label_match(label, other):
    text = str(label or "").strip().lower()
    other = str(other or "").strip().lower()
    if not text or not other:
        return False
    return text == other or other in text or text in other


def _label_disabled(label, disabled_labels):
    text = str(label or "").strip().lower()
    if not text:
        return False
    for item in disabled_labels or []:
        if _label_match(text, item):
            return True
    return False


def _label_in_list(label, labels):
    for item in labels or []:
        if _label_match(label, item):
            return True
    return False


def _http():
    session = getattr(_HTTP_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _HTTP_LOCAL.session = session
    return session
