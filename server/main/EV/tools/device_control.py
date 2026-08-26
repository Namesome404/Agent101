# -*- coding: utf-8 -*-
"""Single high-level agent tool for all registered IoT devices."""
from __future__ import annotations

import json

from devices.coding import led
from devices.iot import iot_registry


DEFAULT_DEVICE_ID = "desk-light"


# 每个能力要传什么，写在适配器绑定处——这是唯一知道 led.py 校验规则的地方。
# 描述符和提示词都从这里生成，模型不必靠「不支持 set_color」「red 必须是整数」
# 这种报错一次次试出来（实测一次调灯要三个 LLM 来回，4.4 秒里 3.3 秒在猜）。
LED_COMMAND_ARGS = {
    "power": {"on": "布尔，true 开 false 关（必填）"},
    "color": {
        "color_name": "红黄蓝等常见色名之一：%s（与 red/green/blue 二选一）" % "、".join(
            sorted(led.NAMED_COLORS)
        ),
        "red": "0-255 整数（给 rgb 时三个都要给）",
        "green": "0-255 整数",
        "blue": "0-255 整数",
        "brightness": "可选，0-100 整数",
    },
    "brightness": {"brightness": "0-100 整数（必填），0 即全灭"},
    "effect": {
        "effect": "灯效名：%s（必填）" % "、".join(sorted(led.EFFECTS)),
        "speed": "可选，1-100 整数，默认 50",
        "brightness": "可选，0-100 整数",
    },
    "set": {
        "power": "可选布尔", "red": "可选 0-255", "green": "可选 0-255",
        "blue": "可选 0-255", "brightness": "可选 0-100",
        "effect": "可选灯效名", "speed": "可选 1-100", "count": "可选 1-300 灯珠数",
    },
    "status": {},
}


# 可相对调整的数值属性。声明了量纲，「暗一点」就由服务端读当前值再算，
# 模型不必做算术——同一套机制对以后接进来的任何设备都成立。
LED_ADJUSTABLE = {
    "brightness": {
        "min": 0, "max": 100, "step": 10, "unit": "%", "label": "亮度",
        "read": ["brightness"],
        "via": {"op": "invoke", "command": "brightness", "arg": "brightness"},
    },
}


def _led_executor(action, arguments):
    payload = dict(arguments or {})
    payload["action"] = action
    return led.execute("led_control", payload)


def ensure_builtin_devices() -> None:
    if iot_registry.descriptor(DEFAULT_DEVICE_ID):
        return
    iot_registry.register(
        DEFAULT_DEVICE_ID,
        name="桌面灯带",
        kind="light",
        capabilities=("power", "color", "brightness", "effect", "set", "status"),
        executor=_led_executor,
        transport="lan-http",
        metadata={"adapter": "ws2812", "verified_readback": True},
        command_args=LED_COMMAND_ARGS,
        adjustable=LED_ADJUSTABLE,
    )


def tool_definition(*, slim=False):
    if slim:
        description = (
            "物联网中控；desk-light=桌面灯带。仅在明确改变/查询设备时调用；"
            "问看法/评价用 conversation_reply。用 power/color/brightness/effect"
            "控制；只认回执。"
        )
    else:
        description = (
            "统一物联网中控。控制已注册设备并读取真实回执；当前 desk-light 是桌面灯带。"
            "开关/颜色/亮度/灯效分别用 power/color/brightness/effect，查询用 status。"
            "常见颜色必须用 color_name，只有自定义颜色才用 RGB；设备不可达时 ok=false。"
            "每次写操作都由设备适配器写入后独立读回验证，只有实际状态匹配才算完成。"
            "未来新增 Home Assistant、MQTT、Matter 设备仍使用本工具，不新增平铺工具。"
        )
    return {
        "type": "function",
        "function": {
            "name": "device_control",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "enum": [DEFAULT_DEVICE_ID],
                        "description": "稳定设备 id；当前为 desk-light。",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["power", "color", "brightness", "effect", "set", "status"],
                    },
                    "on": {"type": "boolean"},
                    "color_name": {
                        "type": "string",
                        "enum": list(led.NAMED_COLORS),
                    },
                    "red": {"type": "integer", "description": "0-255"},
                    "green": {"type": "integer", "description": "0-255"},
                    "blue": {"type": "integer", "description": "0-255"},
                    "brightness": {"type": "integer", "description": "0-100"},
                    "effect": {
                        "type": "string",
                        "enum": ["solid", "rainbow", "breathing", "wipe"],
                    },
                    "speed": {"type": "integer", "description": "1-100"},
                    "count": {"type": "integer", "description": "1-300"},
                    "speak_while": {
                        "type": "boolean",
                        "description": "执行时并行播安全开始语；不代表完成。",
                    },
                    "continue_after": {
                        "type": "boolean",
                        "description": "必须填；有后续步骤才true。",
                    },
                    "reply": {
                        "type": "string",
                        "description": "成功后说的那句：你自己的话，口语，十来个字，别套模板。",
                    },
                },
                "required": ["device_id", "action", "continue_after", "reply"],
            },
        },
    }


