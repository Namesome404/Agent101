# -*- coding: utf-8 -*-
"""Constant-schema control plane for UI, devices, canvases, apps and skills."""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, Tuple

from control_plane.object_registry import object_registry
from devices.coding.scene_store import scene_store
from devices.coding import surface_tools
from tools import canvas_control, device_control, surface_apps, surface_control


_BUILTINS_REGISTERED = False
_THEME_KEYS = {
    "mode", "workspace", "surface", "surface_2", "surface_3", "line",
    "line_strong", "text", "secondary", "tertiary", "accent",
    "accent_soft", "danger", "dark", "light", "background",
}


def tool_definition():
    """The schema is intentionally independent of registered capabilities."""
    return {
        "type": "function",
        "function": {
            "name": "object_control",
            "description": (
                "统一对象控制。页面、助手自身界面、设备、画布、内置应用和已安装技能都用它；"
                "能力不确定先 inspect，改属性用 apply，执行命令用 invoke；用户说「暗一点/大一点/往左挪挪」这类相对调整用 adjust(target,property,direction,amount)，当前值由服务端读，你不要自己算数值。target 必须是当前对象"
                "记忆或 inspect 返回的稳定ID；不要把助手自身(agent.ui.status)误当实体灯。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["inspect", "apply", "invoke", "adjust"],
                    },
                    "target": {
                        "type": "string",
                        "description": "稳定对象ID；未知时留空并 inspect。",
                    },
                    "selector": {
                        "type": "object",
                        "description": "inspect 过滤条件：kind、owner、query。",
                    },
                    "patch": {
                        "type": "object",
                        "description": "apply 的属性补丁；字段由对象描述符校验。",
                    },
                    "command": {
                        "type": "string",
                        "description": "invoke 命令；来自对象描述符。",
                    },
                    "args": {
                        "type": "object",
                        "description": "invoke 参数；由对象适配器校验。",
                    },
                    "say": {
                        "type": "string",
                        "description": (
                            "做成后对用户说的那句，你自己的措辞、跟对话同语言、别写数值。"
                            "变更类命令这句就是用户会听到的话，服务端不替你编；inspect 填空串。"
                        ),
                    },
                    "property": {
                        "type": "string",
                        "description": "adjust 用：要调的数值属性（见 adjustable）。",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "adjust：调大/调小。",
                    },
                    "amount": {
                        "type": "string",
                        "enum": ["small", "medium", "large"],
                        "description": "adjust：幅度，默认 medium。",
                    },
                    "base_rev": {
                        "type": "integer",
                        "description": "对象要求乐观锁时，传最近 inspect 的 rev。",
                    },
                    "speak_while": {"type": "boolean"},
                    "progress_reply": {"type": "string"},
                    "continue_after": {
                        "type": "boolean",
                        "description": "回执后确有下一独立步骤才 true。",
                    },
                },
                # say 必填。它本来就是「下指令时顺手把话写好」的设计，但之前是
                # 可选的，实测 29 次变更调用里漏写 10 次；漏写就只能等回执回来再
                # 跑一趟模型组织语言（中位 +1460ms），或者播服务端那句模子话。
                # 列进 required 之后，话和指令同一次生成，两笔账都没了。
                "required": ["op", "continue_after", "say"],
            },
        },
    }


