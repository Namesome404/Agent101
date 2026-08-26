# -*- coding: utf-8 -*-
"""把最近的搜索结果暴露成可操作对象：result.1 / result.2 …

模型引用序号即可打开，不需要（也拿不到）真实 URL——URL 只留在服务端。
这样既保住「弱证据不把链接交给模型」那条既有保证，又让「把那个链接打开」
真的能打开正确的东西，而不是让模型凭记忆编一个 BV 号出来。

纯新增 provider：不修改任何既有 provider 与工具 schema。
"""
from __future__ import annotations

from typing import Any, Dict, List

from control_plane import search_results
from control_plane.object_registry import object_registry

_REGISTERED = False
_PREFIX = "result."


def _discover() -> List[Dict[str, Any]]:
    snapshot = search_results.snapshot()
    objects: List[Dict[str, Any]] = []
    for index, item in enumerate(snapshot.get("items") or [], 1):
        objects.append({
            "target_id": "%s%d" % (_PREFIX, index),
            # 名称即标题，模型据此判断该开哪条；URL 刻意不放进描述符
            "name": item.get("title") or item.get("site") or "搜索结果 %d" % index,
            "kind": "search_result",
            "owner": "search",
            "description": "%s%s" % (
                ("来源 %s。" % item["site"]) if item.get("site") else "",
                item.get("snippet") or "",
            )[:300],
            "aliases": ["第%d条" % index, "结果%d" % index],
            "properties": {
                "index": index,
                "site": item.get("site") or "",
                "query": snapshot.get("query") or "",
            },
            "commands": ["open"],
        })
    return objects


def _execute(op: str, target: str, payload: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    if not target.startswith(_PREFIX):
        return {"ok": False, "reason": "unsupported_target"}
    try:
        index = int(target[len(_PREFIX):])
    except ValueError:
        return {"ok": False, "reason": "bad_result_index"}
    item = search_results.get(index)
    if not item:
        return {
            "ok": False,
            "reason": "result_not_found",
            "detail": "本轮搜索没有第 %d 条结果；先 inspect result.* 看看有哪些。" % index,
        }
    if op == "inspect":
        return {"ok": True, "target_id": target, "state": dict(item)}
    if op != "invoke":
        return {"ok": False, "reason": "search_result_is_read_only"}
    command = str(payload.get("command") or "open").strip().lower()
    if command not in ("open", "show", "create"):
        return {"ok": False, "reason": "unsupported_command"}

    # 用服务端保存的真实 URL 打开，模型全程没碰过这个字符串
    from tools import surface_control

    text, meta = surface_control.execute({
        "action": "create",
        "url": item["url"],
        "title": item.get("title") or item.get("site") or "",
        "continue_after": False,
        "reply": "",
    })
    return {
        "ok": bool(meta.get("ok")),
        "target_id": target,
        "opened_url": item["url"],
        "title": item.get("title") or "",
        "detail": text,
    }


def ensure_provider() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    object_registry.register_provider(
        "ev.search.results",
        discover=_discover,
        execute=_execute,
        target_prefixes=(_PREFIX,),
    )
    _REGISTERED = True
