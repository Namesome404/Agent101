# -*- coding: utf-8 -*-
"""Dynamic object capability registry behind a constant model-facing protocol.

Providers may add objects, properties and commands at runtime.  None of those
descriptors are copied into the function schema; the model discovers only the
objects relevant to the current request through ``inspect``.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional


DiscoverFn = Callable[[], Iterable[Dict[str, Any]]]
ExecuteFn = Callable[[str, str, Dict[str, Any], Dict[str, Any]], Any]


@dataclass(frozen=True)
class _Provider:
    name: str
    discover: DiscoverFn
    execute: ExecuteFn
    target_prefixes: tuple[str, ...]


# 只读命令：调它们不算变更，别把查询计成动作。
_READ_ONLY_COMMANDS = {"status", "inspect", "get", "read", "list"}


def _display_of(item: Dict[str, Any]) -> str:
    """对象现状的一句人话：provider 给了 display 就用它，否则退回紧凑状态。

    和【世界现状】用的是同一套词汇——模型调用前读到的、回执里拿到的、
    最后播报出去的，必须是同一种说法，否则无从核对。
    """
    if not isinstance(item, dict):
        return ""
    display = str(item.get("display") or "").strip()
    if display:
        return display[:120]
    state = item.get("state") if isinstance(item.get("state"), dict) else {}
    bits = []
    for key, value in list(state.items())[:6]:
        if isinstance(value, bool):
            bits.append("%s=%s" % (key, "是" if value else "否"))
        elif isinstance(value, (int, float)):
            bits.append("%s=%s" % (key, value))
    return "、".join(bits)[:120]


def _public_view(item: Dict[str, Any]) -> Dict[str, Any]:
    """给模型看的描述符：去掉 provider 与 adjustable 的服务端接线字段。"""
    view = dict(item)
    view.pop("_provider", None)
    adjustable = view.get("adjustable")
    if isinstance(adjustable, dict):
        view["adjustable"] = {
            name: {
                key: value for key, value in (spec or {}).items()
                if key in ("min", "max", "step", "unit", "label")
            }
            for name, spec in adjustable.items()
            if isinstance(spec, dict)
        }
    return view


class ObjectCapabilityRegistry:
    """Runtime object catalog with stable IDs and provider-owned validation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._providers: Dict[str, _Provider] = {}
        self._target_provider: Dict[str, str] = {}

    def register_provider(
        self,
        name: str,
        *,
        discover: DiscoverFn,
        execute: ExecuteFn,
        target_prefixes: Iterable[str] = (),
    ) -> None:
        provider_name = str(name or "").strip()
        if not provider_name:
            raise ValueError("provider name 不能为空")
        if not callable(discover) or not callable(execute):
            raise TypeError("discover 和 execute 必须可调用")
        prefixes = tuple(
            str(value or "").strip().lower()
            for value in target_prefixes
            if str(value or "").strip()
        )
        with self._lock:
            self._providers[provider_name] = _Provider(
                provider_name, discover, execute, prefixes,
            )
            self._target_provider = {
                target: owner for target, owner in self._target_provider.items()
                if owner != provider_name
            }

    def unregister_provider(self, name: str) -> bool:
        with self._lock:
            provider_name = str(name or "").strip()
            removed = self._providers.pop(provider_name, None) is not None
            if removed:
                self._target_provider = {
                    target: owner for target, owner in self._target_provider.items()
                    if owner != provider_name
                }
            return removed

    @staticmethod
    def _public_descriptor(raw: Dict[str, Any], provider: str) -> Dict[str, Any]:
        target_id = str(raw.get("target_id") or "").strip()
        if not target_id:
            return {}
        aliases = [
            str(item).strip() for item in list(raw.get("aliases") or [])
            if str(item).strip()
        ]
        return {
            "target_id": target_id,
            "name": str(raw.get("name") or target_id)[:120],
            "kind": str(raw.get("kind") or "object")[:80],
            "owner": str(raw.get("owner") or "system")[:80],
            "description": str(raw.get("description") or "")[:300],
            "aliases": aliases[:20],
            "properties": copy.deepcopy(raw.get("properties") or {}),
            "commands": copy.deepcopy(raw.get("commands") or []),
            # 命令的参数形状属于对象契约：只给命令名，调用方只能靠报错试出
            # 参数怎么写。任何 provider（含以后接进来的 MCP 能力）都能带上。
            "command_args": copy.deepcopy(raw.get("command_args") or {}),
            # 可调数值属性的量纲：谁声明了，谁就自动获得相对调整能力（op=adjust）。
            # 「暗一点/大一点/往左挪挪/再等五分钟」是同一类指令，不该每个对象
            # 各写一遍提示词，更不该让模型自己做算术。
            "adjustable": copy.deepcopy(raw.get("adjustable") or {}),
            # provider 自渲染的一句现状（可选）：格式化知识留在懂它的那一层
            "display": str(raw.get("display") or "")[:120],
            "state": copy.deepcopy(raw.get("state") or {}),
            "rev": raw.get("rev"),
            "_provider": provider,
        }

    def _discover_provider(self, provider: _Provider) -> list:
        objects = []
        try:
            discovered = provider.discover() or []
        except Exception:
            return objects
        for raw in discovered:
            if not isinstance(raw, dict):
                continue
            item = self._public_descriptor(raw, provider.name)
            target_id = str(item.get("target_id") or "")
            if not target_id:
                continue
            objects.append(item)
            with self._lock:
                if provider.name in self._providers:
                    self._target_provider[target_id.lower()] = provider.name
        return objects

    def _catalog(self) -> list:
        with self._lock:
            providers = list(self._providers.values())
        objects = []
        seen = set()
        for provider in providers:
            for item in self._discover_provider(provider):
                target_id = item.get("target_id")
                if not target_id or target_id in seen:
                    continue
                seen.add(target_id)
                objects.append(item)
        return objects

    def _resolve(self, target: str) -> list:
        """Resolve an exact target without scanning unrelated providers.

        A stable target ID is routed through its cached provider or registered
        namespace. Aliases intentionally fall back to catalog discovery because
        they can be ambiguous. This keeps normal apply/invoke latency independent
        of how many other skills are installed.
        """
        requested = str(target or "").strip()
        if not requested:
            return []
        lowered = requested.lower()
        with self._lock:
            providers = list(self._providers.values())
            cached_name = self._target_provider.get(lowered)
            cached = self._providers.get(cached_name) if cached_name else None
        candidates = [cached] if cached else []
        if not candidates:
            prefix_matches = [
                (len(prefix), provider)
                for provider in providers
                for prefix in provider.target_prefixes
                if lowered.startswith(prefix)
            ]
            if prefix_matches:
                longest = max(length for length, _provider in prefix_matches)
                candidates = [
                    provider for length, provider in prefix_matches
                    if length == longest
                ]
        restricted = bool(candidates)
        if not candidates:
            candidates = providers
        matches = []
        for provider in candidates:
            for item in self._discover_provider(provider):
                aliases = {str(alias).lower() for alias in item.get("aliases") or []}
                if str(item.get("target_id") or "").lower() == lowered or lowered in aliases:
                    matches.append(item)
        if not matches and restricted:
            # A dynamic object may have moved providers or been removed. Clear a
            # stale cache and try aliases across the catalog before failing.
            with self._lock:
                self._target_provider.pop(lowered, None)
            for item in self._catalog():
                aliases = {str(alias).lower() for alias in item.get("aliases") or []}
                if str(item.get("target_id") or "").lower() == lowered or lowered in aliases:
                    matches.append(item)
        return matches

    @staticmethod
    def _matches_selector(item: Dict[str, Any], selector: Dict[str, Any]) -> bool:
        kind = str(selector.get("kind") or "").strip().lower()
        owner = str(selector.get("owner") or "").strip().lower()
        if kind and str(item.get("kind") or "").lower() != kind:
            return False
        if owner and str(item.get("owner") or "").lower() != owner:
            return False
        query = " ".join(str(selector.get("query") or "").lower().split())
        if not query:
            return True
        haystack = " ".join([
            str(item.get("target_id") or ""),
            str(item.get("name") or ""),
            str(item.get("kind") or ""),
            str(item.get("description") or ""),
            *[str(value) for value in item.get("aliases") or []],
        ]).lower()
        compact_query = "".join(query.split())
        compact_haystack = "".join(haystack.split())
        return query in haystack or compact_query in compact_haystack

    def inspect(self, target: str = "", selector: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        requested = str(target or "").strip()
        if requested:
            matches = self._resolve(requested)
            if len(matches) == 1:
                return {"ok": True, "op": "inspect", "object": _public_view(matches[0])}
            return {
                "ok": False,
                "op": "inspect",
                "reason": "target_not_found" if not matches else "ambiguous_target",
                "target": requested,
            }
        catalog = self._catalog()
        matches = [
            item for item in catalog
            if self._matches_selector(item, selector or {})
        ]
        scoped = matches[:12]
        public = []
        for item in scoped:
            clean = dict(item)
            clean.pop("_provider", None)
            # Catalog discovery stays compact. Exact inspect returns full state.
            clean.pop("state", None)
            public.append(clean)
        return {
            "ok": True,
            "op": "inspect",
            "objects": public,
            "count": len(public),
            "truncated": len(matches) > len(scoped),
        }


    _AMOUNTS = {"small": 1, "medium": 2, "large": 4}

    @staticmethod
    def _read_state_value(state: Dict[str, Any], path):
        node: Any = state
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node if isinstance(node, (int, float)) and not isinstance(node, bool) else None

    def _adjust(self, target: str, payload: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """相对调整：服务端读当前值再算，模型只说方向和幅度。

        真实事故：灯在 40%，用户说「稍微暗一点」，模型给了 60（更亮）；再说
        「更暗一点」，它给 40（回到原点）。当前值明明就在提示里，但让快模型
        在 2 秒预算内做算术并不可靠——而且这不是灯的问题，窗口大小、面板高度、
        计时时长、播放速度都是同一类。算术留在服务端，模型只声明意图。
        """
        matches = self._resolve(target)
        if len(matches) != 1:
            return {
                "ok": False, "op": "adjust",
                "reason": "target_not_found" if not matches else "ambiguous_target",
                "target": str(target or "").strip(),
            }
        internal = matches[0]
        prop = str(payload.get("property") or "").strip()
        spec = (internal.get("adjustable") or {}).get(prop)
        if not isinstance(spec, dict):
            return {
                "ok": False, "op": "adjust", "target_id": internal.get("target_id"),
                "reason": "property_not_adjustable",
                "detail": "%s 可相对调整的属性：%s" % (
                    internal.get("target_id"),
                    "、".join(sorted(internal.get("adjustable") or {})) or "（无）",
                ),
            }
        read_path = spec.get("read") or [prop]
        current = self._read_state_value(internal.get("state") or {}, read_path)
        if current is None:
            # 状态是进程内缓存，重启后为空——这时不该直接放弃：对象自己有读取
            # 能力（设备的 status 这类）就先去读一次真值，再算。否则「重启后
            # 第一次说暗一点」永远失败，而这恰恰是最常见的时刻。
            refresh = str(spec.get("refresh") or "")
            if not refresh and "status" in (internal.get("commands") or []):
                refresh = "status"
            if refresh:
                self.execute("invoke", str(internal["target_id"]), {"command": refresh}, ctx)
                repeat = self._resolve(target)
                if len(repeat) == 1:
                    internal = repeat[0]
                    current = self._read_state_value(internal.get("state") or {}, read_path)
        if current is None:
            return {
                "ok": False, "op": "adjust", "target_id": internal.get("target_id"),
                "reason": "current_value_unknown",
                "detail": "还不知道 %s 当前的 %s，先 inspect 或执行一次查询。" % (
                    internal.get("target_id"), prop,
                ),
            }
        direction = -1 if str(payload.get("direction") or "").lower() in ("down", "less", "小", "低") else 1
        multiplier = self._AMOUNTS.get(str(payload.get("amount") or "medium").lower(), 2)
        step = float(spec.get("step") or 1)
        low = float(spec.get("min", current))
        high = float(spec.get("max", current))
        after = max(low, min(high, float(current) + direction * step * multiplier))
        after = int(round(after)) if float(current).is_integer() and step.is_integer() else round(after, 2)
        if after == current:
            return {
                "ok": True, "op": "adjust", "changed": False,
                "target_id": internal.get("target_id"),
                "target_name": internal.get("name"),
                "property": prop, "before_value": current, "after_value": after,
                # 到顶/到底是模型事先写不出来的结果：它预写的 say 只会说
                # 「调暗一点了」，而实际什么都没变。这句必须由服务端说，
                # speech_fixed 让 object_control 别把它当模子话抹掉。
                "speech": "%s已经到%s了" % (
                    internal.get("name") or "对象",
                    "最大" if direction > 0 else "最小",
                ),
                "speech_fixed": True,
            }
        via = spec.get("via") if isinstance(spec.get("via"), dict) else {}
        write_op = str(via.get("op") or "apply")
        if write_op == "invoke":
            write_payload = {
                "command": str(via.get("command") or prop),
                "args": {str(via.get("arg") or prop): after},
            }
        else:
            patch: Dict[str, Any] = {}
            node = patch
            path = list(via.get("path") or [prop])
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = after
            write_payload = {"patch": patch, "base_rev": payload.get("base_rev")}
        result = self.execute(write_op, str(internal["target_id"]), write_payload, ctx)
        result = dict(result or {})
        result["op"] = "adjust"
        result["property"] = prop
        result["before_value"] = current
        result["after_value"] = after
        result["changed"] = bool(result.get("ok"))
        if result.get("ok"):
            unit = str(spec.get("unit") or "")
            # 播报说人话：前后值留在回执里给面板和后续核对用，嘴上只说结果。
            # 相对调节的落点是服务端算出来的，模型预写的 say 不许带数值，
            # 所以这句由服务端给，带上真实结果。
            result["speech"] = "%s%s调到%s%s了" % (
                internal.get("name") or "对象",
                str(spec.get("label") or prop), after, unit,
            )
            result["speech_fixed"] = True
        return result

    def world(self) -> list:
        """服务端用的完整目录：带 state 与量纲。

        给模型的 inspect 列表刻意摘掉了 state（保持目录紧凑），但「世界现状」
        这类投影需要真值。两者分开，避免为了投影把模型侧的目录撑大。
        """
        return [_public_view(item) for item in self._catalog()]

    def execute(
        self,
        op: str,
        target: str,
        payload: Optional[Dict[str, Any]] = None,
        ctx: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        operation = str(op or "").strip().lower()
        if operation == "inspect":
            return self.inspect(target, (payload or {}).get("selector"))
        if operation == "adjust":
            return self._adjust(target, payload or {}, ctx or {})
        if operation not in {"apply", "invoke"}:
            return {"ok": False, "op": operation, "reason": "unknown_operation"}
        matches = self._resolve(target)
        if len(matches) != 1:
            return {
                "ok": False,
                "op": operation,
                "reason": "target_not_found" if not matches else "ambiguous_target",
                "target": str(target or "").strip(),
            }
        internal = matches[0]
        descriptor = dict(internal)
        descriptor.pop("_provider", None)
        provider_name = internal.get("_provider")
        with self._lock:
            provider = self._providers.get(str(provider_name or ""))
        if provider is None:
            return {"ok": False, "op": operation, "reason": "provider_unavailable"}
        before_display = _display_of(internal)
        try:
            raw = provider.execute(
                operation,
                str(descriptor["target_id"]),
                copy.deepcopy(payload or {}),
                dict(ctx or {}),
            )
        except Exception as error:
            return {
                "ok": False,
                "op": operation,
                "target_id": descriptor["target_id"],
                "target_name": descriptor["name"],
                "reason": "provider_exception",
                "detail": str(error)[:1000],
            }
        result = dict(raw or {}) if isinstance(raw, dict) else {}
        result.setdefault("ok", False)
        # 改之前/改之后用同一套人话回灌：播报只需复述 after，不必自己组织说法，
        # 也就不需要「禁止声称已完成」那一堆禁令去防它编。
        if result.get("ok"):
            result.setdefault("before", before_display)
            # provider 自己报了新状态就用它（权威且免费）；没报才回头重查一次目录。
            reported = str(result.get("display") or "").strip()
            if reported:
                result["after"] = reported[:120]
            else:
                after = self._resolve(str(descriptor["target_id"]))
                result["after"] = _display_of(after[0]) if len(after) == 1 else ""
            # 现状真的变了，就是变更动作——证据比默认值可靠。
            # _result_from_legacy 的 changed 默认是 False，于是「灯已调成红色」
            # 「窗口已关闭」这类真动作全被记成没变；空转检测和历史标注都吃这个
            # 信号，一路错下去。只做「假否定→真」的更正，不会造出假肯定。
            if result.get("before") and result.get("after"):
                if result["before"] != result["after"]:
                    result["changed"] = True
            elif operation == "apply" or str(
                (payload or {}).get("command") or ""
            ).lower() not in _READ_ONLY_COMMANDS:
                # 重启后状态缓存是空的，before 无从得知。这时不能因为「比不出差别」
                # 就判成没变——成功执行的非查询命令，默认就是变更。
                result["changed"] = True
        result["op"] = operation
        result["target_id"] = descriptor["target_id"]
        result["target_name"] = descriptor["name"]
        result["target_kind"] = descriptor["kind"]
        result["target_owner"] = descriptor["owner"]
        result["verified_target"] = True
        return result


object_registry = ObjectCapabilityRegistry()
