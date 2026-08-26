# ESP32 devices

ESP32 voice terminals use the standard voice-core (`server`) WebSocket protocol.
Device firmware currently lives in the repository-level `esp32_speaker` project.

This directory is reserved for Muse-side ESP32 adapters and orchestration only;
firmware and server protocol implementations stay in their existing projects.

`led_mcp_server.py` exposes the repository-level `ws2812_led` controller
(ESP32 GPIO27 直驱 WS2812 数字灯带) as agent-callable MCP tools. Its default
device URL is `http://ws2812.local`（可用 `.env` 的 `LED_DEVICE_URL` 覆盖）。
运行：`EV.bat` 会自启在 :8012；手动 `python -m devices.esp32.led_mcp_server`。
