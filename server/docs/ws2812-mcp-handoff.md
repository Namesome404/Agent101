# ESP32 WS2812 API 与 MCP 接入交接文档

> 交给另一台设备或另一个 AI/开发者。目标：把已经运行的 ESP32 WS2812 控制器，以 MCP 工具形式接入现有智能助手项目。

> **已接入本项目（2026-08-04）**：EV 侧 `led_mcp_server.py` 已按本固件协议重写，
> 端口用项目约定 **8012**（非文中的 8013），已注册 `muse-led` 到
> `server/main/server/data/.mcp_server_settings.json`（`EV.bat` 自启）。
> 设备地址默认 `http://ws2812.local`，`.env` 里 `LED_DEVICE_URL` 可覆盖。

## 1. 已完成的设备能力

ESP32 固件已经烧录并验证过，ESP32 自己提供网页与 HTTP JSON API，不需要电脑常驻才能控制灯带。

当前硬件和固件参数：

| 项目 | 值 |
|---|---|
| 芯片 | ESP32-D0WD-V3，双核 240MHz，4MB Flash |
| USB 串口芯片 | CP2102 |
| ESP32 MAC | `1c:c3:ab:f9:e0:28` |
| WS2812 数据引脚 | `D27 / GPIO27` |
| WS2812 色序 | `GRB` |
| 最大软件灯珠数量 | 300 |
| 当前默认灯珠数量 | 60，可由网页/API修改 |
| 最大软件电流限制 | 5V / 3000mA |
| 主机名 | `ws2812.local` |
| 最近使用的局域网 IP | `192.168.2.81`，DHCP 可能变化 |
| HTTP 端口 | 80 |
| API 鉴权 | 无，仅允许在可信局域网使用 |

固件源项目（原电脑）：

```text
/Users/syz/Documents/Codex/2026-08-04/wo-xia/outputs/ws2812-lan-control
```

网页入口：

```text
http://ws2812.local/
```

如果 `.local` 在目标设备上无法解析，从路由器的 DHCP/设备列表中根据 MAC `1c:c3:ab:f9:e0:28` 查找当前 IP。建议在路由器上给该 MAC 做 DHCP 地址保留。

## 2. 整体架构

```text
智能 AI 助手
    ↓ MCP streamable-http
另一台设备上运行的 MCP Server（默认端口 8013）
    ↓ 局域网 HTTP JSON
ESP32（ws2812.local:80）
    ↓ GPIO27
WS2812 灯带
```

注意两个地址不能混淆：

1. `LED_DEVICE_URL` 是 ESP32 地址，例如 `http://ws2812.local` 或 `http://192.168.2.81`。
2. Agent 项目的 MCP 配置 URL 是运行 Python MCP Server 的那台设备地址，例如 `http://192.168.2.50:8013/mcp`，不是 ESP32 地址。

## 3. ESP32 HTTP API

### 3.1 健康检查

```http
GET /health
```

成功响应：

```text
ok
```

### 3.2 查询灯光状态

```http
GET /api/led/status
```

示例：

```bash
curl http://ws2812.local/api/led/status
```

返回格式：

```json
{
  "power": true,
  "red": 255,
  "green": 0,
  "blue": 0,
  "brightness": 15,
  "effect": "solid",
  "speed": 50,
  "count": 60,
  "pin": 27,
  "ip": "192.168.2.81",
  "hostname": "ws2812.local",
  "rssi": -69
}
```

### 3.3 修改灯光状态

```http
POST /api/led/state
Content-Type: application/json
```

所有字段都是可选的，可以只传需要修改的字段：

| 字段 | 类型/范围 | 说明 |
|---|---|---|
| `power` | boolean | 开灯或关灯 |
| `red` | integer，0–255 | 红色分量 |
| `green` | integer，0–255 | 绿色分量 |
| `blue` | integer，0–255 | 蓝色分量 |
| `brightness` | integer，0–100 | 亮度百分比 |
| `effect` | string | `solid`、`rainbow`、`breathing`、`wipe` |
| `speed` | integer，1–100 | 动态灯效速度 |
| `count` | integer，1–300 | 实际控制的灯珠数量 |

设置会写入 ESP32 的持久存储，重新通电后仍会保留。

开灯：

```bash
curl -X POST http://ws2812.local/api/led/state \
  -H 'Content-Type: application/json' \
  -d '{"power":true}'
```

关灯：

```bash
curl -X POST http://ws2812.local/api/led/state \
  -H 'Content-Type: application/json' \
  -d '{"power":false}'
```

设置暖橙色、30% 亮度：

