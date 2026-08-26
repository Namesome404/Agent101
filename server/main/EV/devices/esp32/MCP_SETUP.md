# ESP32 接入 MCP — 必要信息清单

> 给另一个 AI / 接手的开发看。让 ESP32 设备能力（LED、传感器等）能被语音 Agent / 写码 Agent 当 MCP 工具调用。
> 协议用 `streamable-http`，局域网走 HTTP 直接可达，不需要换协议。

---

## 1. 运行环境（已验证）

| 项 | 值 |
|---|---|
| Python 解释器 | `server/main/server/.venv` |
| MCP 版本 | `mcp==1.22.0`（已装） |
| HTTP 客户端 | `requests==2.32.5`（已装） |
| 参考骨架 | `server/main/EV/devices/esp32/led_mcp_server.py` |
| 端口约定 | 8012 = LED、**新的从 8013 起** |

## 2. ESP32 设备侧

- 固件项目：仓库根 `ws2812_led/`（PlatformIO，WS2812 数字灯带，GPIO27），旧的模拟灯带 `esp32_led/` 已停用
- 设备 HTTP 地址：默认 `http://ws2812.local`（mDNS），或局域网 IP `http://192.168.2.81`（DHCP 可能变化，建议路由器做地址保留）
- 已有 API 示例：
  - `GET /api/led/status`
  - `POST /api/led/state`（payload：`{power, red, green, blue, brightness, effect, speed, count}`）
  - 灯效：`solid`（纯色）、`rainbow`（彩虹）、`breathing`（呼吸）、`wipe`（流水）；speed 1-100；count 1-300
- 局域网无鉴权，裸 HTTP

## 3. MCP Server 写法（照抄骨架）

```python
from mcp.server.fastmcp import FastMCP
import requests

mcp = FastMCP("muse-led", host="0.0.0.0", port=8013)

@mcp.tool()
def xxx(...) -> str:
    """中文描述，Agent 靠这个决定调不调。"""
    ...

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

- **transport**：`streamable-http`，局域网走 HTTP 没问题
- **host 必须 `0.0.0.0`**，默认 `127.0.0.1` 局域网连不上
- **防火墙**：macOS 首次监听会弹「允许传入连接」；Windows 放行该端口入站

## 4. 注册进 EV（Agent 才能调到）

文件：`server/main/server/data/.mcp_server_settings.json`（`EV.bat` / `runtime/start_muse.sh` 会自备，格式如下）

```json
{
  "mcpServers": {
    "muse-led": {
      "url": "http://127.0.0.1:8012/mcp",
      "transport": "streamable-http"
    }
  }
}
```

- 外层 key（如 `muse-led`）就是 Agent 看到的工具前缀，工具名形如 `muse-led_led_power`
- **URL 必须填局域网地址，不能是 `127.0.0.1`**（LED MCP 与核心 server 同机部署时用 127.0.0.1 即可）
- `led_mcp_server.py` 已在 `EV.bat` 中注册自启（:8012），无需手动启动

### EV 语音动作流：直连 skill（不依赖 MCP 进程）

EV 的 `/api/agents/*/chat/stream` 语音链路通过固定的 `object_control` 协议，
**不经过 8012 MCP 进程，内部适配器直接 HTTP 调设备**：

- 稳定对象 ID 为 `iot.desk-light`；模型只使用 `inspect / apply / invoke`
- `device_control` 与 `led.py` 只作为服务端适配层，不进入语音模型的工具 Schema
- 回调即真相：每次执行从设备拿最新 `state` 回执，设备不可达时回执 `ok=false`，绝不谎报
- 设备地址同样读 `.env` 的 `LED_DEVICE_URL`（默认 `http://ws2812.local`）
- 新增 ESP32 设备或能力时，在运行时对象注册表注册描述符和执行适配器；禁止修改
  `_build_chat_tools`、工具 Schema 或路由提示。模型通过 `object_control.inspect` 动态发现能力

> 8012 MCP 服务仍保留：它是给核心 server（8000）的 agent 用的独立通道。

### LED 工具清单（新固件）

| 工具 | 说明 |
|---|---|
| `led_status` | 查询开关/颜色/亮度/灯效/速度 |
| `led_power(on)` | 开灯或关灯 |
| `led_brightness(brightness)` | 亮度 0-100 |
| `led_color(red, green, blue, brightness?)` | 设颜色（自动开灯+纯色） |
| `led_effect(effect, speed?, brightness?)` | 设灯效，speed 1-100 |
| `led_set(...)` | 一次改多个参数，含 count(1-300) |

## 5. 交付自测清单

- [ ] 本机：`curl http://127.0.0.1:8012/mcp` 能正常握手（GET 返回 406 属正常）
- [ ] 设备可达：`curl http://ws2812.local/api/led/status`
- [ ] `data/.mcp_server_settings.json` 里 URL 是局域网地址
- [ ] Agent 侧能看到 `muse-led_*` 工具并调用成功
- [ ] 开关、亮度、RGB 颜色与 `solid/rainbow/breathing/wipe` 四种灯效均实测成功
