# -*- coding: utf-8 -*-
"""Muse 智能体会话：主机终端 + 远程麦克风/摄像头并入同一会话。"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Set

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

MUSE_HOST_BY_AGENT: Dict[int, Any] = {}
MUSE_REMOTES_BY_AGENT: Dict[int, Set[Any]] = {}
MUSE_CONVERSATION_API = "http://127.0.0.1:8002/api/agents/{}/conversation"


def parse_muse_agent_id(device_id: Optional[str]) -> Optional[int]:
    if not device_id or not str(device_id).startswith("muse:"):
        return None
    tail = str(device_id).split(":", 1)[1]
    try:
        return int(tail)
    except ValueError:
        return None


def is_remote_client(client_id: Optional[str]) -> bool:
    return "-remote" in (client_id or "")


def register_host(agent_id: int, handler) -> None:
    MUSE_HOST_BY_AGENT[agent_id] = handler
    logger.bind(tag=TAG).info(f"Muse host registered agent={agent_id} device={handler.device_id}")


def unregister_host(agent_id: int, handler) -> None:
    if MUSE_HOST_BY_AGENT.get(agent_id) is handler:
        MUSE_HOST_BY_AGENT.pop(agent_id, None)
        logger.bind(tag=TAG).info(f"Muse host unregistered agent={agent_id}")


def get_host(agent_id: int):
    return MUSE_HOST_BY_AGENT.get(agent_id)


def _conversation_request(agent_id: int, payload=None) -> dict:
    url = MUSE_CONVERSATION_API.format(agent_id)
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=0.4) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def sync_shared_history(handler, agent_id: int) -> None:
    seen = getattr(handler, "_muse_shared_message_ids", None)
    if seen is None:
        seen = set()
        handler._muse_shared_message_ids = seen
    result = _conversation_request(agent_id)
    for item in result.get("messages", []):
        message_id = int(item.get("id") or 0)
        if not message_id or message_id in seen:
            continue
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            from core.utils.dialogue import Message
            handler.dialogue.put(Message(role=role, content=content))
        seen.add(message_id)


def publish_shared_message(handler, agent_id: int, role: str, content: str) -> None:
    content = str(content or "").strip()
    if not content:
        return
    result = _conversation_request(agent_id, {
        "role": role,
        "content": content,
        "source": "browser",
    })
    message_id = int(result.get("id") or 0)
    if message_id:
        seen = getattr(handler, "_muse_shared_message_ids", None)
        if seen is None:
            seen = set()
            handler._muse_shared_message_ids = seen
        seen.add(message_id)


def host_status(agent_id: int) -> dict:
    host = get_host(agent_id)
    ready = _host_ready(host)
    return {
        "registered": host is not None,
        "ready": ready,
        "session_id": getattr(host, "session_id", None) if host else None,
        "device_id": getattr(host, "device_id", None) if host else None,
    }


async def wait_host_ready(agent_id: int, timeout_sec: float = 25.0):
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        host = get_host(agent_id)
        if _host_ready(host):
            return host
        await asyncio.sleep(0.4)
    return None


def _host_ready(host) -> bool:
    if not host or not host.websocket:
        return False
    try:
        closed = getattr(host.websocket, "closed", None)
        if closed is True:
            return False
        state = getattr(getattr(host.websocket, "state", None), "name", None)
        if state == "CLOSED":
            return False
    except Exception:
        pass
    if host.need_bind:
        return False
    return host.bind_completed_event.is_set()


async def _notify_remotes(agent_id: int, payload: dict) -> None:
    remotes = list(MUSE_REMOTES_BY_AGENT.get(agent_id, set()))
    raw = json.dumps(payload, ensure_ascii=False)
    dead = []
    for ws in remotes:
        try:
            await ws.send(raw)
        except Exception:
            dead.append(ws)
    for ws in dead:
        MUSE_REMOTES_BY_AGENT.get(agent_id, set()).discard(ws)


async def broadcast_to_remotes(agent_id: int, payload: dict) -> None:
    if agent_id not in MUSE_REMOTES_BY_AGENT:
        return
    await _notify_remotes(agent_id, payload)


def has_active_remotes(agent_id: int) -> bool:
    return bool(MUSE_REMOTES_BY_AGENT.get(agent_id))


async def notify_remote_presence(agent_id: int) -> None:
    """通知主机：是否有 iPhone 远程麦克风在线（主机应暂停本地麦克风）。"""
    host = get_host(agent_id)
    if not host or not host.websocket:
        return
    count = len(MUSE_REMOTES_BY_AGENT.get(agent_id, set()))
    payload = {
        "type": "muse_remote",
        "active": count > 0,
        "count": count,
    }
    try:
        await host.websocket.send(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


async def _send_host_welcome(remote_ws, host) -> None:
    welcome = dict(host.welcome_msg or {})
    welcome["type"] = "hello"
    welcome["session_id"] = host.session_id
    payload = json.dumps(welcome, ensure_ascii=False)
    await remote_ws.send(payload)
    logger.bind(tag=TAG).info(f"Muse remote welcome sent session={host.session_id}")


async def relay_remote_connection(remote_ws, agent_id: int) -> None:
    """iPhone 远程端：音频/指令转发到已在线的主机会话。"""
    remotes = MUSE_REMOTES_BY_AGENT.setdefault(agent_id, set())
    remote_listen_started = False
    joined = False

    try:
        async for message in remote_ws:
            if not joined and isinstance(message, str):
                try:
                    msg = json.loads(message)
                except Exception:
                    continue
                if msg.get("type") == "hello":
                    host = await wait_host_ready(agent_id, 30.0)
                    if not host:
                        await remote_ws.send(json.dumps({
                            "type": "error",
                            "message": "主机会话未建立，请先在电脑端点击「轻触开始」并等待连接成功",
                        }, ensure_ascii=False))
                        break
                    remotes.add(remote_ws)
                    joined = True
                    logger.bind(tag=TAG).info(
                        f"Muse remote joined agent={agent_id} session={host.session_id}"
                    )
                    await _send_host_welcome(remote_ws, host)
                    await notify_remote_presence(agent_id)
                    continue

            if not joined:
                continue

            host = get_host(agent_id)
            if not host or not _host_ready(host):
                await remote_ws.send(json.dumps({
                    "type": "error",
                    "message": "主机会话已断开",
                }, ensure_ascii=False))
                break

            if isinstance(message, bytes):
                if not remote_listen_started:
                    remote_listen_started = True
                    from core.handle.textHandle import handleTextMessage
                    await handleTextMessage(host, json.dumps({
                        "type": "listen",
                        "state": "start",
                        "mode": "auto",
                        "session_id": host.session_id,
                    }))
                host.asr_audio_queue.put(message)
                continue

            try:
                msg = json.loads(message)
            except Exception:
                continue

            msg_type = msg.get("type")
            if msg_type == "hello":
                await _send_host_welcome(remote_ws, host)
                continue

            if msg_type == "listen":
                from core.handle.textHandle import handleTextMessage
                await handleTextMessage(host, message)
                continue

            if msg_type in ("abort", "mcp"):
                from core.handle.textHandle import handleTextMessage
                await handleTextMessage(host, message)
    except Exception as exc:
        logger.bind(tag=TAG).warning(
            f"Muse remote relay ended agent={agent_id}: {exc}\n{traceback.format_exc()}"
        )
    finally:
        remotes.discard(remote_ws)
        logger.bind(tag=TAG).info(f"Muse remote left agent={agent_id}")
        await notify_remote_presence(agent_id)
