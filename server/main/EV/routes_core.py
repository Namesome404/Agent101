# -*- coding: utf-8 -*-
"""核心契约层：/xiaozhi/config/*、/config/* 兼容、OTA、/agent/* 记忆/会话回调。

从 app.py 拆出的 APIRouter。这是 ESP32/小智设备对接 Muse 的 manager-api 兼容面，
鉴权用 server.secret，全部走 /xiaozhi/* 路径（另有 /config/*、/agent/* 兼容别名）。
"""
from __future__ import annotations

import socket
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import JSONResponse, Response

from app_shared import _external_base_url
from control_plane import contract
from control_plane import database as db

router = APIRouter()


def _check_secret(authorization):
    secret = db.get_setting("server.secret")
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return authorization[7:].strip() == secret


def _first_text(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _selected_module(payload):
    selected = (
        payload.get("selectedModule")
        or payload.get("selected_module")
        or payload.get("selectedModuleFromClient")
        or payload.get("selected_module_from_client")
        or {}
    )
    return selected if isinstance(selected, dict) else {}


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "client-id, content-type, device-id, authorization, device-model, device-version",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Credentials": "true",
    }


def _lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _ws_url_for(request) -> str:
    """下发给设备的 websocket 地址，跟随访问来源：
    HTTPS / Tailscale → wss://<Host>/xiaozhi/v1/（经 Muse 反代）；
    局域网直连 → ws://<本机IP>:8000/xiaozhi/v1/。"""
    try:
        base = _external_base_url(request)
        parsed = urlparse(base)
        host = (parsed.netloc or parsed.hostname or "").split(",")[0].strip()
        if parsed.scheme == "https" or host.endswith(".ts.net"):
            # 保留完整 host（含端口）：Tailscale 443 无端口不受影响；自签 HTTPS 的非标端口(如 8443)得保住端口
            return "wss://%s/xiaozhi/v1/" % host
    except Exception:
        pass
    return "ws://%s:8000/xiaozhi/v1/" % _lan_ip()


def _ok():
    return {"code": 0, "msg": "success", "data": None}


# ==================== /xiaozhi/config/*（manager-api 兼容） ====================
@router.post("/xiaozhi/config/server-base")
async def server_base(authorization: str = Header(None)):
    if not _check_secret(authorization):
        return JSONResponse({"code": 401, "msg": "unauthorized"}, status_code=401)
    return {"code": 0, "msg": "success", "data": contract.build_server_base()}


@router.post("/config/server-base")
async def server_base_compat(authorization: str = Header(None)):
    return await server_base(authorization)


@router.post("/xiaozhi/config/agent-models")
async def agent_models(request: Request, payload: dict = Body(...), authorization: str = Header(None)):
    if not _check_secret(authorization):
        return JSONResponse({"code": 401, "msg": "unauthorized"}, status_code=401)
    headers = request.headers
    mac = _first_text(
        payload.get("macAddress"),
        payload.get("mac_address"),
        payload.get("deviceId"),
        payload.get("device_id"),
        payload.get("device-id"),
        headers.get("device-id"),
        headers.get("mac-address"),
    )
    client_id = _first_text(
        payload.get("clientId"),
        payload.get("client_id"),
        payload.get("client-id"),
        headers.get("client-id"),
        mac,
    )
    status, data = contract.build_agent_models(mac, client_id, _selected_module(payload))
    if status == "bind":
        # 10042 → 核心抛 DeviceBindException，把 msg 当绑定码播报给设备
        return {"code": 10042, "msg": data}
    return {"code": 0, "msg": "success", "data": data}


@router.post("/config/agent-models")
async def agent_models_compat(request: Request, payload: dict = Body(...), authorization: str = Header(None)):
    return await agent_models(request, payload, authorization)


@router.post("/xiaozhi/config/correct-words")
async def correct_words(payload: dict = Body(...), authorization: str = Header(None)):
    if not _check_secret(authorization):
        return JSONResponse({"code": 401, "msg": "unauthorized"}, status_code=401)
    return {"code": 0, "msg": "success", "data": {}}


@router.post("/config/correct-words")
async def correct_words_compat(payload: dict = Body(...), authorization: str = Header(None)):
    return await correct_words(payload, authorization)


