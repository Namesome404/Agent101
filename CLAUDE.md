# Muse 项目 · 工作约定

基于 `server` 改造的自托管多智能体语音·感知·对话中枢。
核心引擎 `main/server`（ws:8000 / ota:8003），管理后台 `main/EV`（FastAPI+SQLite，:8002）。

## 提交约定（重要）

**每完成一处修改就立即 `git commit`**，不要攒着。原因：本机曾因非正常重启把未提交的
`db.py`/`index.html` 清零丢失，频繁提交是唯一可靠的防丢手段。

- 直接提交到当前分支（`main`），无需先问，除非用户另有要求。
- 一次逻辑改动 = 一次提交，提交信息用中文、说清「改了什么」。
- 提交信息结尾附：`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

## 提交前安全检查（每次必做）

- **绝不提交密钥**。以下已在 `.gitignore`，确认它们不在暂存区：
  `config.yaml`、`data/.config.yaml`、`*.db`、`*.db.bak-*`、`*.db.rescue`。
- 提交前扫一遍暂存文件有没有明文 `sk-` 开头的 key：
  `git diff --cached | grep -nE "sk-[a-zA-Z0-9]{20,}"`（有命中就撤下该文件）。
- `data/pose_landmarker.task` 等大二进制模型不提交（已忽略）。

## 数据安全

- `main/EV/muse.db` 是唯一的数据源（智能体/设备），已 gitignore。改 schema 前先 `cp` 备份。
- 备份文件命名 `muse.db.bak-<时间>`，留在本地即可，已被忽略。

## 摄像头语音终端（camera_voice.py）

- 半双工：摄像头麦 → webrtcvad 分段 → MiMo ASR → Muse 试聊(LLM) → MiniMax TTS → 出声。
- 不使用本地唤醒模型；麦克风启用时为持续会话，静音由设备页麦克风开关控制。生产环境只从 `run_muse.sh` 启动，控制面与语音终端统一使用 `server/main/server/.venv`。
- 依赖：`aiortc`、`webrtcvad-wheels`、系统 `ffmpeg`（choco）。麦克风取流用 ffmpeg 拉 go2rtc RTSP 音频轨（稳，勿加 aresample/wallclock 会不出数据）。
- **输出默认走本机喇叭**（`CAMERA_VOICE_OUTPUT=pc`）。原因见下——摄像头喇叭那条是坏的。
- **火山 TTS 用 `DoubaoTTS`（HTTP 一次性版），不要用 `HuoshanDoubleStreamTTS`**。后者是 WebSocket 双向流式，
  `text_to_speak(text,_)` 忽略输出文件、要先建 WS，Muse 的 `tts_preview.py` 一次性路径驱动不了它，报"WebSocket连接不存在/未生成音频"。
  DoubaoTTS 需 appid/access_token/`cluster: volcano_tts`/voice(湾湾小何=`zh_female_wanwanxiaohe_moon_bigtts`)。
  要真用双向流式低延迟，得改 camera_voice 直接驱动火山 WS 边合成边播。
- **双向流式低延迟(句间无gap)已做成通用适配器**：`tts_duplex.py`(Muse侧桥接,不import核心)+
  `tts_duplex_worker.py`(子进程,在 server 上下文用 conn 垫片驱动任意核心流式 provider,opus→PCM)。
  `/api/tts/duplex` 非 minimax 流式走它。**火山用 `HuoshanDoubleStreamTTS`(V1,服务10029),已验证首PCM~1.7s、无gap**;
  `V2`(seed-tts-2.0/服务10035)账号没开会 403。依赖 `opuslib`。
  两个坑记牢：核心模块级会拉 manager-api 配置(无法 in-process import,故走子进程);
  Windows/uvicorn 下 asyncio 子进程管道不通(用 Popen+线程);worker 把真stdout留给二进制帧、日志重定向到stderr。

### ⚠️ 小米摄像头喇叭（backchannel）不可用，别再折腾

- 现象：往 `chuangmi.camera.039a01` 推 TTS 全是噪声/机器音；麦克风方向完全正常。
- 已定位：音频经 go2rtc 确实发到了摄像头(senders 有字节流)，是摄像头喇叭把 go2rtc 的 OPUS 渲染成噪声。
- 根因：go2rtc 的小米 backchannel 是**逐型号**实现，v1.9.13 只修了 `72ac1`/`hlc6`，**039a01 未支持**（查证 go2rtc issue #2050 无解）。外部调采样率/声道都改不到（逻辑在 go2rtc 二进制里）。
- blurams 摄像头也不行（双向对讲 App 独占，不出 RTSP/ONVIF backchannel）。
- 要摄像头出声只能换支持 ONVIF 双向音频的机器（Reolink/Amcrest/大华），届时 `CAMERA_VOICE_OUTPUT=camera` 即切回。

## LED 灯带（ESP32 WS2812）

- 设备：`ws2812-lan-control` 固件（仓库根 `ws2812_led/`），ESP32 GPIO27 直驱 WS2812 数字灯带。
  主机名 `ws2812.local`（最近 IP `192.168.2.81`，DHCP 可能变化），MAC `1c:c3:ab:f9:e0:28`。
- 独立 MCP 服务 `led_mcp_server.py`（FastMCP，`:8012/mcp`，devices/esp32 下）代理 ESP32 HTTP API，
  `EV.bat` 自启并注册 `muse-led` 到核心 `data/.mcp_server_settings.json`，工具
  `muse-led_led_status / led_power / led_brightness / led_color / led_effect / led_set`。
- 设备地址默认 `http://ws2812.local`，`.env` 的 `LED_DEVICE_URL` 可覆盖；灯效 `solid/rainbow/breathing/wipe`，
  `speed` 1-100，`count` 1-300（亮度 0 即全灭）。
- 注意：旧模拟灯带固件 `esp32_led/`（`ev-led.local`）已删除停用，别再往 `.env` 填 `ev-led.local`。

## 环境备注

- Python 走 `main/server/.venv`（Windows，opus.dll 已就位）。
- 本地 Ollama（:11434）跑本地文本模型；go2rtc（:1984，Desktop 的 exe+yaml）出摄像头流。
- Windows 控制台是 GBK，打印中文/emoji 会报错——脚本里用 `python -X utf8` 或写文件再读。
