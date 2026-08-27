# -*- coding: utf-8 -*-
"""把任意 MCP server 的工具映射成对象注册表里的对象。

为什么要有这一层：EV 侧此前一个 MCP 客户端都没有。灯带能在语音里用，不是因为
它有 MCP，而是 devices/coding/led.py 直连 HTTP 打设备、另外手写了一遍对象适配
器——同一个灯做了两套接入。每加一个 MCP 都手写一遍，等于把 MCP 的通用性扔了。

映射规则（一个 server = 一个对象）：
    server 名          → target_id 前缀 mcp.<server>，定向解析靠它，不全表扫描
    tool 名            → 对象的 commands
    tool 的入参 schema → 对象的 command_args
    连接状况           → 对象的 state.reachable

这样接进来的能力，模型照旧只用 object_control 操作，不多一个工具、不多一个字
的提示词——对象注册表已经量过：加 1200 个对象，每轮提示词零增长。

超时是这一层的生命线，不是可选项。反射层的全部价值是一个来回约 1.5 秒；桥连的
是另一个进程，它卡住就把整轮语音的预算吃光。所以：每次调用都带硬超时，超时即
降级——这一轮当这个能力不存在，并把 reachable=False 如实写进 state，让世界快照
反映出来，模型才不会对着一个连不上的东西许诺。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from control_plane.object_registry import object_registry


# 单次 MCP 往返的上限。反射层一轮预算约 1500ms，这里只肯花其中一小段。
CALL_TIMEOUT_S = 3.0
# 工具清单的缓存时长。列表几乎不变，但每轮都去问一次就是白等一个往返。
CATALOG_TTL_S = 30.0
# 连不上之后的冷却：别每轮都去撞一次墙，那等于每轮白等一个超时。
UNREACHABLE_COOLDOWN_S = 20.0

_LOCK = threading.Lock()
_SERVERS: Dict[str, Dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _target_prefix(server: str) -> str:
    return "mcp.%s" % str(server or "").strip().lower()


def register_server(name: str, url: str, *, timeout_s: float = CALL_TIMEOUT_S) -> None:
    """登记一个 MCP server。只记地址，不在这里连——注册不该阻塞启动。"""
    server = str(name or "").strip().lower()
    if not server:
        raise ValueError("MCP server 名不能为空")
    with _LOCK:
        _SERVERS[server] = {
            "name": server,
            "url": str(url or "").strip(),
            "timeout_s": float(timeout_s or CALL_TIMEOUT_S),
            "tools": None,          # 缓存的工具清单
            "tools_at": 0.0,
            "reachable": None,      # None=还没试过
            "error": "",
            "cooldown_until": 0.0,
        }


def registered_servers() -> List[str]:
    with _LOCK:
        return sorted(_SERVERS)


def forget_all() -> None:
    """测试用：清空登记。"""
    with _LOCK:
        _SERVERS.clear()


def _call_blocking(url: str, timeout_s: float, action: str, **kwargs) -> Tuple[Any, str]:
    """在独立线程里跑一次 MCP 会话，返回 (结果, 错误说明)。

    每次都新建会话而不是长连接：MCP 的 streamable-http 会话是有状态的，跨轮复用
    要自己管重连、超时和半死连接，而列工具/调一次工具本来就便宜。先把正确性做对，
    连接复用等量到了确实是瓶颈再说。
    """
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def run():
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if action == "list":
                    listed = await session.list_tools()
                    return [
                        {
                            "name": str(item.name),
                            "description": str(item.description or ""),
                            "schema": dict(item.inputSchema or {}),
                        }
                        for item in (listed.tools or [])
                    ]
                result = await session.call_tool(kwargs["tool"], kwargs.get("args") or {})
                parts = []
                for block in (result.content or []):
                    text = getattr(block, "text", None)
                    if text:
                        parts.append(str(text))
                return {
                    "ok": not bool(getattr(result, "isError", False)),
                    "text": "\n".join(parts)[:4000],
                }

    box: Dict[str, Any] = {}

    def worker():
        try:
            box["value"] = anyio.run(run)
        except Exception as exc:  # 连不上、协议不对、对面抛错，都归为不可达
            box["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        # 线程还在跑也不再等它。守住这一轮的预算比拿到这个结果重要，
        # 它是 daemon，进程退出时自然收走。
        return None, "超时（%.1fs）" % timeout_s
    if "error" in box:
        return None, box["error"]
    return box.get("value"), ""


def _catalog(server: str) -> Tuple[List[Dict[str, Any]], str]:
    """拿某个 server 的工具清单，走缓存与冷却。"""
    with _LOCK:
        meta = _SERVERS.get(server)
        if not meta:
            return [], "未登记"
        now = _now()
        if meta["tools"] is not None and now - meta["tools_at"] < CATALOG_TTL_S:
            return list(meta["tools"]), ""
        if now < meta["cooldown_until"]:
            return list(meta["tools"] or []), meta["error"] or "暂时不可达"
        url, timeout_s = meta["url"], meta["timeout_s"]

    tools, error = _call_blocking(url, timeout_s, "list")
    with _LOCK:
        meta = _SERVERS.get(server)
        if not meta:
            return [], "未登记"
        if error:
            meta["reachable"] = False
            meta["error"] = error
            meta["cooldown_until"] = _now() + UNREACHABLE_COOLDOWN_S
            return list(meta["tools"] or []), error
        meta["tools"] = tools or []
        meta["tools_at"] = _now()
        meta["reachable"] = True
        meta["error"] = ""
        meta["cooldown_until"] = 0.0
        return list(meta["tools"]), ""


def _command_args(schema: Dict[str, Any], doc: str = "") -> Dict[str, str]:
    """把一个工具压成「参数名 → 一句话」，对齐手写适配器的 command_args。

    doc 是工具自己的描述，必须带上：拿灯带对照时发现，取值范围和可选值往往只写在
    工具的 docstring 里，JSON Schema 里只有一个光秃秃的 type。例如 led_brightness
    的 schema 只说 integer，而它的描述写着「范围为 0 到 100」；led_effect 的可选
    灯效名同理。丢了这一句，模型就得靠报错试出参数范围——正是手写适配器当初要
    消灭的那件事（实测一次调灯因此要三个来回、4.4 秒里 3.3 秒在猜）。

    放在「说明」这个键下，渲染出来是 `led_brightness：说明=…；brightness=…`，
    和参数并列但一眼能看出不是参数名。
    """
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    out: Dict[str, str] = {}
    doc_text = " ".join(str(doc or "").split())
    if doc_text:
        out["说明"] = doc_text[:110]
    for key, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        bits = []
        text = str(spec.get("description") or "").strip()
        if text:
            bits.append(text)
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            bits.append("可选：%s" % "、".join(str(v) for v in enum[:12]))
        elif spec.get("type"):
            bits.append(str(spec.get("type")))
        bits.append("必填" if key in required else "可选")
        out[str(key)] = "；".join(bits)
    return out


def _descriptor(server: str) -> Dict[str, Any]:
    tools, error = _catalog(server)
    with _LOCK:
        meta = dict(_SERVERS.get(server) or {})
    commands = [item["name"] for item in tools]
    command_args = {}
    for item in tools:
        shape = _command_args(item.get("schema") or {}, item.get("description") or "")
        if shape:
            command_args[item["name"]] = shape
    reachable = bool(meta.get("reachable"))
    return {
        "target_id": _target_prefix(server),
        "name": server,
        "kind": "mcp",
        "owner": "assistant",
        "description": "外部 MCP 服务 %s 提供的能力。" % server,
        "aliases": [server],
        "commands": commands,
        "command_args": command_args,
        "properties": {},
        "state": {
            # 连不上要如实说。世界快照照实投影，模型才不会对着一个
            # 连不上的东西许诺「已经做好了」。
            "reachable": reachable,
            "tools": len(commands),
            "error": (error or meta.get("error") or "")[:120],
        },
    }


def _discover() -> List[Dict[str, Any]]:
    return [_descriptor(server) for server in registered_servers()]


def _execute(op: str, target: str, payload: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    del ctx
    server = str(target or "")[len("mcp."):].strip().lower()
    with _LOCK:
        meta = dict(_SERVERS.get(server) or {})
    if not meta:
        return {"ok": False, "error": "没有这个 MCP 服务：%s" % server}

    if op == "inspect":
        return {"ok": True, "op": "inspect", "objects": [_descriptor(server)]}
    if op != "invoke":
        return {"ok": False, "error": "MCP 对象只支持 inspect / invoke"}

    command = str(payload.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "要调哪个工具（command 必填）"}
    tools, error = _catalog(server)
    known = {item["name"] for item in tools}
    if command not in known:
        return {
            "ok": False,
            "error": "%s 没有 %s 这个工具" % (server, command),
            "available": sorted(known)[:20],
            "detail": error,
        }

    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    value, error = _call_blocking(
        meta["url"], meta["timeout_s"], "call", tool=command, args=args,
    )
    if error:
        with _LOCK:
            live = _SERVERS.get(server)
            if live is not None:
                live["reachable"] = False
                live["error"] = error
                live["cooldown_until"] = _now() + UNREACHABLE_COOLDOWN_S
        # 明确说没做成。没有回执就没有 after 可复述，播报规则会挡住
        # 「已经做好了」这种话。
        return {"ok": False, "changed": False, "error": error, "target_id": target}
    return {
        "ok": bool((value or {}).get("ok")),
        "changed": bool((value or {}).get("ok")),
        "target_id": target,
        "target_name": server,
        "command": command,
        "after": str((value or {}).get("text") or "")[:600],
    }


_REGISTERED = False


def ensure_provider() -> None:
    """把桥挂进对象注册表。纯加法：没登记任何 server 时它什么都不产出。"""
    global _REGISTERED
    if _REGISTERED:
        return
    object_registry.register_provider(
        "mcp-bridge",
        discover=_discover,
        execute=_execute,
        target_prefixes=("mcp.",),
    )
    _REGISTERED = True
