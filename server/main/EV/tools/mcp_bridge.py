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

装一个 MCP 就是往 data/mcp_servers.json 里加一行（data/ 已 gitignore，和
config.yaml 一个待遇）。格式：

    {
      "mcpServers": {
        "muse-led": {"url": "http://127.0.0.1:8012/mcp", "timeout_s": 3},
        "chrome": {
          "url": "http://127.0.0.1:9222/mcp",
          "voice_tools": ["new_page", "close_page", "take_screenshot"]
        },
        "chrome-dev": {
          "command": "npx",
          "args": ["-y", "chrome-devtools-mcp@latest", "--isolated"],
          "voice_tools": ["new_page", "navigate_page", "close_page"]
        }
      }
    }

两种形式都认：写 url 的是 streamable-http，每次新建会话；写 command 的是 stdio，
EV 把它拉起来常驻——npx 冷启动要好几秒，每次重开扛不住反射层 1.5 秒的预算。

voice_tools 是白名单：只有列出来的给语音看，其余留给工作 Agent。不写就是全给。
exclude 是黑名单：从全给里剔掉几个。写白名单之前先看启动日志，它会把每个服务的
全量工具名打出来、并标明哪些没进语音层。

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


def register_server(
    name: str,
    url: str = "",
    *,
    command: str = "",
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_s: float = CALL_TIMEOUT_S,
    voice_tools: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
) -> None:
    """登记一个 MCP server。只记地址，不在这里连——注册不该阻塞启动。

    voice_tools 是白名单：只有列出来的工具给语音看。不写就是全给。
    exclude 是黑名单：从全给里剔掉几个。两个都写时白名单先生效。

    为什么要分：语音一轮预算约 1.5 秒，只装得下「一次调用就有确定回执」的能力。
    Chrome 的「开标签页」属于这一类；「排查控制台报错」要看日志、改代码、再验证，
    多轮，属于工作 Agent。可这两类工具常常来自同一个 MCP 服务，所以判据必须落在
    单个工具上，不能按服务一刀切。

    没进语音层的工具不是废了——工作 Agent 用的是它自己那份 MCP 配置（Claude
    Code 在工作目录下读），不经过 EV。所以这里的「工作层」就是「不给语音看」，
    不需要在 EV 里再建一条路由。
    """
    server = str(name or "").strip().lower()
    if not server:
        raise ValueError("MCP server 名不能为空")
    with _LOCK:
        _SERVERS[server] = {
            "name": server,
            "url": str(url or "").strip(),
            "command": str(command or "").strip(),
            "args": [str(a) for a in (args or [])],
            "env": {str(k): str(v) for k, v in (env or {}).items()},
            "timeout_s": float(timeout_s or CALL_TIMEOUT_S),
            "voice_tools": [str(t).strip() for t in (voice_tools or []) if str(t).strip()],
            "exclude": [str(t).strip() for t in (exclude or []) if str(t).strip()],
            "tools": None,          # 缓存的工具清单（服务端说它有什么，未过滤）
            "tools_at": 0.0,
            "reachable": None,      # None=还没试过
            "error": "",
            "cooldown_until": 0.0,
        }