def _status_state() -> dict:
    from control_plane import info_panel

    item = scene_store.get(surface_tools.STATUS_TIMELINE_SURFACE) or {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    return {
        "visible": item.get("visible") is not False,
        "expanded": bool(info_panel.snapshot().get("expanded")),
        "theme": surface_tools.status_timeline_theme(),
        "window": copy.deepcopy(data.get("window") or {}),
    }


def _discover_builtin_objects():
    from control_plane import info_panel
    from devices.coding import agent_runtime, project_fsm

    surface_tools.ensure_status_timeline_surface()
    project_state = project_fsm.load(1)
    active_work = agent_runtime.get_active_run() or {}
    objects = [
        {
            "target_id": "agent.ui.status",
            "name": "助手状态栏",
            "kind": "ui",
            "owner": "assistant",
            "description": "助手自己的可见状态与信息推送界面；‘你/你自己/给自己换色’指这个对象。",
            "aliases": ["status-timeline", "assistant.self", "assistant-ui"],
            "properties": {
                "theme": "任意明暗配色令牌",
                "visible": "boolean",
                "expanded": "boolean",
                "window": "几何对象",
            },
            "commands": ["show", "hide", "expand", "collapse"],
            "state": _status_state(),
            "rev": scene_store.rev,
        },
        {
            "target_id": "project.active",
            "name": "当前工程任务",
            "kind": "agent_task",
            "owner": "assistant",
            "description": "EV 当前协作工程；先确认任务，再由后台工作 Agent 修改、检查并返回可验证结果。",
            "aliases": ["work-agent", "coding", "当前项目", "工程任务"],
            "properties": {
                "goal": "string", "cwd": "path", "mode": "external/self_extend", "plan_steps": "string[]",
            },
            "commands": ["plan", "confirm", "update", "status", "cancel", "revert"],
            "state": {
                "phase": project_state.get("phase") or "idle",
                "goal": (project_state.get("brief") or {}).get("goal") or "",
                "work_order": copy.deepcopy(project_state.get("work_order")),
                "active": bool(active_work),
                "run": {
                    key: active_work.get(key)
                    for key in ("run_id", "phase", "detail", "files", "checks")
                } if active_work else None,
                "last_run": copy.deepcopy(project_state.get("last_run")),
            },
            "rev": int(project_state.get("updated_at") or 0),
        },
        {
            "target_id": "canvas.active",
            "name": "当前研究画布",
            "kind": "canvas",
            "owner": "assistant",
            "description": "搜索结果、图片、表格、图表和3D模型所在的研究画布。",
            "aliases": ["research-canvas", "info-board"],
            "properties": {
                "focus_id": "节点ID", "selected_id": "节点ID", "zoom": "0.25-6",
                "fit": "contain/cover/width/actual", "fullscreen": "boolean",
                "expanded": "boolean", "active_tab_id": "标签ID", "close_tab_id": "标签ID",
            },
            "commands": ["inspect"],
            "state": {
                "available": info_panel.has_content(),
                "expanded": bool(info_panel.snapshot().get("expanded")),
                "active_tab_id": str(info_panel.snapshot().get("active_tab_id") or ""),
            },
            "rev": info_panel.snapshot().get("rev"),
        },
        {
            "target_id": "app.timer",
            "name": "计时器",
            "kind": "app",
            "owner": "assistant",
            "description": "内置倒计时应用。",
            "aliases": ["timer"],
            "state": surface_apps.live_state("timer"),
            "command_args": {
                "start": {"duration_seconds": "计时秒数，必填（30 分钟就是 1800）"},
                "add": {"duration_seconds": "追加的秒数，必填"},
            },
            "properties": {},
            "commands": ["open", "close", "start", "pause", "resume", "add", "reset", "status"],
        },
        {
            "target_id": "app.notes",
            "name": "记事本",
            "kind": "app",
            "owner": "assistant",
            "description": "内置语音记事应用。",
            "aliases": ["notes"],
            "state": surface_apps.live_state("notes"),
            "command_args": {
                "append": {"text": "要追加的一行文字，必填"},
                "replace": {"text": "替换成的全文，必填"},
            },
            "properties": {"text": "string"},
            "commands": ["open", "close", "append", "replace", "clear", "record_start", "record_stop"],
        },
        {
            "target_id": "agent.audio",
            "name": "声音通道",
            "kind": "audio",
            "owner": "assistant",
            "description": "从哪个设备出声、用哪个麦收音。用户说「声音切到耳机/用电脑麦」时改这里。",
            "aliases": ["音频", "声音", "扬声器", "麦克风", "耳机"],
            "properties": {"output": "设备名", "input": "设备名"},
            "commands": ["use_output", "use_input", "status"],
            "command_args": {
                "use_output": {"device": "设备名或说法：耳机 / 扬声器 / AirPods Pro。只有用户明说「跟随系统」才写 auto"},
                "use_input": {"device": "设备名或说法：内置麦 / 耳机。只有用户明说「跟随系统」才写 auto"},
            },
            "state": _audio_route_state(),
        },
        {
            "target_id": "surface.new",
            "name": "新页面",
            "kind": "surface_factory",
            "owner": "assistant",
            "description": "创建新的页面或打开一个尚不存在的网站。",
            "aliases": ["new-window", "new-surface"],
            # properties 曾是空的，模型只能猜字段名（实测它写 content=…，
            # 内容整段被丢掉，窗口只剩一个标题）。这里把能传什么写清楚。
            "properties": {
                "title": "string",
                "summary": "string",
                "sections": "array",
                "url": "string",
            },
            "commands": ["create"],
        },
    ]

    device_control.ensure_builtin_devices()
    known_states = {
        str(item.get("device_id") or ""): item.get("state") or {}
        for item in device_control.iot_registry.world_state()
    }
    for device in device_control.list_devices():
        device_id = str(device.get("device_id") or "")
        objects.append({
            "target_id": "iot.%s" % device_id,
            "name": str(device.get("name") or device_id),
            "kind": "iot.%s" % str(device.get("kind") or "device"),
            "owner": "physical",
            "description": "实体设备；只有用户明确指向它或上下文唯一指向它时才操作。",
            "aliases": [device_id],
            # 曾经每个能力都只写 "adapter-validated"（「适配器会校验」），
            # 等于告诉模型有这个命令、却不告诉它要传什么，只能靠报错试。
            "properties": {
                capability: "、".join(
                    "%s=%s" % (arg, hint)
                    for arg, hint in (
                        (device.get("command_args") or {}).get(capability) or {}
                    ).items()
                ) or "无参数"
                for capability in device.get("capabilities") or []
                if capability != "status"
            },
            "commands": list(device.get("capabilities") or []),
            "command_args": copy.deepcopy(device.get("command_args") or {}),
            "adjustable": copy.deepcopy(device.get("adjustable") or {}),
            "state": copy.deepcopy(known_states.get(device_id) or {}),
            # 现状的人话由适配器渲染：色名、单位这些只有它清楚，
            # 投影层再抄一份必然和真值漂移。
            "display": device_control.state_description(known_states.get(device_id) or {}),
        })

    try:
        scene = scene_store.inspect(scope="all")
    except Exception:
        scene = {"surfaces": []}
    for item in scene.get("surfaces") or []:
        surface_id = str(item.get("id") or "")
        if not surface_id or surface_id in {
            surface_tools.STATUS_TIMELINE_SURFACE,
            surface_tools.PINNED_INFO_SURFACE,
            surface_tools.WORK_HUD_SURFACE,
        }:
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        objects.append({
            "target_id": "surface.%s" % surface_id,
            "name": str(data.get("title") or surface_id),
            "kind": "surface",
            "owner": "assistant",
            "description": "已存在的桌面页面。",
            "aliases": _surface_aliases(surface_id, data),
            "properties": {
                "title": "string", "window": "geometry", "theme": "object",
                "content": "object", "visible": "boolean",
            },
            "commands": ["show", "hide", "delete", "append", "record_start", "record_stop"],
            "adjustable": {
                "width": {"label": "宽度", "min": 320, "max": 2000, "step": 80, "unit": "px",
                          "read": ["bounds", "width"],
                          "via": {"op": "apply", "path": ["window", "width"]}},
                "height": {"label": "高度", "min": 180, "max": 1400, "step": 60, "unit": "px",
                           "read": ["bounds", "height"],
                           "via": {"op": "apply", "path": ["window", "height"]}},
                "x": {"label": "横向位置", "min": 0, "max": 3000, "step": 60, "unit": "px",
                      "read": ["bounds", "x"],
                      "via": {"op": "apply", "path": ["window", "x"]}},
                "y": {"label": "纵向位置", "min": 0, "max": 2000, "step": 60, "unit": "px",
                      "read": ["bounds", "y"],
                      "via": {"op": "apply", "path": ["window", "y"]}},
            },
            "state": {
                "visible": bool(item.get("visible")),
                "focused": bool(item.get("focused")),
                "bounds": copy.deepcopy(item.get("bounds") or {}),
                "content_status": str(item.get("content_status") or "unknown"),
            },
            "rev": scene.get("rev"),
        })
    return objects


def _result_from_legacy(text: str, meta: dict, *, changed_default=False) -> dict:
    receipt = dict(meta or {})
    receipt["ok"] = bool(receipt.get("ok"))
    receipt.setdefault("changed", changed_default if receipt["ok"] else False)
    receipt.setdefault("detail", str(text or "")[:1000])
    receipt.pop("direct_reply", None)
    return receipt


def _execute_status(op: str, payload: dict) -> dict:
    from control_plane import info_panel

    if op == "invoke":
        command = str(payload.get("command") or "").strip().lower()
        if command in {"show", "open"}:
            text, meta = surface_tools.surface_manage_execute({
                "action": "open", "surface_id": surface_tools.STATUS_TIMELINE_SURFACE,
                "focus": False,
            })
            result = _result_from_legacy(text, meta)
            result["speech"] = "状态栏已显示"
            return result
        if command in {"hide", "close"}:
            text, meta = surface_tools.surface_manage_execute({
                "action": "close", "surface_id": surface_tools.STATUS_TIMELINE_SURFACE,
            })
            result = _result_from_legacy(text, meta)
            result["speech"] = "状态栏已隐藏"
            return result
        if command in {"expand", "collapse"}:
            expanded = command == "expand"
            before = bool(info_panel.snapshot().get("expanded"))
            state = info_panel.set_expanded(expanded)
            surface_tools.set_status_timeline_expanded(bool(state.get("expanded")))
            changed = before != bool(state.get("expanded"))
            return {
                "ok": True,
                "changed": changed,
                "state": _status_state(),
                "speech": "信息推送已展开" if expanded else "信息推送已收起",
            }
        return {"ok": False, "reason": "unknown_command", "detail": "状态栏不支持该命令"}

    patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
    if not patch:
        return {"ok": False, "reason": "empty_patch", "detail": "没有要修改的属性"}
    changed = False
    theme_patch = None
    for key in ("theme", "appearance", "colors"):
        if isinstance(patch.get(key), dict):
            theme_patch = dict(patch[key])
            break
    direct_theme = {key: value for key, value in patch.items() if key in _THEME_KEYS}
    if direct_theme:
        theme_patch = {**(theme_patch or {}), **direct_theme}
    if theme_patch is not None:
        themed = surface_tools.set_status_timeline_theme(theme_patch)
        if not themed.get("ok"):
            return themed
        changed = changed or bool(themed.get("changed"))
    if "expanded" in patch:
        before = bool(info_panel.snapshot().get("expanded"))
        state = info_panel.set_expanded(bool(patch["expanded"]))
        surface_tools.set_status_timeline_expanded(bool(state.get("expanded")))
        changed = changed or before != bool(state.get("expanded"))
    if "visible" in patch:
        action = "open" if bool(patch["visible"]) else "close"
        text, meta = surface_tools.surface_manage_execute({
            "action": action,
            "surface_id": surface_tools.STATUS_TIMELINE_SURFACE,
            "focus": False,
        })
        if not meta.get("ok"):
            return _result_from_legacy(text, meta)
        changed = changed or bool(meta.get("changed", True))
    if isinstance(patch.get("window"), dict):
        text, meta = surface_tools.surface_manage_execute({
            "action": "set",
            "surface_id": surface_tools.STATUS_TIMELINE_SURFACE,
            "window": patch["window"],
        })
        if not meta.get("ok"):
            return _result_from_legacy(text, meta)
        changed = changed or bool(meta.get("changed"))
    recognized = bool(theme_patch is not None or set(patch) & {"expanded", "visible", "window"})
    if not recognized:
        return {"ok": False, "reason": "unsupported_property", "detail": "状态栏不支持这些属性"}
    return {
        "ok": True,
        "changed": changed,
        "state": _status_state(),
        "speech": "状态栏已更新" if changed else "状态栏已经是这个状态",
    }


def _device_arguments(patch: dict) -> Tuple[str, dict]:
    values = dict(patch or {})
    if "on" in values and "power" not in values:
        values["power"] = values.pop("on")
    color = values.pop("color", None)
    if color is not None:
        value = str(color or "").strip()
        if value.startswith("#") and len(value) in {4, 7}:
            if len(value) == 4:
                value = "#" + "".join(char * 2 for char in value[1:])
            try:
                values.update({
                    "red": int(value[1:3], 16),
                    "green": int(value[3:5], 16),
                    "blue": int(value[5:7], 16),
                })
            except ValueError:
                return "", {}
        else:
            values["color_name"] = value.lower()
    meaningful = [key for key in values if key not in {"speak_while", "progress_reply", "continue_after"}]
    if not meaningful:
        return "", {}
    if set(meaningful) <= {"power"}:
        return "power", {"on": bool(values.get("power"))}
    if set(meaningful) <= {"color_name", "red", "green", "blue"}:
        return "color", values
    if set(meaningful) <= {"brightness"}:
        return "brightness", values
    if set(meaningful) <= {"effect", "speed"}:
        return "effect", values
    return "set", values


def _execute_device(op: str, target: str, payload: dict, ctx: dict) -> dict:
    device_id = target[len("iot."):]
    if op == "apply":
        patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
        action, args = _device_arguments(patch)
        if not action:
            return {"ok": False, "reason": "invalid_device_patch", "detail": "设备属性补丁无效"}
    else:
        action = str(payload.get("command") or "").strip()
        args = dict(payload.get("args") or {})
    text, meta = device_control.execute({
        "device_id": device_id,
        "action": action,
        **args,
    }, request_id=str(ctx.get("trace_id") or ""))
    result = _result_from_legacy(text, meta)
    result["verified"] = bool(meta.get("verified", meta.get("ok")))
    # 自报改完之后的现状：适配器本来就懂怎么把 state 渲染成人话，
    # 报了就免掉注册表回头重查一次目录。
    state = meta.get("state") if isinstance(meta.get("state"), dict) else {}
    if state:
        result["display"] = device_control.state_description(state)
    return result


def _execute_canvas(op: str, payload: dict) -> dict:
    if op == "invoke":
        command = str(payload.get("command") or "").strip().lower()
        if command != "inspect":
            return {"ok": False, "reason": "unknown_command", "detail": "画布命令无效"}
        text, meta = canvas_control.execute({"action": "inspect"})
        return _result_from_legacy(text, meta)
    patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
    args = {
        "action": "apply",
        "base_rev": payload.get("base_rev"),
        **patch,
    }
    text, meta = canvas_control.execute(args)
    return _result_from_legacy(text, meta)


# 常见站点的中文叫法。窗口的可搜索文本全是英文（surface.web-bilibili-com、
# 标题 Bilibili），用户说「把哔哩哔哩关上」时子串一个都对不上，模型只能回
# 「没找到哔哩哔哩，可能没装或者叫别的名」——实测日志里就是这么答的。
# 这是一张数据表，不是逻辑：以后接新站点往里加一行即可。
_SITE_ALIASES = {
    "bilibili": ["哔哩哔哩", "b站", "比站"],
    "zhihu": ["知乎"],
    "youtube": ["油管", "优兔"],
    "baidu": ["百度"],
    "taobao": ["淘宝"],
    "jd": ["京东"],
    "weibo": ["微博"],
    "douyin": ["抖音"],
    "xiaohongshu": ["小红书"],
    "google": ["谷歌"],
    "github": ["吉特哈布"],
    "csdn": ["csdn博客"],
    "twitter": ["推特"],
    "x": ["推特"],
}


def _audio_route_state():
    try:
        from control_plane import audio_route

        return audio_route.snapshot()
    except Exception:
        return {}


def _surface_aliases(surface_id: str, data: dict) -> list:
    """窗口的别名：稳定 id + 站点中文叫法，让「哔哩哔哩」也能命中。"""
    aliases = [surface_id]
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    host = ""
    match = re.match(r"^https?://([^/?#]+)", str(content.get("url") or ""))
    if match:
        host = match.group(1).lower().removeprefix("www.")
    elif surface_id.startswith("web-"):
        host = surface_id[4:].replace("-", ".")
    if host:
        aliases.append(host)
        label = host.split(".")[0]
        aliases.append(label)
        aliases.extend(_SITE_ALIASES.get(label, []))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _execute_app(target: str, payload: dict) -> dict:
    app_id = target[len("app."):]
    command = str(payload.get("command") or "open")
    args = dict(payload.get("args") or {})
    if command in {"close", "hide", "delete", "show"}:
        # 内置应用此前只有 open/start/pause… 没有任何关闭命令，用户「把记事本
        # 关上」时模型只能回「我这边关不掉它，你自己点叉」。应用本身也是窗口，
        # 关闭走窗口那条路即可。
        text, meta = surface_control.execute({
            "action": "show" if command == "show" else "close",
            "surface_id": "app-%s" % app_id,
        })
    elif command in {"record_start", "record_stop"}:
        text, meta = surface_control.execute({
            "action": command,
            "surface_id": "app-%s" % app_id,
            **args,
        })
    else:
        text, meta = surface_control.execute({
            "action": "app",
            "app_id": app_id,
            "command": command,
            "lang": payload.get("lang") or "zh",
            **args,
        })
    return _result_from_legacy(text, meta)


def _execute_surface(op: str, target: str, payload: dict) -> dict:
    if target == "surface.new":
        if op != "invoke" or str(payload.get("command") or "") != "create":
            return {"ok": False, "reason": "factory_requires_create"}
        args = dict(payload.get("args") or {})
        text, meta = surface_control.execute({"action": "create", **args})
        result = _result_from_legacy(text, meta)
        result["created_target_id"] = (
            "surface.%s" % meta.get("surface_id") if meta.get("surface_id") else ""
        )
        return result
    surface_id = target[len("surface."):]
    if op == "apply":
        patch = dict(payload.get("patch") or {})
        visible = patch.pop("visible", None) if "visible" in patch else None
        changed = False
        if patch:
            text, meta = surface_tools.surface_manage_execute({
                "action": "set", "surface_id": surface_id, "definition": patch,
            })
            if not meta.get("ok"):
                return _result_from_legacy(text, meta)
            changed = changed or bool(meta.get("changed"))
        if visible is not None:
            text, meta = surface_tools.surface_manage_execute({
                "action": "open" if visible else "close", "surface_id": surface_id,
            })
            if not meta.get("ok"):
                return _result_from_legacy(text, meta)
            changed = changed or bool(meta.get("changed", True))
        if not patch and visible is None:
            return {"ok": False, "reason": "empty_patch"}
        return {"ok": True, "changed": changed, "speech": "%s已更新" % surface_id}
    command = str(payload.get("command") or "").strip().lower()
    args = dict(payload.get("args") or {})
    action = {"show": "show", "open": "show", "hide": "close"}.get(command, command)
    text, meta = surface_control.execute({
        "action": action,
        "surface_id": surface_id,
        **args,
    })
    return _result_from_legacy(text, meta)


def _execute_project(op: str, payload: dict, ctx: dict) -> dict:
    if op == "apply":
        patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
        if not patch:
            return {"ok": False, "reason": "empty_patch"}
        from devices.coding import project_fsm
        allowed = {key: value for key, value in patch.items() if key in {"goal", "cwd", "mode", "plan_steps"}}
        if not allowed:
            return {"ok": False, "reason": "unsupported_property"}
        project_fsm.update_brief(int(ctx.get("aid") or 1), allowed)
        return {"ok": True, "changed": True, "speech": "工程信息已经更新"}

    if op != "invoke":
        return {"ok": False, "reason": "project_requires_invoke"}
    command = str(payload.get("command") or "").strip().lower()
    args = dict(payload.get("args") or {})
    aid = int(ctx.get("aid") or 1)
    from control_plane import database as db
    from devices.coding import agent_runtime, orchestrator, project_fsm

    if command == "status":
        state = project_fsm.load(aid)
        return {
            "ok": True, "changed": False, "state": state,
            "runtime": agent_runtime.get_active_run(),
            "speech": project_fsm.status_speech(aid),
        }
    if command == "cancel":
        active = agent_runtime.get_active_run() or {}
        stopped = agent_runtime.cancel_run(str(active.get("run_id") or ""))
        if stopped:
            return {"ok": True, "changed": True, "speech": "正在停止这项工作。"}
        return {"ok": False, "changed": False, "reason": "not_running", "detail": "现在没有执行中的工程任务"}
    if command == "revert":
        result = orchestrator.handle_revert(aid)
        return {
            **result,
            "changed": bool(result.get("ok")),
            "speech": "已经恢复到修改前。" if result.get("ok") else str(result.get("error") or "没有可恢复的检查点"),
        }
    if command == "update":
        request = str(args.get("request") or args.get("text") or "").strip()
        if not request:
            return {"ok": False, "reason": "missing_request", "detail": "缺少补充要求"}
        if agent_runtime.get_active_run():
            accepted = agent_runtime.steer_run(request)
            if accepted:
                return {"ok": True, "changed": True, "speech": "我把这点补充进正在进行的工作了。"}
            project_fsm.set_pending_patch(aid, request)
            return {"ok": True, "changed": True, "queued": True, "speech": "这点我记下了，会接着处理。"}
        project_fsm.update_brief(aid, {"current_request": request})
        return {"ok": True, "changed": True, "speech": "要求已经更新，还没有开始执行。"}
    if command == "plan":
        goal = str(args.get("goal") or args.get("request") or "").strip()
        steps = args.get("plan_steps") if isinstance(args.get("plan_steps"), list) else []
        steps = [str(item).strip() for item in steps if str(item).strip()][:20]
        if not goal:
            return {"ok": False, "reason": "missing_goal", "detail": "还缺少明确目标"}
        if not steps:
            steps = ["检查相关文件和当前行为", "完成必要修改", "运行检查并核对文件回执"]
        patch = {"goal": goal, "plan_steps": steps}
        if str(args.get("cwd") or "").strip():
            patch["cwd"] = str(args["cwd"]).strip()
        if str(args.get("mode") or "").strip() in {"external", "self_extend"}:
            patch["mode"] = str(args["mode"]).strip()
        project_fsm.update_brief(aid, patch)
        order = project_fsm.prepare_work_order(aid, goal=goal, plan_steps=steps)
        project_fsm.transition(aid, "awaiting_confirm", reason="conversation_plan_ready")
        return {
            "ok": True, "changed": True, "work_order": order,
            "speech": "我会先%s；然后%s。确认后我就开始。" % (steps[0], "；再".join(steps[1:])),
        }
    if command == "confirm":
        state = project_fsm.load(aid)
        order = project_fsm.approve_current_work_order(aid, approval_source="conversation_confirmed")
        if not order:
            return {"ok": False, "reason": "no_current_work_order", "detail": "没有等待确认的当前任务"}
        brief = state.get("brief") or {}
        steps = list(order.get("plan_steps") or [])
        task = "%s\n\nConfirmed plan:\n%s" % (
            order.get("goal") or brief.get("goal") or "完成已确认工程任务",
            "\n".join("- %s" % item for item in steps),
        )
        started = orchestrator.start_writing(
            aid,
            task,
            get_setting=db.get_setting,
            set_setting=db.set_setting,
            base_url="http://127.0.0.1:8002",
            mode=str(brief.get("mode") or "external"),
            cwd=str(brief.get("cwd") or ""),
            open_desk=True,
        )
        return {
            "ok": bool(started.get("ok")),
            "changed": bool(started.get("ok")),
            "run_id": started.get("run_id") or "",
            "speech": str(started.get("speech") or started.get("error") or "工作 Agent 没能启动。"),
        }
    return {"ok": False, "reason": "unknown_command", "detail": "工程任务不支持该命令"}


def _execute_builtin(op: str, target: str, payload: dict, ctx: dict) -> dict:
    if target == "agent.ui.status":
        return _execute_status(op, payload)
    if target == "canvas.active":
        return _execute_canvas(op, payload)
    if target == "project.active":
        return _execute_project(op, payload, ctx)
    if target.startswith("iot."):
        return _execute_device(op, target, payload, ctx)
    if target == "agent.audio":
        from control_plane import audio_route

        if op != "invoke":
            return {"ok": False, "reason": "audio_requires_invoke"}
        return audio_route.execute(
            str(payload.get("command") or "status"),
            payload.get("args") if isinstance(payload.get("args"), dict) else {},
        )
    if target.startswith("app."):
        if op != "invoke":
            return {"ok": False, "reason": "app_requires_invoke"}
        return _execute_app(target, payload)
    if target == "surface.new" or target.startswith("surface."):
        return _execute_surface(op, target, payload)
    return {"ok": False, "reason": "unsupported_target"}


def ensure_builtin_provider() -> None:
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    object_registry.register_provider(
        "ev.builtin",
        discover=_discover_builtin_objects,
        execute=_execute_builtin,
        target_prefixes=("agent.ui.", "project.", "canvas.", "app.", "surface.", "iot."),
    )
    _BUILTINS_REGISTERED = True
    # 搜索结果作为可打开对象（result.N）独立注册，与内置 provider 互不影响
    from tools import search_objects
    search_objects.ensure_provider()


def _lang_of(text) -> str:
    """这轮对话在说中文还是英文——决定保留下来的那几句固定话术用哪种。

    只看用户这一句里有没有汉字。够用：用户切到英文时整句都是英文，
    切回中文时也是。判错的代价只是一句「Paused.」说成「暂停了。」。
    """
    return "zh" if any("一" <= ch <= "鿿" for ch in str(text or "")) else "en"


def execute(arguments: Dict[str, Any], *, ctx=None) -> Tuple[str, Dict[str, Any]]:
    ensure_builtin_provider()
    args = dict(arguments or {})
    op = str(args.get("op") or "").strip().lower()
    target = str(args.get("target") or "").strip()
    payload = {
        "lang": _lang_of((ctx or {}).get("user_message")),
        "selector": args.get("selector") if isinstance(args.get("selector"), dict) else {},
        "patch": args.get("patch") if isinstance(args.get("patch"), dict) else {},
        "command": str(args.get("command") or ""),
        "args": args.get("args") if isinstance(args.get("args"), dict) else {},
        "base_rev": args.get("base_rev"),
        "property": str(args.get("property") or ""),
        "direction": str(args.get("direction") or ""),
        "amount": str(args.get("amount") or ""),
        "say": str(args.get("say") or "").strip()[:160],
    }
    result = object_registry.execute(op, target, payload, ctx or {})
    if op == "inspect":
        if result.get("ok"):
            text = json.dumps(result, ensure_ascii=False)
        else:
            text = "没有找到这个对象；请缩小 selector 后重新 inspect。"
        return text, result
    if result.get("ok"):
        # 播报优先用模型自己写的那句话。服务端替它说出来的都是同一个模子里
        # 刻的（「窗口已关闭」「桌面灯带已更新」），听着就像念回执。
        said = str(payload.get("say") or "").strip()
        if said and _say_names_another_object(
            said, str(result.get("target_id") or ""), str(result.get("target_name") or ""),
        ):
            said = ""      # 说成了别的对象：不播，让它看着回执重说
        resolved = str(result.get("device") or "").strip()
        if said and resolved and "".join(resolved.lower().split()) not in "".join(said.lower().split()):
            # 选择类命令由服务端解析出真实设备（「耳机」→ AirPods Pro）。
            # 预写的话是执行前写的，可能和结果对不上——实测「切到扬声器」被
            # 传成了「默认」，回执说「改回跟随系统」，它却播报「切到扬声器了」。
            said = ""
        if said:
            result["speech"] = said
            result["direct_reply"] = said
        elif not result.get("speech_fixed"):
            # 模型没写就不替它说。这行以前只是句注释：适配器早就把「窗口已关闭」
            # 「已经加上时间了」塞进回执了，这里不覆盖它也照样被播出去，用户听到的
            # 永远是同一套念稿。现在真的抹掉，让它看着回执自己组织语言。
            # speech_fixed 是适配器明确声明「这句固定话术是有意保留的」的标记，
            # 目前只有计时器的暂停/恢复带它。
            result.pop("speech", None)
            result.pop("direct_reply", None)
        return json.dumps(result, ensure_ascii=False), result
    return json.dumps(result, ensure_ascii=False), result


def _say_names_another_object(said: str, target_id: str, target_name: str) -> bool:
    """这句话是不是把动作说成了别的对象？

    模型自己写播报之后，服务端不再能保证话里提到的就是真实目标。但要求它
    必须念出目标全名也不合理——人说话本来就省略（「好了，灯变蓝了」不会说
    「桌面灯带」）。所以只拦真正的冒充：话里出现了另一个对象的名字，而目标
    自己的名字没出现。
    """
    body = "".join(str(said or "").lower().split())
    if not body:
        return False
    own = "".join(str(target_name or "").lower().split())
    if own and own in body:
        return False
    try:
        catalog = object_registry.world()
    except Exception:
        return False
    for item in catalog or []:
        if not isinstance(item, dict) or str(item.get("target_id") or "") == target_id:
            continue
        other = "".join(str(item.get("name") or "").lower().split())
        if len(other) >= 2 and other in body:
            return True
    return False


def register(registry, *, wrapper=None):
    ensure_builtin_provider()

    def fn(args, ctx):
        return execute(args, ctx=ctx)

    final = wrapper(fn, "object_control") if wrapper else fn
    registry.register("object_control", final, conflicts="target")