```bash
curl -X POST http://ws2812.local/api/led/state \
  -H 'Content-Type: application/json' \
  -d '{"power":true,"red":255,"green":100,"blue":20,"brightness":30,"effect":"solid"}'
```

设置彩虹效果：

```bash
curl -X POST http://ws2812.local/api/led/state \
  -H 'Content-Type: application/json' \
  -d '{"power":true,"brightness":30,"effect":"rainbow","speed":60}'
```

成功时返回修改后的完整状态 JSON。无效 JSON 返回 HTTP 400：

```json
{"error":"invalid_json"}
```

不存在的路径返回 HTTP 404：

```json
{"error":"not_found"}
```

## 4. MCP Server 运行要求

推荐环境：

- Python 3.10–3.13
- `mcp==1.22.0`
- `requests==2.32.5`
- transport：`streamable-http`
- MCP 默认端口：`8013`
- MCP 监听地址：`0.0.0.0`，否则其他局域网设备无法连接

`requirements.txt`：

```text
mcp==1.22.0
requests==2.32.5
```

## 5. 可直接使用的 MCP Server

创建 `led_mcp_server.py`：

```python
import json
import os
from typing import Optional

import requests
from mcp.server.fastmcp import FastMCP

DEVICE_URL = os.getenv("LED_DEVICE_URL", "http://ws2812.local").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("LED_REQUEST_TIMEOUT", "3"))

mcp = FastMCP("muse-led", host="0.0.0.0", port=8013)


def _validate_range(name: str, value: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    try:
        response = requests.request(
            method,
            f"{DEVICE_URL}{path}",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"无法连接灯光设备 {DEVICE_URL}: {exc}") from exc


def _result(message: str, state: dict) -> str:
    return json.dumps(
        {"message": message, "state": state},
        ensure_ascii=False,
    )


@mcp.tool()
def led_status() -> str:
    """查询 WS2812 灯的开关、颜色、亮度、灯效、速度和在线状态。"""
    return _result("已读取灯光状态", _request("GET", "/api/led/status"))


@mcp.tool()
def led_power(on: bool) -> str:
    """打开或关闭 WS2812 灯。on=true 开灯，on=false 关灯。"""
    state = _request("POST", "/api/led/state", {"power": on})
    return _result("灯已打开" if on else "灯已关闭", state)


@mcp.tool()
def led_brightness(brightness: int) -> str:
    """设置灯光亮度百分比，brightness 范围为 0 到 100。"""
    brightness = _validate_range("brightness", brightness, 0, 100)
    state = _request("POST", "/api/led/state", {"brightness": brightness})
    return _result(f"亮度已设为 {brightness}%", state)


@mcp.tool()
def led_color(red: int, green: int, blue: int, brightness: Optional[int] = None) -> str:
    """设置 RGB 颜色。red、green、blue 范围均为 0 到 255；可选 brightness 为 0 到 100。设置颜色时会开灯并切换到纯色模式。"""
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
def led_effect(effect: str, speed: int = 50, brightness: Optional[int] = None) -> str:
    """设置灯效。effect 可选 solid、rainbow、breathing、wipe；speed 范围 1 到 100；brightness 可选 0 到 100。"""
    allowed = {"solid", "rainbow", "breathing", "wipe"}
    if effect not in allowed:
        raise ValueError(f"effect 必须是：{', '.join(sorted(allowed))}")
    payload = {
        "power": True,
        "effect": effect,
        "speed": _validate_range("speed", speed, 1, 100),
    }
    if brightness is not None:
        payload["brightness"] = _validate_range("brightness", brightness, 0, 100)
    state = _request("POST", "/api/led/state", payload)
    return _result(f"已切换到 {effect} 灯效", state)


@mcp.tool()
def led_set(
    power: Optional[bool] = None,
    red: Optional[int] = None,
    green: Optional[int] = None,
    blue: Optional[int] = None,
    brightness: Optional[int] = None,
    effect: Optional[str] = None,
    speed: Optional[int] = None,
) -> str:
    """一次设置多个灯光参数。只传需要修改的字段；RGB 为 0 到 255，亮度为 0 到 100，速度为 1 到 100。"""
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
    if effect is not None:
        if effect not in {"solid", "rainbow", "breathing", "wipe"}:
            raise ValueError("不支持的 effect")
        payload["effect"] = effect
    if not payload:
        raise ValueError("至少提供一个需要修改的参数")
    return _result("灯光参数已更新", _request("POST", "/api/led/state", payload))


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

## 6. 安装与启动 MCP

在另一台设备上：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
LED_DEVICE_URL=http://ws2812.local python led_mcp_server.py
```