def _voice_visible(server: str, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按配置滤出给语音看的那部分。"""
    with _LOCK:
        meta = _SERVERS.get(server) or {}
        allow = list(meta.get("voice_tools") or [])
        deny = set(meta.get("exclude") or [])
    if allow:
        wanted = set(allow)
        return [t for t in tools if t["name"] in wanted]
    return [t for t in tools if t["name"] not in deny]


def all_tools(server: str) -> List[str]:
    """服务端到底有哪些工具——含没给语音看的。

    写白名单之前得先知道有什么可写。启动日志和这个函数都给全量，
    过滤只发生在给模型的那一份上。
    """
    tools, _ = _catalog(server)
    return [t["name"] for t in tools]


def registered_servers() -> List[str]:
    with _LOCK:
        return sorted(_SERVERS)


def forget_all() -> None:
    """测试用：清空登记。"""
    with _LOCK:
        _SERVERS.clear()


def _unpack(action: str, result: Any) -> Any:
    """把 MCP 的返回压成桥内部那两种形状。list 与 call 共用。"""
    if action == "list":
        return [
            {
                "name": str(item.name),
                "description": str(item.description or ""),
                "schema": dict(item.inputSchema or {}),
            }
            for item in (result.tools or [])
        ]
    parts = []
    for block in (result.content or []):
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return {
        "ok": not bool(getattr(result, "isError", False)),
        "text": "\n".join(parts)[:4000],
    }


class _StdioSession:
    """一个常驻的 stdio MCP 子进程。

    stdio 形式的 server（chrome-devtools-mcp、多数 npx 起的那种）没有网址，
    只能由我们把它拉起来、按管道说话。关键是**进程必须常驻**：每次调用都
    npx 一遍要好几秒，反射层一轮预算才 1.5 秒，根本扛不住。

    实现用 anyio 的 blocking portal：一个后台线程跑事件循环，把会话开着；
    外面的同步代码通过 portal 提交任务，拿 concurrent.futures.Future，
    于是超时仍然由调用方说了算——卡住的子进程不能把这一轮拖没。
    """

    def __init__(self, name: str, command: str, args: List[str], env: Dict[str, str]):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self._portal_cm = None
        self._portal = None
        self._stdio_cm = None
        self._session_cm = None
        self._session = None
        self._lock = threading.Lock()

    def _start_locked(self, timeout_s: float) -> str:
        import os

        from anyio.from_thread import start_blocking_portal
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            # 子进程要继承 PATH 才找得到 npx/node；只把配置里写的覆盖上去。
            env=dict(os.environ, **self.env),
        )
        self._portal_cm = start_blocking_portal()
        self._portal = self._portal_cm.__enter__()
        self._stdio_cm = self._portal.wrap_async_context_manager(stdio_client(params))
        read, write = self._stdio_cm.__enter__()
        self._session_cm = self._portal.wrap_async_context_manager(ClientSession(read, write))
        self._session = self._session_cm.__enter__()
        future = self._portal.start_task_soon(self._session.initialize)
        future.result(timeout=timeout_s)
        return ""

    def _stop_locked(self) -> None:
        for closer in (self._session_cm, self._stdio_cm):
            try:
                if closer is not None:
                    closer.__exit__(None, None, None)
            except Exception:
                pass
        try:
            if self._portal_cm is not None:
                self._portal_cm.__exit__(None, None, None)
        except Exception:
            pass
        self._portal_cm = self._portal = None
        self._stdio_cm = self._session_cm = self._session = None

    def call(self, timeout_s: float, action: str, **kwargs) -> Tuple[Any, str]:
        with self._lock:
            if self._session is None:
                try:
                    self._start_locked(timeout_s)
                except Exception as exc:
                    self._stop_locked()
                    return None, "启动失败 %s: %s" % (type(exc).__name__, str(exc)[:180])
            session, portal = self._session, self._portal
            try:
                if action == "list":
                    future = portal.start_task_soon(session.list_tools)
                else:
                    future = portal.start_task_soon(
                        session.call_tool, kwargs["tool"], kwargs.get("args") or {},
                    )
                return _unpack(action, future.result(timeout=timeout_s)), ""
            except Exception as exc:
                # 超时也好、管道断了也好，都把会话丢掉重来：半死的 stdio 连接
                # 修不回来，留着只会让接下来每次调用都吃一个超时。
                self._stop_locked()
                name = type(exc).__name__
                if name in ("TimeoutError", "FuturesTimeoutError"):
                    return None, "超时（%.1fs）" % timeout_s
                return None, "%s: %s" % (name, str(exc)[:180])


_STDIO: Dict[str, _StdioSession] = {}
_ATEXIT_HOOKED = False


def shutdown_stdio() -> None:
    """收掉所有常驻子进程。

    必须有这一步：blocking portal 的线程不是 daemon，会话开着的时候解释器
    退不掉——实测一个脚本取完清单就卡死在那儿不返回。EV 是长驻服务，
    真让它关不掉就成了要 kill -9 的东西。
    """
    with _LOCK:
        items = list(_STDIO.values())
        _STDIO.clear()
    for item in items:
        try:
            with item._lock:
                item._stop_locked()
        except Exception:
            pass


def _stdio_session(server: str, meta: Dict[str, Any]) -> _StdioSession:
    global _ATEXIT_HOOKED
    with _LOCK:
        item = _STDIO.get(server)
        if item is None:
            item = _StdioSession(
                server, meta["command"], meta.get("args") or [], meta.get("env") or {},
            )
            _STDIO[server] = item
        hook = not _ATEXIT_HOOKED
        _ATEXIT_HOOKED = True
    if hook:
        import atexit

        atexit.register(shutdown_stdio)
    return item


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


def _talk(server: str, timeout_s: float, action: str, **kwargs) -> Tuple[Any, str]:
    """跟一个 server 说一次话。上层不必知道它是 http 还是 stdio。

    http：每次新建会话（无状态、便宜）。
    stdio：常驻子进程（npx 冷启动要好几秒，每次重开扛不住反射层的预算）。
    """
    with _LOCK:
        meta = dict(_SERVERS.get(server) or {})
    if not meta:
        return None, "未登记"
    if meta.get("command"):
        return _stdio_session(server, meta).call(timeout_s, action, **kwargs)
    return _call_blocking(meta["url"], timeout_s, action, **kwargs)


def _refresh_catalog(server: str) -> None:
    """去取一次清单并写回缓存。只在后台线程里跑。

    已经有人在取时直接返回。想等那一次的结果，用 _wait_refresh。
    """
    with _LOCK:
        meta = _SERVERS.get(server)
        if not meta or meta.get("refreshing"):
            return
        meta["refreshing"] = True
        meta["refresh_done"] = threading.Event()
        timeout_s = meta["timeout_s"]
    tools, error = _talk(server, timeout_s, "list")
    with _LOCK:
        meta = _SERVERS.get(server)
        if not meta:
            return
        meta["refreshing"] = False
        done = meta.get("refresh_done")
        if done is not None:
            done.set()
        if error:
            meta["reachable"] = False
            meta["error"] = error
            meta["cooldown_until"] = _now() + UNREACHABLE_COOLDOWN_S
            return
        meta["tools"] = tools or []
        meta["tools_at"] = _now()
        meta["reachable"] = True
        meta["error"] = ""
        meta["cooldown_until"] = 0.0


def _catalog(server: str, *, allow_blocking: bool = False) -> Tuple[List[Dict[str, Any]], str]:
    """拿工具清单。默认永远不在这里等网络。

    清单每轮语音都要用（世界现状、参数形状都读它）。要是在这儿现取，
    一次往返就砸在那个倒霉的回合上——实测一个连不上设备的 MCP 单次往返
    2.5 秒，而一轮语音的总预算才 1.5 秒。
    所以：过期了就把旧的先给出去，同时在后台线程去取新的，下一轮自然是新的。

    allow_blocking 只给 invoke 用：那是用户主动发起的动作，冷缓存时宁可等一下，
    也不能误报「没有这个工具」——清单还没取回来就说工具不存在，是把自己的时序
    问题说成对方的能力缺失。
    """
    with _LOCK:
        meta = _SERVERS.get(server)
        if not meta:
            return [], "未登记"
        now = _now()
        fresh = meta["tools"] is not None and now - meta["tools_at"] < CATALOG_TTL_S
        cooling = now < meta["cooldown_until"]
        cached = list(meta["tools"] or [])
        error = "" if fresh else (meta["error"] or "")
        need = not fresh and not cooling and not meta.get("refreshing")
        never_fetched = meta["tools"] is None
    if allow_blocking and never_fetched and not cooling:
        # 已经有人在取（多半是启动时的预热）就等那一次，别自己再发一次也别
        # 空手而归——空手而归就是「没有这个工具」，用户会以为装的 MCP 坏了。
        with _LOCK:
            meta = _SERVERS.get(server) or {}
            pending = meta.get("refresh_done") if meta.get("refreshing") else None
            wait_s = float(meta.get("timeout_s") or CALL_TIMEOUT_S)
        if pending is not None:
            pending.wait(wait_s)
        else:
            _refresh_catalog(server)
        with _LOCK:
            meta = _SERVERS.get(server) or {}
            return list(meta.get("tools") or []), str(meta.get("error") or "")
    if need:
        threading.Thread(
            target=_refresh_catalog, args=(server,), daemon=True,
            name="mcp-catalog-%s" % server,
        ).start()
    return cached, error


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
    hidden = len(tools) - len(_voice_visible(server, tools))
    tools = _voice_visible(server, tools)
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
            # 有多少工具留给了工作 Agent。如实说出来，模型才知道
            # 「这个服务还有别的能力，只是不归我管」，不会去猜命令名。
            "work_only": hidden,
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
    # 动作路径允许等：冷缓存时误报「没有这个工具」比多等一下糟得多。
    tools, error = _catalog(server, allow_blocking=True)
    visible = _voice_visible(server, tools)
    known = {item["name"] for item in visible}
    if command not in known:
        exists = command in {item["name"] for item in tools}
        return {
            "ok": False,
            # 存在但没分给语音，和压根没有，是两回事。说成「没有」会让模型
            # 换个名字接着猜；说清楚它归工作 Agent，模型才知道该交出去。
            "error": (
                "%s 的 %s 属于工作 Agent，不在语音这一层" % (server, command)
                if exists else "%s 没有 %s 这个工具" % (server, command)
            ),
            "available": sorted(known)[:20],
            "detail": error,
        }

    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    value, error = _talk(server, meta["timeout_s"], "call", tool=command, args=args)
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
    text = str((value or {}).get("text") or "")
    return {
        "ok": bool((value or {}).get("ok")),
        "changed": bool((value or {}).get("ok")),
        "target_id": target,
        "target_name": server,
        "command": command,
        # 工具自己说的那句就是「现在什么样」，填 display 而不是 after：
        # 注册表统一算 after，provider 报了 display 才用它，否则会回头重查
        # 对象目录、把「reachable=是、tools=5」这种状态摘要当成结果播出去。
        "display": " ".join(text.split())[:120],
        # 完整输出给模型看（页面 id、网址、控制台内容都在这儿），
        # 播报只用上面那句短的。
        "text": text[:1200],
    }


_REGISTERED = False

# 装 MCP 就是往这个文件里加一行。放在 EV 自己的 data/ 下而不是核心引擎那份
# .mcp_server_settings.json：那份是核心引擎的对话链路在用，两边读一份会互相
# 牵制——核心引擎装的未必适合语音（语音一轮预算 1.5 秒），反过来也一样。
CONFIG_NAME = "mcp_servers.json"


def config_path():
    from common.paths import MUSE_DIR

    return MUSE_DIR / "data" / CONFIG_NAME


def load_config() -> Dict[str, Any]:
    """读配置。文件不在、写坏了都当成「没有 MCP」，不让它挡住启动。"""
    try:
        path = config_path()
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    servers = raw.get("mcpServers") if isinstance(raw.get("mcpServers"), dict) else raw
    return servers if isinstance(servers, dict) else {}


def load_from_config() -> List[str]:
    """按配置登记。返回真正登记上的名字。

    url 与 command 两种形式都认。
    """
    loaded = []
    for name, spec in (load_config() or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        if spec.get("enabled") is False:
            continue
        url = str(spec.get("url") or "").strip()
        command = str(spec.get("command") or "").strip()
        if not url and not command:
            continue
        register_server(
            name,
            url,
            command=command,
            args=spec.get("args") or [],
            env=spec.get("env") or {},
            timeout_s=float(spec.get("timeout_s") or CALL_TIMEOUT_S),
            voice_tools=spec.get("voice_tools") or [],
            exclude=spec.get("exclude") or [],
        )
        loaded.append(str(name))
    return loaded


def _warm_and_report(server: str) -> None:
    """预热清单，并把全量工具名打出来。

    白名单得照着写，不知道有什么就写不出来。所以日志给全量，
    并标出哪些没进语音层。
    """
    _refresh_catalog(server)
    tools, _ = _catalog(server)
    if not tools:
        return
    visible = {t["name"] for t in _voice_visible(server, tools)}
    parts = [
        ("%s" % t["name"]) if t["name"] in visible else ("%s(工作层)" % t["name"])
        for t in tools
    ]
    print("[muse] MCP %s 的工具：%s" % (server, "、".join(parts)), flush=True)


def ensure_provider() -> None:
    """把桥挂进对象注册表并按配置登记。

    纯加法：配置文件不存在时，桥什么都不产出，对现有行为零影响。
    """
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
    names = load_from_config()
    if names:
        print("[muse] MCP 桥已登记：%s" % "、".join(names), flush=True)
        # 启动时先在后台把清单取回来。不预热的话，第一轮语音会拿到空清单——
        # 对象在、命令是空的，模型看着像个坏掉的能力。
        for name in names:
            threading.Thread(
                target=_warm_and_report, args=(name,), daemon=True,
                name="mcp-warm-%s" % name,
            ).start()
