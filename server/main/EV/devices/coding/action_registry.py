# -*- coding: utf-8 -*-
"""动作流注册表：动作名 → 执行器 + 冲突域。

动作流范式下模型每轮输出若干动作，程序按注册表查表执行并返回回执：

- 执行器 = 注册表加一行（本地函数或未来的 MCP 工具），程序不猜语义只查表。
- 冲突域：conflicts=None 的动作可并发；conflicts="key" 按 args[key] 分组，
  同组严格按流内顺序串行（保住 create→open 的先后关系）。
- 回执是唯一真相：每个动作返回 {ok, result, meta}，无 ok:true 即视为未完成。

线程安全：register 与执行互不干扰，run_batch 用线程池并发执行无冲突组。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

Executor = Callable[[Dict[str, Any], Dict[str, Any]], Any]


class ActionRegistry:
    """动作名到执行器的注册表，支持冲突域分组并行执行。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._actions: Dict[str, Dict[str, Any]] = {}
        self._aliases: Dict[str, str] = {}

    def register(
        self,
        name: str,
        fn: Executor,
        *,
        conflicts: Any = None,
        aliases: Optional[List[str]] = None,
    ) -> None:
        """注册一个动作。

        fn(args, ctx) 返回 (text, meta)；meta 的 ok 字段即回执。
        conflicts：None 表示可并行；字符串按 args[conflicts] 分组；callable 自定义。
        aliases：模型常见的幻觉变体名，执行时归一化到本动作，避免"改不生效"。
        """
        with self._lock:
            self._actions[name] = {"fn": fn, "conflicts": conflicts}
            for alias in aliases or []:
                self._aliases[str(alias)] = name

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._actions or name in self._aliases

    def resolve(self, name: str) -> Optional[str]:
        """把模型幻觉变体归一化到注册名；未注册时返回 None。"""
        if not isinstance(name, str):
            return None
        with self._lock:
            return self._actions.get(name) and name or self._aliases.get(name)

    def names(self) -> List[str]:
        with self._lock:
            return list(self._actions)

    def _conflict_key(self, spec: Any, args: Dict[str, Any]) -> Any:
        if spec is None:
            return None
        if callable(spec):
            try:
                return spec(args)
            except Exception:
                return ("conflict_error",)
        if isinstance(spec, str):
            return (spec, args.get(spec))
        return (str(spec), args)

    def exec_action(
        self,
        name: str,
        args: Optional[Dict[str, Any]],
        ctx: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """执行单个动作，返回 {ok, result, meta, error}。异常不抛出，转为失败回执。"""
        # 工具参数解析失败：把详细错误（含原始内容）回显给模型，让它重试。
        if isinstance(args, dict) and args.get("__parse_error__"):
            detail = str(args.get("__parse_error__"))[:1000]
            return {
                "ok": False, "error": "tool_arguments_parse_error",
                "detail": detail, "name": name,
                "result": "（工具参数解析失败）%s" % detail,
                "meta": {"ok": False, "action": "", "reason": "parse_error", "detail": detail},
            }
        canonical = self.resolve(name)
        if canonical is None:
            return {
                "ok": False, "error": "unknown_action", "name": name,
                "detail": "未注册的动作：%s（可选动作：%s）" % (name, ", ".join(self.names())),
            }
        with self._lock:
            spec = self._actions.get(canonical)
        ctx = ctx if isinstance(ctx, dict) else {}
        try:
            text, meta = spec["fn"](args if isinstance(args, dict) else {}, ctx)
        except Exception as exc:  # noqa: BLE001 - 动作失败必须转回执，不能中断整批
            return {
                "ok": False,
                "error": "tool_execution_exception",
                "detail": str(exc)[:1000],
                "name": name,
            }
        if not isinstance(meta, dict) or "ok" not in meta:
            return {
                "ok": False,
                "error": "invalid_action_receipt",
                "detail": "动作执行器必须显式返回 meta.ok",
                "name": name,
                "result": str(text)[:2000],
                "meta": {
                    "ok": False,
                    "name": canonical,
                    "reason": "missing_explicit_ok",
                },
            }
        meta["name"] = canonical
        if canonical != name:
            meta["aliased_from"] = name
        return {"ok": bool(meta.get("ok")), "result": text, "meta": meta}

    def run_batch(
        self,
        actions: List[Dict[str, Any]],
        ctx: Optional[Dict[str, Any]] = None,
        *,
        max_workers: int = 4,
    ) -> List[Dict[str, Any]]:
        """按冲突域分组执行一组动作。

        actions 保持流内顺序；返回与原顺序一致的执行结果列表。
        无冲突动作并发，同冲突域按序串行。
        """
        actions = list(actions or [])
        n = len(actions)
        results: List[Optional[Dict[str, Any]]] = [None] * n

        groups: List[List[int]] = []
        group_keys: List[Any] = []
        key_to_group: Dict[Any, int] = {}
        for idx, act in enumerate(actions):
            name = str((act or {}).get("action") or "")
            args = (act or {}).get("args") if isinstance((act or {}).get("args"), dict) else {}
            canonical = self.resolve(name)
            with self._lock:
                spec = self._actions.get(canonical) if canonical else None
            if spec is None:
                key = ("unknown", name)
            else:
                key = self._conflict_key(spec.get("conflicts"), args)
            if key is None:
                groups.append([idx])
                group_keys.append(None)
                continue
            known = key_to_group.get(key)
            if known is None:
                key_to_group[key] = len(groups)
                groups.append([idx])
                group_keys.append(key)
            else:
                groups[known].append(idx)

        def run_one(idx: int) -> None:
            act = actions[idx]
            results[idx] = self.exec_action(
                str((act or {}).get("action") or ""),
                (act or {}).get("args"),
                ctx=ctx,
            )

        def run_serial(indices: List[int]) -> None:
            for idx in indices:
                run_one(idx)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = []
            for gi, grp in enumerate(groups):
                if len(grp) == 1 and group_keys[gi] is None:
                    futures.append(pool.submit(run_one, grp[0]))
                else:
                    futures.append(pool.submit(run_serial, grp))
            for fut in futures:
                fut.result()
        return results


action_registry = ActionRegistry()
