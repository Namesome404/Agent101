# -*- coding: utf-8 -*-
"""Proxy core WebSocket and vision traffic through the Muse entrypoint."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import parse_qs

import websockets
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from common.paths import TMP_DIR

CORE_WS_URL = "ws://127.0.0.1:8000/xiaozhi/v1/"
CORE_HTTP = "http://127.0.0.1:8003"

router = APIRouter()
logger = logging.getLogger("muse.proxy_core")
_PROXY_LOG = TMP_DIR / "ws_proxy.log"


def _plog(msg: str) -> None:
    try:
        with open(_PROXY_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _core_ws_target(client_ws: WebSocket) -> str:
    query = str(client_ws.url.query) if client_ws.url.query else ""
    if not query:
        return CORE_WS_URL
    return CORE_WS_URL.rstrip("/") + "/?" + query


def _forward_ws_headers(client_ws: WebSocket) -> dict:
    out = {}
    for key in (
        "device-id",
        "client-id",
        "authorization",
        "protocol-version",
        "user-agent",
    ):
        val = client_ws.headers.get(key)
        if val:
            out[key] = val
    qs = parse_qs(str(client_ws.url.query or ""))
    for key in ("device-id", "client-id", "authorization", "protocol-version"):
        if key not in out and key in qs and qs[key]:
            out[key] = qs[key][0]
    host = client_ws.headers.get("host") or client_ws.headers.get("x-forwarded-host")
    if host:
        out["X-Forwarded-Host"] = host.split(",")[0].strip()
    proto = client_ws.headers.get("x-forwarded-proto")
    if not proto:
        proto = "https" if client_ws.url.scheme == "wss" else "http"
    out["X-Forwarded-Proto"] = proto
    return out


async def _ws_proxy_loop(client_ws: WebSocket, backend) -> None:
    closed = asyncio.Event()

    async def pump_backend_to_client() -> None:
        try:
            async for raw in backend:
                _plog(
                    "backend->client "
                    + ("text" if isinstance(raw, str) else "bytes")
                    + f" len={len(raw)}"
                )
                if isinstance(raw, str):
                    await client_ws.send_text(raw)
                else:
                    await client_ws.send_bytes(raw)
        except websockets.ConnectionClosed as exc:
            _plog(f"backend closed: {exc.code} {exc.reason}")
        except Exception as exc:
            _plog(f"pump backend->client error: {type(exc).__name__}: {exc}")
        finally:
            closed.set()

    pump_task = asyncio.create_task(pump_backend_to_client())
    try:
        while not closed.is_set():
            msg = await client_ws.receive()
            mtype = msg.get("type")
            if mtype == "websocket.disconnect":
                _plog("client disconnect")
                break
            text = msg.get("text")
            data = msg.get("bytes")
            if text:
                _plog(f"client->backend text len={len(text)}")
                await backend.send(text)
            elif data:
                _plog(f"client->backend bytes len={len(data)}")
                await backend.send(data)
    except WebSocketDisconnect:
        _plog("client WebSocketDisconnect")
    except Exception as exc:
        _plog(f"client loop error: {type(exc).__name__}: {exc}")
    finally:
        closed.set()
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


@router.websocket("/xiaozhi/v1")
@router.websocket("/xiaozhi/v1/")
async def proxy_core_websocket(client_ws: WebSocket):
    await client_ws.accept()
    target = _core_ws_target(client_ws)
    backend = None
    try:
        _plog(f"connect target={target}")
        backend = await websockets.connect(
            target,
            max_size=16 * 1024 * 1024,
            ping_interval=None,
            close_timeout=5,
            open_timeout=10,
        )
        await _ws_proxy_loop(client_ws, backend)
    except Exception as exc:
        _plog(f"proxy failed: {type(exc).__name__}: {exc}")
        logger.warning("WS proxy failed target=%s err=%s", target, exc)
    finally:
        if backend is not None:
            try:
                await backend.close()
            except Exception:
                pass
        try:
            await client_ws.close()
        except Exception:
            pass


def _proxy_headers(request: Request) -> dict:
    out = {}
    for key, val in request.headers.items():
        lk = key.lower()
        if lk in ("host", "connection", "content-length", "transfer-encoding"):
            continue
        out[key] = val
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if host:
        out["X-Forwarded-Host"] = host.split(",")[0].strip()
    proto = request.headers.get("x-forwarded-proto")
    if not proto:
        proto = "https" if request.url.scheme == "https" else "http"
    out["X-Forwarded-Proto"] = proto
    return out

