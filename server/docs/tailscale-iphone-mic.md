# iPhone 当 Muse 麦克风/摄像头（Tailscale HTTPS）

## 为什么需要 HTTPS

iPhone 浏览器只在 **安全上下文（HTTPS）** 下允许 `getUserMedia`（麦克风/摄像头）。  
局域网 `http://192.168.x.x` 不行；经 **Tailscale HTTPS** 可以，且将来上云只需换域名。

## 架构

```
iPhone Safari ──HTTPS──► Tailscale Serve :443
                              │
                              ▼
                         Muse :8002
                    ┌─────────┴─────────┐
                    │ /terminal  终端页  │
                    │ /xiaozhi/ota/ OTA │
                    │ /xiaozhi/v1/  WS ─┼──► 核心 :8000
                    │ /mcp/vision/* ────┼──► 核心 :8003
                    └───────────────────┘

ESP32 ──HTTP 局域网──► 核心 :8003 OTA ──► ws://局域网IP:8000/...
```

## 一次性准备

1. **Windows + iPhone** 安装 Tailscale，登录**同一 tailnet**（可用 Google 登录）。
2. Tailscale 管理后台 → [DNS](https://login.tailscale.com/admin/dns)：
   - 开启 **MagicDNS**
   - 开启 **HTTPS Certificates**
3. 本机启动 Muse(8002) + 核心(8000/8003)。

## 启动 HTTPS 入口

```powershell
cd xiaozhi-esp32-server\scripts
.\tailscale_serve.ps1
```

脚本会把 `https://<你的机器>.<tailnet>.ts.net/` 反代到 Muse `8002`。

重置：`.\tailscale_serve.ps1 -Reset`

## iPhone 使用（精简：仅麦克风 + 摄像头，并入电脑同一会话）

**顺序很重要：先电脑，后 iPhone。**

1. **电脑端**：打开 Muse 智能体 → 点 **「轻触开始」**，等连接成功。
2. **iPhone**：Tailscale 已连接 → Safari 打开 **`https://<host>.ts.net/remote/1`**
3. 点 **「连接麦克风与摄像头」** → 允许权限。
4. 显示「已并入电脑会话」后，对着 iPhone 说话；识别与回复走**电脑端同一会话**（回复在电脑播放）。

无需 6 位绑定码，无需完整配置界面。

## 电脑端完整终端（可选）

Safari/Chrome 访问 `https://<host>.ts.net/#/terminal/1` → 「轻触开始」。

## 验证

```powershell
# OTA 经 HTTPS 代理应返回 wss
curl -s -H "X-Forwarded-Proto: https" -H "X-Forwarded-Host: desktop-xxx.ts.net" `
  http://127.0.0.1:8002/xiaozhi/ota/

# 局域网 ESP32 仍返回 ws://局域网IP:8000/...
curl -s http://127.0.0.1:8003/xiaozhi/ota/ -H "device-id: aa:bb:cc:dd:ee:ff" -H "client-id: test" -d "{}"
```

## 配置说明

- `data/.config.yaml` 中 `server.websocket` / `vision_explain` 使用占位符 → OTA 自动**跟随 Host**。
- `muse-bridge.js` 的 OTA 地址使用 `location.origin`，与 HTTPS 同源。
- 上云：部署到云服务器 + 域名证书，无需改代码，OTA 仍跟随 Host。