# ==================== OTA（API 模式下由 Muse 作为 manager-api 提供） ====================
@router.options("/xiaozhi/ota/")
def ota_options():
    return Response(status_code=204, headers=_cors_headers())


@router.get("/xiaozhi/ota/")
def ota_get(request: Request):
    ws_url = _ws_url_for(request)
    return Response("Muse OTA online. websocket=%s" % ws_url,
                    media_type="text/plain", headers=_cors_headers())


@router.post("/xiaozhi/ota/")
async def ota_post(request: Request):
    device_id = request.headers.get("device-id", "").strip()
    client_id = request.headers.get("client-id", "").strip()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not device_id:
        device_id = str(payload.get("mac_address") or payload.get("macAddress") or "").strip()
    if not client_id:
        client_id = str(payload.get("client_id") or payload.get("clientId") or "muse-web-client").strip()
    if device_id:
        db.touch_or_create_device(device_id, client_id)
    version = ""
    try:
        version = str((payload.get("application") or {}).get("version") or "")
    except Exception:
        version = ""
    if not version:
        version = request.headers.get("device-version", "0.0.0")
    data = {
        "server_time": {
            "timestamp": int(round(time.time() * 1000)),
            "timezone_offset": 480,
        },
        "firmware": {
            "version": version,
            "url": "",
        },
        "websocket": {
            "url": _ws_url_for(request),
            "token": "",
        },
    }
    return JSONResponse(data, headers=_cors_headers())


# ==================== 核心回调：记忆写回 / 会话总结（兼容 manager-api 路径） ====================
@router.put("/agent/saveMemory/{mac}")
@router.put("/xiaozhi/agent/saveMemory/{mac}")
async def agent_save_memory(mac: str, payload: dict = Body(...), authorization: str = Header(None)):
    if not _check_secret(authorization):
        return JSONResponse({"code": 401, "msg": "unauthorized"}, status_code=401)
    summary = payload.get("summaryMemory")
    if summary is None:
        summary = payload.get("summary_memory", "")
    if not db.set_summary_memory_by_mac(mac, summary if summary is not None else ""):
        return {"code": 10041, "msg": "device not found or unbound"}
    return _ok()


@router.post("/agent/appendMemory/{mac}")
@router.post("/xiaozhi/agent/appendMemory/{mac}")
async def agent_append_memory(mac: str, payload: dict = Body(...), authorization: str = Header(None)):
    """会话中追加单条/多条记忆（不覆盖已有自动总结条目）。"""
    if not _check_secret(authorization):
        return JSONResponse({"code": 401, "msg": "unauthorized"}, status_code=401)
    texts = payload.get("items") or payload.get("texts") or []
    if isinstance(payload.get("text"), str) and payload.get("text").strip():
        texts = [payload["text"]]
    if not isinstance(texts, list):
        texts = [str(texts)]
    source = (payload.get("source") or "explicit").strip() or "explicit"
    if not db.add_memory_items_by_mac(mac, texts, source=source):
        return {"code": 10041, "msg": "device not found or unbound"}
    return _ok()


@router.post("/agent/chat-summary/{session_id}/save")
@router.post("/xiaozhi/agent/chat-summary/{session_id}/save")
async def agent_chat_summary(session_id: str, authorization: str = Header(None)):
    """兼容核心回调。Muse 侧记忆由 saveMemory 写入；此处直接成功返回。"""
    if not _check_secret(authorization):
        return JSONResponse({"code": 401, "msg": "unauthorized"}, status_code=401)
    return _ok()


@router.post("/agent/chat-title/{session_id}/generate")
@router.post("/xiaozhi/agent/chat-title/{session_id}/generate")
async def agent_chat_title(session_id: str, authorization: str = Header(None)):
    if not _check_secret(authorization):
        return JSONResponse({"code": 401, "msg": "unauthorized"}, status_code=401)
    return _ok()


@router.post("/agent/chat-history/report")
@router.post("/xiaozhi/agent/chat-history/report")
async def agent_chat_history_report(payload: dict = Body(None), authorization: str = Header(None)):
    if not _check_secret(authorization):
        return JSONResponse({"code": 401, "msg": "unauthorized"}, status_code=401)
    return _ok()
