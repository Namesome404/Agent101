# -*- coding: utf-8 -*-
"""ESP32 WS2812 灯带 MCP 服务（streamable-http，:8012/mcp）。

把 ws2812-lan-control 固件（ESP32 GPIO27 直驱 WS2812 数字灯带，见仓库根
ws2812_led/）的 HTTP JSON API 暴露成标准 MCP 工具，供核心 server / EV 语音
Agent 统一调用。

设备地址默认 http://ws2812.local（mDNS），可用 .env 里的 LED_DEVICE_URL
覆盖为具体局域网 IP（DHCP 变化时），或 LED_MCP_PORT 换端口。
"""

import json
import os
from typing import Optional

import requests
from mcp.server.fastmcp import FastMCP

from common.paths import ENV_PATH

EFFECTS = {"solid", "rainbow", "breathing", "wipe"}


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

HOST = os.environ.get("LED_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("LED_MCP_PORT", "8012"))
DEVICE_URL = os.environ.get(
    "LED_DEVICE_URL",
    "http://ws2812.local",
).rstrip("/")
REQUEST_TIMEOUT = float(os.environ.get("LED_REQUEST_TIMEOUT", "2.5"))
HTTP = requests.Session()
HTTP.trust_env = False

mcp = FastMCP("muse-led", host=HOST, port=PORT)


def _validate_range(name: str, value: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 必须是整数" % name)
    if not minimum <= value <= maximum:
        raise ValueError("%s 必须在 %d 到 %d 之间" % (name, minimum, maximum))
    return value


def _request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    try:
        response = HTTP.request(
            method,
            DEVICE_URL + path,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        raise RuntimeError(
            "无法连接灯光设备 %s：%s" % (DEVICE_URL, error)
        ) from error


def _result(message: str, state: dict) -> str:
    return json.dumps(
        {"message": message, "state": state},
        ensure_ascii=False,
    )


@mcp.tool()
def led_status() -> str:
    """查询 WS2812 灯带的开关、颜色、亮度、灯效、速度与在线状态。"""
    return _result("已读取灯光状态", _request("GET", "/api/led/status"))


@mcp.tool()
def led_power(on: bool) -> str:
    """打开或关闭 WS2812 灯带。on=true 开灯，on=false 关灯。"""
    state = _request("POST", "/api/led/state", {"power": on})
    return _result("灯已打开" if on else "灯已关闭", state)


@mcp.tool()
def led_brightness(brightness: int) -> str:
    """设置灯光亮度百分比，brightness 范围为 0 到 100。"""
    brightness = _validate_range("brightness", brightness, 0, 100)
    state = _request("POST", "/api/led/state", {"brightness": brightness})
    return _result("亮度已设为 %d%%" % brightness, state)


@mcp.tool()
def led_color(
    red: int,
    green: int,
    blue: int,
    brightness: Optional[int] = None,
) -> str:
    """设置 RGB 颜色。red、green、blue 范围均为 0 到 255；可选 brightness 为 0 到 100。
    设置颜色时会开灯并切换到纯色模式。"""
    payload = {
        "power": True,
        "red": _validate_range("red", red, 0, 255),
        "green": _validate_range("green", green, 0, 255),
        "blue": _validate_range("blue", blue, 0, 255),
        "effect": "solid",
    }
    if brightness is not None:
        payload["brightness"] = _validate_range("brightness", brightness, 0, 100)
    state = _request("POST", "/api/led/state", payload)
    return _result("颜色已设置", state)


@mcp.tool()
def led_effect(
    effect: str,
    speed: int = 50,
    brightness: Optional[int] = None,
) -> str:
    """设置灯效。effect 可选 solid(纯色)、rainbow(彩虹)、breathing(呼吸)、wipe(流水)；
    speed 范围 1 到 100；brightness 可选 0 到 100。"""
    if effect not in EFFECTS:
        raise ValueError("effect 必须是：%s" % ", ".join(sorted(EFFECTS)))
    payload = {
        "power": True,
        "effect": effect,
        "speed": _validate_range("speed", speed, 1, 100),
    }
    if brightness is not None:
        payload["brightness"] = _validate_range("brightness", brightness, 0, 100)
    state = _request("POST", "/api/led/state", payload)
    return _result("已切换到 %s 灯效" % effect, state)


@mcp.tool()
def led_set(
    power: Optional[bool] = None,
    red: Optional[int] = None,
    green: Optional[int] = None,
    blue: Optional[int] = None,
    brightness: Optional[int] = None,
    effect: Optional[str] = None,
    speed: Optional[int] = None,
    count: Optional[int] = None,
) -> str:
    """一次设置多个灯光参数。只传需要修改的字段；RGB 为 0 到 255，亮度为 0 到 100，
    速度为 1 到 100，灯珠数 count 为 1 到 300。"""
    payload: dict = {}
    if power is not None:
        payload["power"] = power
    for name, value in (("red", red), ("green", green), ("blue", blue)):
        if value is not None:
            payload[name] = _validate_range(name, value, 0, 255)
    if brightness is not None:
        payload["brightness"] = _validate_range("brightness", brightness, 0, 100)
    if speed is not None:
        payload["speed"] = _validate_range("speed", speed, 1, 100)
    if count is not None:
        payload["count"] = _validate_range("count", count, 1, 300)
    if effect is not None:
        if effect not in EFFECTS:
            raise ValueError("不支持的 effect，可选：%s" % ", ".join(sorted(EFFECTS)))
        payload["effect"] = effect
    if not payload:
        raise ValueError("至少提供一个需要修改的参数")
    return _result("灯光参数已更新", _request("POST", "/api/led/state", payload))


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
