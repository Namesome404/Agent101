# WS2812 局域网控制器

ESP32 直接提供手机网页，不需要额外电脑或服务器常驻运行。

> 本目录是 `ws2812-lan-control` 固件的仓库副本（原机器 Codex 输出目录会清空）。
> 已在 EV 项目接好 MCP：`server/main/EV/devices/esp32/led_mcp_server.py`
> 把本固件的 HTTP API 暴露成 `muse-led_*` 工具（:8012/mcp），见
> `server/docs/ws2812-mcp-handoff.md`。
> 已烧录设备：MAC `1c:c3:ab:f9:e0:28`，默认 `http://ws2812.local`，最近 IP `192.168.2.81`。

## 当前接线

- ESP32 `D27/GPIO27` → 330–470Ω 电阻 → WS2812 `DIN`
- ESP32 `GND`、灯带 `GND`、5V 电源负极共地
- 外部 5V 电源正极 → 灯带 `5V`

## 第一次使用

1. ESP32 启动后，手机连接 Wi-Fi：`WS2812-Setup`
2. 热点密码：`ws2812setup`
3. 在自动弹出的页面中选择家里的 Wi-Fi，输入密码
4. 手机切回家里的 Wi-Fi
5. 打开 `http://ws2812.local`

如果 `.local` 打不开，可在串口日志或路由器设备列表中查看 ESP32 的 IP，并访问 `http://设备IP`。

网页可设置开关、颜色、亮度、纯色/彩虹/呼吸/流水灯效、灯效速度以及 1–300 颗灯珠。设置会保存在 ESP32 中，重新通电后仍会保留。

## 编译和烧录

开发板通过 `/dev/cu.usbserial-0001` 连接时，在本目录运行：

```sh
pio run
pio run --target upload
pio device monitor
```

## HTTP API

查询状态：

```text
GET /api/led/status
```

修改状态：

```text
POST /api/led/state
Content-Type: application/json

{"power":true,"red":255,"green":100,"blue":20,"brightness":30,"effect":"solid","speed":50}
```

这组 API 与后续 MCP 工具可以直接对接。