如果目标系统解析不了 mDNS，改用实际 IP：

```bash
LED_DEVICE_URL=http://192.168.2.81 python led_mcp_server.py
```

Windows PowerShell：

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:LED_DEVICE_URL = "http://192.168.2.81"
python led_mcp_server.py
```

## 7. 注册进现有 Agent 项目

假设运行 MCP Server 的另一台设备局域网 IP 是 `192.168.2.50`，配置为：

```json
{
  "mcpServers": {
    "muse-led": {
      "url": "http://192.168.2.50:8013/mcp",
      "transport": "streamable-http"
    }
  }
}
```

必须把 `192.168.2.50` 换成实际运行 MCP Server 的设备 IP。不要填 `127.0.0.1`，除非 Agent 与 MCP Server 确实在同一台设备、同一网络环境内。

如果接入的是已有 EV 项目，按现有约定写入：

```text
server/main/server/data/.mcp_server_settings.json
```

Agent 最终应能看到类似工具：

```text
muse-led_led_status
muse-led_led_power
muse-led_led_brightness
muse-led_led_color
muse-led_led_effect
muse-led_led_set
```

## 8. 交付自测

先测试目标设备能访问 ESP32：

```bash
curl http://ws2812.local/health
curl http://ws2812.local/api/led/status
```

再测试颜色控制：

```bash
curl -X POST http://ws2812.local/api/led/state \
  -H 'Content-Type: application/json' \
  -d '{"power":true,"red":0,"green":80,"blue":255,"brightness":20,"effect":"solid"}'
```

检查 MCP 端口是否监听：

```bash
curl -i http://127.0.0.1:8013/mcp
```

MCP 的 GET 请求不一定像普通 REST API 一样返回 200 页面；关键是服务正在监听、MCP 客户端能完成 streamable-http 初始化并列出工具。

最后在 Agent 中依次测试：

1. 查询灯光状态。
2. 把灯打开。
3. 调成蓝色、亮度 20%。
4. 切换到彩虹效果、速度 60%。
5. 关闭灯光。

## 9. 常见故障

### ESP32 地址打不开

- 确认 ESP32 已通电。
- MCP 主机和 ESP32 必须在可互访的同一局域网/VLAN。
- `.local` 不可用时按 MAC `1c:c3:ab:f9:e0:28` 查路由器设备列表。
- ESP32 最近地址 `192.168.2.81` 只是 DHCP 地址，可能发生变化。
- 设备只支持 2.4GHz Wi-Fi，但 5GHz 手机/电脑通常只要路由器允许频段互访也可访问。

### API 正常但灯不响应

- 固件使用 `GPIO27`，数据线必须接 `D27/GPIO27`。
- 数据线必须接 WS2812 的 `DIN`，不能接 `DOUT`。
- ESP32 GND、灯带 GND、外部 5V 电源负极必须共地。
- ESP32 数据是 3.3V；若不稳定，增加 `74AHCT125` 或 `74HCT14` 电平转换。
- 万用表测数据线接近 0V 是正常的，WS2812 数据是短时高速脉冲。

### MCP 本机能用，其他设备连不上

- FastMCP 必须使用 `host="0.0.0.0"`。
- 放行 TCP 8013 入站防火墙。
- Agent 配置使用 MCP 主机的局域网 IP，不能错误使用 ESP32 IP。
- MCP URL 结尾是 `/mcp`。

## 10. 安全边界

当前 ESP32 API 没有 Token、TLS 或用户鉴权，只适用于可信局域网：

- 不要在路由器上把 ESP32 的 80 端口映射到公网。
- 不要直接把 MCP 的 8013 端口暴露到公网。
- 需要跨网络控制时，应通过 VPN/Tailscale、反向代理鉴权或在 MCP 服务层加入访问控制。
- MCP Server 中的 ESP32 地址必须是固定配置，不允许模型传入任意 URL，避免被用来访问局域网其他设备。

## 11. 完成标准

- [ ] 目标设备可以访问 ESP32 `/health` 和 `/api/led/status`
- [ ] HTTP POST 可以实际改变灯光
- [ ] MCP Server 监听 `0.0.0.0:8013`
- [ ] Agent 配置指向 MCP 主机的 `<局域网IP>:8013/mcp`
- [ ] Agent 能列出全部 `muse-led_*` 工具
- [ ] 开关、亮度、RGB 颜色和四种灯效均实测成功
- [ ] ESP32 与 MCP 主机 IP 已做 DHCP 地址保留，或使用稳定的主机名