def execute(arguments=None, *, request_id=""):
    ensure_builtin_devices()
    args = dict(arguments or {})
    device_id = str(args.pop("device_id", DEFAULT_DEVICE_ID) or DEFAULT_DEVICE_ID)
    action = str(args.pop("action", "") or "")
    natural_reply = str(args.pop("reply", "") or "").strip()[:240]
    # Runtime scheduling controls belong to the agent, not the device adapter.
    args.pop("speak_while", None)
    args.pop("continue_after", None)
    receipt = iot_registry.execute(
        device_id,
        action,
        args,
        request_id=request_id,
    )
    meta = dict(receipt["meta"] or {})
    if meta.get("ok") and action != "status" and natural_reply:
        meta["direct_reply"] = natural_reply
    return receipt["result"], meta


def list_devices(*, include_status=False):
    ensure_builtin_devices()
    devices = iot_registry.list_devices()
    if include_status:
        for descriptor in devices:
            status = iot_registry.execute(
                descriptor["device_id"],
                "status",
                {},
            )
            descriptor["online"] = bool(status["ok"])
            descriptor["status"] = status["meta"].get("state") or {}
            if not status["ok"]:
                descriptor["error"] = status["meta"].get("error") or "状态读取失败"
    return devices


def known_state_hint() -> str:
    """设备当前真实状态快照（内存缓存，不发网络请求），供 voice 注入。

    只描述世界现状，不出现任何工具名/动作名——「最近调过哪些工具」的日志
    会诱导模型跟调同类工具（如满屏 status 回执 → 模型跟着调 status），
    而「现在灯是红的」这类状态不会。窗口状态由 surface memory_hint 注入。
    """
    try:
        ensure_builtin_devices()
        known = iot_registry.world_state()
    except Exception:
        return ""
    if not known:
        return ""
    lines = []
    for item in known:
        desc = _state_description(item.get("state") or {})
        if desc:
            device_id = str(item.get("device_id") or "")
            lines.append(
                "- target=iot.%s | name=%s | owner=physical：%s"
                % (device_id, item.get("name") or device_id, desc)
            )
    if not lines:
        return ""
    return (
        "【当前设备状态】（按最近一次真实回执自动更新，仅作背景状态，不是聊天话术）\n"
        + "\n".join(lines)
        + "\n用途只有两个：①核对外部真假（只信这里的实际状态，不信聊天历史里"
          "没有回执的『已调好/已打开』）；②接上『这个/刚才』的指代。"
          "它【不是提示你再做一次】。要不要有新动作，以用户最新这一句为准："
          "最新这句确实要求改变/打开/关闭/查询时才重新调工具；只是评价、感叹、答应或闲聊，"
          "就 conversation_reply，别因为上一轮做过就重复上一个动作。"
          "实体设备 owner=physical；『你/你自己/给自己』不是在指实体设备。"
    )


def state_description(state) -> str:
    """公开入口：把设备 state 渲染成一句中文现状（世界快照与提示都用它）。"""
    return _state_description(state)


def _state_description(state) -> str:
    """把设备回执里的原始 state 渲染成一句中文现状，不暴露协议细节。"""
    if not isinstance(state, dict):
        return ""
    parts = []
    power = state.get("power")
    if power is False:
        parts.append("关闭")
    elif power is True:
        parts.append("打开")
        try:
            red = int(state.get("red") or 0)
            green = int(state.get("green") or 0)
            blue = int(state.get("blue") or 0)
            color = led._color_name(red, green, blue)
            if color and color not in ("黑色", "彩色"):
                parts.append(color)
        except (TypeError, ValueError):
            pass
        brightness = state.get("brightness")
        if brightness is not None:
            try:
                parts.append("亮度%d%%" % int(brightness))
            except (TypeError, ValueError):
                pass
        effect = str(state.get("effect") or "")
        if effect and effect != "solid":
            parts.append("%s灯效" % effect)
    elif power is None and state.get("effect"):
        effect = str(state.get("effect") or "")
        parts.append("%s灯效" % effect)
    return "，".join(parts)


def register(registry, *, wrapper=None):
    ensure_builtin_devices()

    def fn(args, ctx):
        ctx = ctx if isinstance(ctx, dict) else {}
        return execute(args, request_id=ctx.get("trace_id") or "")

    final_fn = wrapper(fn, "device_control") if wrapper else fn
    registry.register(
        "device_control",
        final_fn,
        conflicts="device_id",
        aliases=["led_control"],
    )


def format_receipt(arguments=None):
    """Small deterministic helper for non-agent API consumers."""
    text, meta = execute(arguments)
    return json.loads(text) if text.startswith("{") else {"message": text}, meta
