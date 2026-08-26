# esp32_speaker · Muse 网络扬声器固件

把一块 ESP32 + I2S 功放(MAX98357A) 变成 **Muse 语音 AI 的扬声器之一**。
ESP32 作 WebSocket 客户端连到 Muse(`:8002`) 的 `/api/speaker/ws`，Muse 生成 TTS
时把裸 PCM 扇出过来，ESP32 直接写 I2S 出声。**与本机喇叭同时发声**（可在
Muse「设备」页开关和调音量），不影响原有的 camera_voice 语音回路。

## 接线（MAX98357A）

| MAX98357A | ESP32 | 说明 |
|---|---|---|
| DIN  | **GPIO15** (D15) | I2S 数据 |
| BCLK | **GPIO2**  (D2)  | 位时钟 |
| LRC  | **GPIO4**  (D4)  | 左右/字选 (WS) |
| VIN  | 5V | 供电（也可 3V3，声更小） |
| GND  | GND | |
| GAIN / SD | 悬空 | 默认 9dB 增益、常开 |

> ⚠️ BCLK 用到了 GPIO2（部分板子的板载 LED / 启动 strapping 脚）。若烧录后
> 反复重启或不出声，把 BCLK 换到别的脚（改 `main.cpp` 的 `PIN_I2S_BCLK`），
> 例如 GPIO26/27，重新烧录即可。喇叭若完全没声，试把 `channel_format`
> 从 `ONLY_LEFT` 改成 `ONLY_RIGHT`。

## 烧录

装好 [PlatformIO](https://platformio.org/)（VSCode 插件或 CLI），然后：

```bash
cd esp32_speaker
pio run -t upload -t monitor
```

首次会自动装好依赖（WebSockets / ArduinoJson / WiFiManager）。

## 首次配网

1. 上电后 ESP32 开一个热点 **`Muse-Speaker-XXXX`**，手机连上它。
2. 弹出的配置页里选好 WiFi，并填：
   - **Muse 主机 IP**：运行 Muse 的那台电脑的局域网 IP（如 `192.168.1.20`）。
   - **Muse 端口**：默认 `8002`。
   - **扬声器名**：随意，如「客厅音箱」，会显示在 Muse 后台。
3. 保存后自动连网并接入 Muse。

- 想改 WiFi / Muse 地址：**上电时按住 BOOT** 进配置门户；或运行中**长按
  BOOT 3 秒**清空配置重启。

## 在 Muse 里管理

打开 Muse 后台 → **设备** 页 → **网络扬声器** 面板：在线的 ESP32 会自动出现，
可即时 **开启/关闭** 和拖动 **音量**（软增益）。关掉后本机喇叭照常出声。

## 说明 / 限制

- 音频格式：Muse 的 MiniMax TTS 出的 **24kHz / 16-bit / 单声道** 裸 PCM，
  固件按 `start` 控制帧里的 `sample_rate` 自适应。
- 只覆盖 Muse 走 `/api/tts/stream` 与 `/api/tts/duplex` 的**流式 TTS**路径
  （即当前 camera_voice 默认路径）。若智能体改用非 MiniMax 的 TTS（如 Edge），
  该路径不产生流式 PCM，网络扬声器不会出声——这与后端实现绑定，换 TTS 时留意。
- ESP32 跟不上时后端队列会丢最旧的音频（不会卡爆内存），短句 TTS 一般无碍。
