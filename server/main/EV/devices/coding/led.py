# -*- coding: utf-8 -*-
"""ESP32 WS2812 灯带 skill。

把 ws2812-lan-control 固件（ESP32 GPIO27 直驱 WS2812 数字灯带）的 HTTP JSON API
接入 Muse 动作流，作为与 surface_manage / coding_flow 平级的动作流工具。

设备地址默认 http://ws2812.local（mDNS），可用 .env 的 LED_DEVICE_URL 覆盖为
具体局域网 IP；也可换端口（LED_MCP_PORT）。独立实现的设备调用不依赖
led_mcp_server 进程，MCP 服务（:8012）仍是核心 server 侧的独立通道。

回调即真相：每个动作都从设备拿到最新 state 回执，设备不可达时返回 ok:false，
绝不谎报已执行。
"""

import json
import os
import threading
import urllib.request

from common.paths import ENV_PATH, TMP_DIR

EFFECTS = {"solid", "rainbow", "breathing", "wipe"}
NAMED_COLORS = {
    "red": ((255, 0, 0), "红色"),
    "green": ((0, 255, 0), "绿色"),
    "blue": ((0, 0, 255), "蓝色"),
    "yellow": ((255, 255, 0), "黄色"),
    "purple": ((128, 0, 255), "紫色"),
    "cyan": ((0, 255, 255), "青色"),
    "white": ((255, 255, 255), "白色"),
    "orange": ((255, 96, 0), "橙色"),
    "pink": ((255, 64, 128), "粉色"),
    "warm_white": ((255, 180, 96), "暖白色"),
}

_LOCK = threading.RLock()


def _load_env():
    if not ENV_PATH.is_file():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(
            name.strip(),
            value.strip().strip("\"'"),
        )


_load_env()

DEVICE_URL = os.environ.get(
    "LED_DEVICE_URL",
    "http://ws2812.local",
).rstrip("/")
REQUEST_TIMEOUT = float(os.environ.get("LED_REQUEST_TIMEOUT", "2.5"))
_TRACE_PATH = TMP_DIR / "ev_led_trace.jsonl"
_VERIFY_ATTEMPTS = 2


def _request(method: str, path: str, payload: dict = None) -> dict:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        DEVICE_URL + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace").strip()
            if not raw:
                return {}
            return json.loads(raw)
    except Exception as error:
        raise RuntimeError(
            "无法连接灯光设备 %s：%s" % (DEVICE_URL, error)
        ) from error


def _result(message: str, state: dict) -> str:
    return json.dumps(
        {"message": message, "state": state},
        ensure_ascii=False,
    )


def _color_name(red: int, green: int, blue: int) -> str:
    """RGB → 中文颜色名（供语音播报与世界现状）。

    先查自己的色表：用户说「调成暖白」时设备被设成的就是表里那组值，
    再用色相近似去猜等于把自己刚设的颜色说错——实测 (255,180,96) 正是
    warm_white 的定义值，却被近似成「黄色」，于是回执里的现状和用户
    刚下的指令对不上。表里没有的颜色才走近似。
    """
    for name, (rgb, label) in NAMED_COLORS.items():
        if tuple(rgb) == (int(red), int(green), int(blue)):
            return label
    if red >= 200 and green >= 200 and blue >= 200:
        return "白色"
    if red < 40 and green < 40 and blue < 40:
        return "黑色"
    if red >= 150 and green < 100 and blue < 100:
        return "红色"
    if green >= 150 and red < 100 and blue < 100:
        return "绿色"
    if blue >= 150 and red < 100 and green < 100:
        return "蓝色"
    if red >= 150 and green >= 150 and blue < 100:
        return "黄色"
    if red >= 150 and blue >= 150 and green < 100:
        return "紫色"
    if green >= 150 and blue >= 150 and red < 100:
        return "青色"
    if red > green and red > blue:
        return "偏红"
    if green > red and green > blue:
        return "偏绿"
    if blue > red and blue > green:
        return "偏蓝"
    return "彩色"


