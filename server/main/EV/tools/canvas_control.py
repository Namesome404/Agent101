# -*- coding: utf-8 -*-
"""Small, reliable control surface for the research canvas."""
from __future__ import annotations

from typing import Any, Dict, Tuple


def tool_definition():
    return {
        "type": "function",
        "function": {
            "name": "canvas_control",
            "description": (
                "查看或修改研究画布。先 inspect 获取 rev 和节点ID；apply 时原样回传 base_rev。"
                "打开/放大图片：focus_id=image-1、selected_id=image-1、zoom=2。字段都是顶层字段。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["inspect", "apply"]},
                    "tab_id": {"type": "string"},
                    "base_rev": {"type": "integer", "minimum": 1},
                    "focus_id": {"type": "string", "description": "聚焦节点ID；空字符串关闭预览。"},
                    "selected_id": {"type": "string"},
                    "zoom": {"type": "number", "minimum": 0.25, "maximum": 6},
                    "fit": {"type": "string", "enum": ["contain", "cover", "width", "actual"]},
                    "fullscreen": {"type": "boolean"},
                    "expanded": {"type": "boolean"},
                    "active_tab_id": {"type": "string"},
                    "close_tab_id": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    }


def _escape_pointer(value: Any) -> str:
    return str(value or "").replace("~", "~0").replace("/", "~1")


def _invalid(detail: str, snapshot: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    result = dict(snapshot or {})
    result.update({"ok": False, "action": "apply", "error": "invalid_changes", "detail": detail})
    return "研究画布没有修改：%s。" % detail, result


def _first(arguments: Dict[str, Any], *names: str):
    for name in names:
        if name in arguments and arguments.get(name) is not None:
            return arguments.get(name)
    return None


def _compile_patches(arguments: Dict[str, Any], inspected: Dict[str, Any]):
    """Compile flat fields and tolerate common model naming variants."""
    document = inspected.get("document") or {}
    node_ids = set((document.get("nodes") or {}).keys())
    tabs = {str(item.get("id") or "") for item in (inspected.get("tabs") or [])}
    patches = []

    focus = _first(
        arguments, "focus_id", "focus", "focus_node", "focusNode",
        "node", "node_id", "nodeId", "target",
    )
    if focus is not None:
        focus = str(focus or "")
        if focus and focus not in node_ids:
            raise ValueError("节点不存在：%s" % focus)
        patches.append({"op": "replace", "path": "/view/focus_id", "value": focus})

    selected = _first(arguments, "selected_id", "selected")
    if selected is None and focus:
        selected = focus
    if selected is not None:
        selected = str(selected or "")
        if selected and selected not in node_ids:
            raise ValueError("节点不存在：%s" % selected)
        patches.append({"op": "replace", "path": "/view/selected_id", "value": selected})

    zoom = arguments.get("zoom")
    inferred_fit = None
    if isinstance(zoom, str) and zoom.lower() in {"fill", "large", "enlarge", "放大"}:
        zoom, inferred_fit = 2.0, "contain"
    if zoom is not None:
        try:
            zoom = float(zoom)
        except (TypeError, ValueError):
            raise ValueError("zoom 必须是 0.25 到 6 的数字")
        if not .25 <= zoom <= 6:
            raise ValueError("zoom 必须在 0.25 到 6 之间")
        patches.append({"op": "replace", "path": "/view/zoom", "value": zoom})

    fit = arguments.get("fit") or inferred_fit
    if fit is not None:
        if fit not in {"contain", "cover", "width", "actual"}:
            raise ValueError("fit 值无效")
        patches.append({"op": "replace", "path": "/view/fit", "value": fit})
    if "fullscreen" in arguments:
        if not isinstance(arguments["fullscreen"], bool):
            raise ValueError("fullscreen 必须是布尔值")
        patches.append({"op": "replace", "path": "/view/fullscreen", "value": arguments["fullscreen"]})

    workspace_fields = any(
        key in arguments for key in ("expanded", "active_tab_id", "close_tab_id")
    )
    if workspace_fields and patches:
        raise ValueError("标签操作不能与图片视图操作混在同一次 apply")
    if "expanded" in arguments:
        if not isinstance(arguments["expanded"], bool):
            raise ValueError("expanded 必须是布尔值")
        patches.append({"op": "replace", "path": "/expanded", "value": arguments["expanded"]})
    if "active_tab_id" in arguments:
        target = str(arguments.get("active_tab_id") or "")
        if target not in tabs:
            raise ValueError("标签页不存在：%s" % target)
        patches.append({"op": "replace", "path": "/active_tab_id", "value": target})
    if "close_tab_id" in arguments:
        target = str(arguments.get("close_tab_id") or "")
        if target not in tabs:
            raise ValueError("标签页不存在：%s" % target)
        patches.append({"op": "remove", "path": "/tabs/%s" % _escape_pointer(target)})
    if not patches:
        raise ValueError("apply 没有有效修改字段")
    return patches


def execute(arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    from control_plane import info_panel

    args = dict(arguments or {})
    action = str(args.get("action") or "").lower()
    if action == "inspect":
        result = info_panel.inspect(str(args.get("tab_id") or ""))
        if not result.get("ok"):
            return "当前没有可操作的研究画布。", result
        nodes = (result.get("document") or {}).get("nodes") or {}
        result["example"] = {
            "action": "apply", "base_rev": result.get("rev"),
            "focus_id": "image-1", "selected_id": "image-1", "zoom": 2,
        }
        result["action"] = "inspect"
        node_summary = [
            "%s:%s" % (node_id, (node or {}).get("type") or "unknown")
            for node_id, node in nodes.items()
        ]
        return (
            "画布 rev=%s；节点：%s。放大第一张图片就 apply base_rev=%s, focus_id=image-1, zoom=2。"
            % (result.get("rev"), "、".join(node_summary) or "无", result.get("rev")),
            result,
        )
    if action == "apply":
        inspected = info_panel.inspect(str(args.get("tab_id") or ""))
        if not inspected.get("ok"):
            return "当前没有可操作的研究画布。", inspected
        try:
            base_rev = int(args.get("base_rev") or 0)
        except (TypeError, ValueError):
            base_rev = 0
        if base_rev <= 0:
            return _invalid("缺少最近 inspect 返回的 base_rev", inspected)
        # One-release tolerance for an already generated nested call.
        nested = args.get("changes") if isinstance(args.get("changes"), dict) else {}
        view = nested.get("view") if isinstance(nested.get("view"), dict) else {}
        workspace = nested.get("workspace") if isinstance(nested.get("workspace"), dict) else {}
        flat = {**view, **workspace, **args}
        flat.pop("changes", None)
        try:
            patches = _compile_patches(flat, inspected)
        except ValueError as error:
            return _invalid(str(error), inspected)
        result = info_panel.apply(
            patches=patches,
            tab_id=str(args.get("tab_id") or ""),
            base_rev=base_rev,
        )
        result["action"] = "apply"
        if result.get("ok"):
            try:
                from devices.coding.surface_tools import sync_status_timeline_to_canvas
                sync_status_timeline_to_canvas()
            except Exception:
                pass
            if result.get("changed"):
                return "研究画布已更新。", result
            return "画布已经是这个状态。", result
        if result.get("error") == "revision_conflict":
            return "画布已经变化，请重新 inspect。", result
        return "研究画布修改失败：%s。" % (result.get("detail") or result.get("error") or "未知错误"), result
    return "canvas_control 缺少有效 action。", {
        "ok": False, "action": action, "error": "unknown_action",
    }


def register(registry, wrapper=None):
    def fn(args, ctx):
        del ctx
        return execute(args)

    final = wrapper(fn, "canvas_control") if wrapper else fn
    registry.register("canvas_control", final, conflicts="research_canvas")