def _trace(action: str, arguments: dict, ok: bool, meta: dict) -> None:
    """LED 动作留痕，供 turn_trace / 调试排查。"""
    try:
        TMP_DIR.mkdir(exist_ok=True)
        with _LOCK, open(_TRACE_PATH, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "action": action,
                        "arguments": arguments,
                        "ok": ok,
                        "meta": meta,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def _validate_range(name: str, value, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 必须是整数" % name)
    if not minimum <= value <= maximum:
        raise ValueError("%s 必须在 %d 到 %d 之间" % (name, minimum, maximum))
    return value


def _state_matches(state: dict, expected: dict) -> bool:
    if not isinstance(state, dict):
        return False
    for key, wanted in expected.items():
        actual = state.get(key)
        if key == "power":
            if bool(actual) != bool(wanted):
                return False
            continue
        if key in {"red", "green", "blue", "brightness", "speed", "count"}:
            try:
                if int(actual) != int(wanted):
                    return False
            except (TypeError, ValueError):
                return False
            continue
        if str(actual) != str(wanted):
            return False
    return True


def _write_and_verify(payload: dict) -> tuple:
    """写设备后独立读回；只有实际状态满足期望才返回成功。

    弱网下 POST 响应可能超时/丢包，但设备往往已执行变更。因此 POST 抛错时不
    直接判失败，继续独立读回：读回符合期望即算成功，真实状态说了算。
    返回 (实际状态, 失败原因或空串, 写请求是否曾异常)。
    """
    write_error = ""
    try:
        _request("POST", "/api/led/state", payload)
    except Exception as error:
        write_error = str(error)
    last_actual = {}
    for _attempt in range(_VERIFY_ATTEMPTS):
        try:
            actual = _request("GET", "/api/led/status")
        except Exception:
            continue
        last_actual = actual
        if _state_matches(actual, payload):
            return actual, "", write_error
    if write_error:
        return last_actual, "%s；读回未确认变更" % write_error, write_error
    return last_actual, "设备返回的实际状态与请求不一致", write_error


def _verified_meta(action: str, arguments: dict, payload: dict, speech: str, **extra):
    try:
        state, verify_error, write_error = _write_and_verify(payload)
    except Exception as error:
        meta = {
            "ok": False,
            "verified": False,
            "action": action,
            "error": str(error),
            **extra,
        }
        _trace(action, arguments, False, meta)
        return _result(str(error), {}), meta
    ok = not verify_error
    meta = {
        "ok": ok,
        "verified": ok,
        "action": action,
        "desired_state": dict(payload),
        "state": state,
        **extra,
    }
    if ok:
        meta["speech"] = speech
        if write_error:
            meta["write_response_lost"] = True
    else:
        meta["error"] = verify_error
    _trace(action, arguments, ok, meta)
    message = speech if ok else verify_error
    return _result(message, state), meta


# ==================== 动作执行 ====================

def led_status_execute(arguments: dict = None):
    arguments = arguments if isinstance(arguments, dict) else {}
    try:
        state = _request("GET", "/api/led/status")
    except Exception as error:
        return _result(str(error), {}), {
            "ok": False,
            "action": "status",
            "error": str(error),
        }
    return _result("已读取灯光状态", state), {
        "ok": bool(state),
        "action": "status",
        "state": state,
    }


def led_power_execute(arguments: dict = None):
    arguments = arguments if isinstance(arguments, dict) else {}
    on = bool(arguments.get("on"))
    return _verified_meta(
        "power",
        arguments,
        {"power": on},
        "灯已打开" if on else "灯已关闭",
        on=on,
    )


def led_brightness_execute(arguments: dict = None):
    arguments = arguments if isinstance(arguments, dict) else {}
    try:
        brightness = _validate_range(
            "brightness", arguments.get("brightness"), 0, 100
        )
    except (ValueError, TypeError) as error:
        return _result(str(error), {}), {
            "ok": False,
            "action": "brightness",
            "error": str(error),
        }
    return _verified_meta(
        "brightness",
        arguments,
        {"brightness": brightness},
        "亮度已设为 %d%%" % brightness,
        brightness=brightness,
    )


def led_color_execute(arguments: dict = None):
    arguments = arguments if isinstance(arguments, dict) else {}
    color_name = str(arguments.get("color_name") or "").strip().lower()
    if color_name:
        named = NAMED_COLORS.get(color_name)
        if named is None:
            error = "不支持的 color_name：%s" % color_name
            return _result(error, {}), {
                "ok": False,
                "action": "color",
                "error": error,
            }
        (red, green, blue), spoken_name = named
    else:
        try:
            red = _validate_range("red", arguments.get("red"), 0, 255)
            green = _validate_range("green", arguments.get("green"), 0, 255)
            blue = _validate_range("blue", arguments.get("blue"), 0, 255)
        except (ValueError, TypeError) as error:
            return _result(str(error), {}), {
                "ok": False,
                "action": "color",
                "error": str(error),
            }
        spoken_name = _color_name(red, green, blue)
    payload = {
        "power": True,
        "red": red,
        "green": green,
        "blue": blue,
        "effect": "solid",
    }
    if arguments.get("brightness") is not None:
        try:
            payload["brightness"] = _validate_range(
                "brightness", arguments.get("brightness"), 0, 100
            )
        except (ValueError, TypeError) as error:
            return _result(str(error), {}), {
                "ok": False,
                "action": "color",
                "error": str(error),
            }
    return _verified_meta(
        "color",
        arguments,
        payload,
        "灯已调成%s" % spoken_name,
        red=red,
        green=green,
        blue=blue,
        color_name=color_name or None,
    )


def led_effect_execute(arguments: dict = None):
    arguments = arguments if isinstance(arguments, dict) else {}
    effect = str(arguments.get("effect") or "").strip()
    if effect not in EFFECTS:
        return _result(
            "不支持的 effect，可选：%s" % ", ".join(sorted(EFFECTS)), {}
        ), {
            "ok": False,
            "action": "effect",
            "error": "effect 必须是：%s" % ", ".join(sorted(EFFECTS)),
        }
    payload = {
        "power": True,
        "effect": effect,
        "speed": _validate_range("speed", arguments.get("speed", 50), 1, 100),
    }
    if arguments.get("brightness") is not None:
        payload["brightness"] = _validate_range(
            "brightness", arguments.get("brightness"), 0, 100
        )
    return _verified_meta(
        "effect",
        arguments,
        payload,
        "已切换到%s灯效" % effect,
        effect=effect,
    )


def led_set_execute(arguments: dict = None):
    arguments = arguments if isinstance(arguments, dict) else {}
    payload: dict = {}
    try:
        if arguments.get("power") is not None:
            payload["power"] = bool(arguments.get("power"))
        for name in ("red", "green", "blue"):
            value = arguments.get(name)
            if value is not None:
                payload[name] = _validate_range(name, value, 0, 255)
        if arguments.get("brightness") is not None:
            payload["brightness"] = _validate_range(
                "brightness", arguments.get("brightness"), 0, 100
            )
        if arguments.get("speed") is not None:
            payload["speed"] = _validate_range(
                "speed", arguments.get("speed"), 1, 100
            )
        if arguments.get("count") is not None:
            payload["count"] = _validate_range(
                "count", arguments.get("count"), 1, 300
            )
        effect = arguments.get("effect")
        if effect is not None:
            effect = str(effect).strip()
            if effect not in EFFECTS:
                raise ValueError(
                    "不支持的 effect，可选：%s" % ", ".join(sorted(EFFECTS))
                )
            payload["effect"] = effect
    except (ValueError, TypeError) as error:
        return _result(str(error), {}), {
            "ok": False,
            "action": "set",
            "error": str(error),
        }
    if not payload:
        return _result("至少提供一个需要修改的参数", {}), {
            "ok": False,
            "action": "set",
            "error": "至少提供一个需要修改的参数",
        }
    return _verified_meta(
        "set",
        arguments,
        payload,
        "灯光参数已更新",
    )


# ==================== 工具定义 ====================

def led_control_tool_definition(*, slim=False):
    """LED 工具定义。slim=True 返回精简版（低频使用，描述一行）。"""
    if slim:
        return {
            "type": "function",
            "function": {
                "name": "led_control",
                "description": "控制 WS2812 灯带（开关/颜色/亮度/灯效）。常见颜色必须用 color_name，只有自定义颜色才用 RGB。设备不可达回执 ok=false。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["power", "color", "brightness", "effect", "set", "status"],
                            "description": "power(配on)/color(常见色配color_name，自定义色配RGB)/brightness/effect/set/status",
                        },
                        "on": {"type": "boolean", "description": "true 开灯"},
                        "color_name": {
                            "type": "string",
                            "enum": list(NAMED_COLORS),
                            "description": "常见颜色的确定性映射；用户说颜色名时必须使用本字段",
                        },
                        "red": {"type": "integer", "description": "0-255"},
                        "green": {"type": "integer", "description": "0-255"},
                        "blue": {"type": "integer", "description": "0-255"},
                        "brightness": {"type": "integer", "description": "0-100"},
                        "effect": {
                            "type": "string",
                            "enum": ["solid", "rainbow", "breathing", "wipe"],
                            "description": "solid/rainbow/breathing/wipe",
                        },
                        "speed": {"type": "integer", "description": "1-100"},
                        "count": {"type": "integer", "description": "1-300"},
                    },
                    "required": ["action"],
                },
            },
        }
    return {
        "type": "function",
        "function": {
            "name": "led_control",
            "description": (
                "控制 WS2812 智能灯带（开关/颜色/亮度/灯效）。"
                "action 必须填写：power 开关灯、color 设纯色（自动开灯）、"
                "brightness 调亮度、effect 切灯效、set 一次改多个参数、status 查当前状态。"
                "用户说常见颜色名时必须用 color_name 的枚举值，由代码确定性映射；"
                "只有用户要自定义颜色时才用 RGB 三通道 0-255。亮度 0-100，speed 1-100。"
                "每次执行都会从设备拿最新状态回执；设备不可达时回执 ok=false，"
                "不要谎称已经调好。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["power", "color", "brightness", "effect", "set", "status"],
                        "description": "power 配 on；color 常见色配 color_name、自定义色配 RGB；brightness 配 brightness；effect 配 effect；set 改多个；status 查询。",
                    },
                    "on": {
                        "type": "boolean",
                        "description": "power 用：true 开灯，false 关灯。",
                    },
                    "color_name": {
                        "type": "string",
                        "enum": list(NAMED_COLORS),
                        "description": "常见颜色的确定性映射。用户说红/绿/蓝/黄/紫/青/白/橙/粉/暖白时必须使用本字段。",
                    },
                    "red": {"type": "integer", "description": "红色通道 0-255。"},
                    "green": {"type": "integer", "description": "绿色通道 0-255。"},
                    "blue": {"type": "integer", "description": "蓝色通道 0-255。"},
                    "brightness": {"type": "integer", "description": "亮度百分比 0-100。"},
                    "effect": {
                        "type": "string",
                        "enum": ["solid", "rainbow", "breathing", "wipe"],
                        "description": "solid 纯色；rainbow 彩虹；breathing 呼吸；wipe 流水。",
                    },
                    "speed": {"type": "integer", "description": "灯效速度 1-100。"},
                    "count": {"type": "integer", "description": "灯珠数 1-300。"},
                },
                "required": ["action"],
            },
        },
    }


def tool_definitions():
    return [led_control_tool_definition()]


def execute(name: str, arguments: dict = None):
    """动作流/聊天工具统一入口。返回 (text, meta)。"""
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "led_control":
        return led_control_execute(arguments)
    return "led_skill: unknown action %s." % name, {"ok": False, "action": name}


def led_control_execute(arguments: dict = None):
    """led_control 分发：把 action 字段映射到具体动作执行。"""
    arguments = arguments if isinstance(arguments, dict) else {}
    action = str(arguments.get("action") or "").strip()
    if action == "status":
        return led_status_execute(arguments)
    if action == "power":
        return led_power_execute(arguments)
    if action == "brightness":
        return led_brightness_execute(arguments)
    if action == "color":
        return led_color_execute(arguments)
    if action == "effect":
        return led_effect_execute(arguments)
    if action == "set":
        return led_set_execute(arguments)
    return _result("未知的 led_control action：%s" % action, {}), {
        "ok": False,
        "action": action,
        "error": "未知的 action",
    }


def register(registry, *, wrapper=None):
    """注册进 action_registry。wrapper(fn, name) 可选：自定义执行包装。"""
    def fn(args, ctx, _name="led_control"):
        ctx = ctx if isinstance(ctx, dict) else {}
        return execute(_name, args)

    final_fn = wrapper(fn, "led_control") if wrapper else fn
    registry.register("led_control", final_fn, conflicts=None)
