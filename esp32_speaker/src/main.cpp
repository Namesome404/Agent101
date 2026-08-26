// Muse 网络扬声器 · ESP32 + MAX98357A(I2S)
// 作为语音 AI 的扬声器之一：作 WS 客户端连到 Muse(/api/speaker/ws)，
// 收 Muse 扇出的裸 PCM(16-bit LE 单声道) 直接写 I2S 出声。
//
// 接线（按你的板子丝印 D15/D2/D4，即 GPIO15/2/4）：
//   MAX98357A  DIN  -> GPIO15
//              BCLK -> GPIO2
//              LRC  -> GPIO4      (I2S 的 WS/word-select)
//              VIN  -> 5V   GND -> GND   (GAIN/SD 悬空即可)
//
// 首次上电或连不上 WiFi：手机连热点 "Muse-Speaker-XXXX"，在弹出的
// 配置页填 WiFi、Muse 主机(局域网 IP，如 192.168.1.20)、端口(默认 8002)、
// 扬声器名。之后自动记住。长按 BOOT(GPIO0) 3 秒可清配置重来。

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <math.h>
#include "driver/i2s.h"
#include "driver/dac.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// 1 = 内置DAC直推喇叭测试(不接功放，喇叭接 GPIO25 或 GPIO26 + GND)；0 = 正常 I2S 功放
#define TEST_DAC 0

// ---- I2S 引脚（你的接线）----
// 全部用干净 IO，避开 strapping(0/2/4/5/12/15) 与板载LED，杜绝板载上下拉干扰
static const int PIN_I2S_DOUT = 25;  // DIN
static const int PIN_I2S_BCLK = 26;  // BCLK
static const int PIN_I2S_LRC  = 27;  // LRC / WS
static const i2s_port_t I2S_PORT = I2S_NUM_0;
static const int PIN_AMP_SD = 33;    // 功放 SD/使能：接 D33，拉高=开启
static const int PIN_AMP_GAIN = 32;  // 功放 GAIN：接 D32（可不接=9dB），拉低=15dB

static const int PIN_BOOT_BTN = 0;   // 长按清配置

// ---- 运行配置（存 NVS）----
Preferences prefs;
String museHost = "";
uint16_t musePort = 8002;
String spkName = "";
static const char *WS_PATH = "/api/speaker/ws";

WebSocketsClient webSocket;
bool wsConnected = false;
int curSampleRate = 24000;
bool shouldSaveConfig = false;

// ---------------- I2S ----------------
static void i2sInit(int sampleRate) {
  i2s_config_t cfg = {
      .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
      .sample_rate = (uint32_t)sampleRate,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      // 立体声：两个声道槽都填相同样本，兼容 MAX98357A 任意声道选择(L/R/(L+R)/2)
      .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 8,
      .dma_buf_len = 256,
      .use_apll = false,
      .tx_desc_auto_clear = true,
      .fixed_mclk = 0,
  };
#if TEST_DAC
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_DAC_BUILT_IN);
  cfg.communication_format = I2S_COMM_FORMAT_STAND_MSB;
  esp_err_t rc_install = i2s_driver_install(I2S_PORT, &cfg, 0, NULL);
  esp_err_t rc_pin = i2s_set_pin(I2S_PORT, NULL);
  i2s_set_dac_mode(I2S_DAC_CHANNEL_BOTH_EN);
  Serial.printf("[i2s] 内置DAC测试模式 install=%s pin=%s；喇叭直接接 GPIO25 或 GPIO26 + GND（不接功放）\n",
                esp_err_to_name(rc_install), esp_err_to_name(rc_pin));
#else
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_I2S_BCLK;
  pins.ws_io_num = PIN_I2S_LRC;
  pins.data_out_num = PIN_I2S_DOUT;
  pins.data_in_num = I2S_PIN_NO_CHANGE;
  esp_err_t rc_install = i2s_driver_install(I2S_PORT, &cfg, 0, NULL);
  esp_err_t rc_pin = i2s_set_pin(I2S_PORT, &pins);
  Serial.printf("[i2s] driver_install=%s  set_pin=%s  (DOUT=%d BCLK=%d LRC=%d)\n",
                esp_err_to_name(rc_install), esp_err_to_name(rc_pin),
                PIN_I2S_DOUT, PIN_I2S_BCLK, PIN_I2S_LRC);
#endif
  i2s_zero_dma_buffer(I2S_PORT);
  curSampleRate = sampleRate;
}

static void i2sSetRate(int sampleRate) {
  if (sampleRate <= 0 || sampleRate == curSampleRate) return;
  i2s_set_clk(I2S_PORT, (uint32_t)sampleRate, I2S_BITS_PER_SAMPLE_16BIT,
              I2S_CHANNEL_STEREO);
  curSampleRate = sampleRate;
  Serial.printf("[i2s] 采样率 -> %d\n", sampleRate);
}

static void playPCM(const uint8_t *data, size_t len) {
  // Muse 发的是单声道 16-bit PCM；I2S 配成立体声，需把每个样本复制到 L/R。
  const int16_t *in = (const int16_t *)data;
  size_t samples = len / 2;
  int16_t stereo[512];  // 256 帧 * 2 声道
  size_t i = 0;
  while (i < samples) {
    int n = 0;
    while (i < samples && n < 256) {
      int16_t v = in[i++];
      stereo[n * 2] = v;
      stereo[n * 2 + 1] = v;
      n++;
    }
    size_t written = 0;
    // i2s_write 阻塞至 DMA 有空间，天然把播放节流到实时速率。
    i2s_write(I2S_PORT, stereo, n * 4, &written, portMAX_DELAY);
  }
}

// 开机自检音：直接合成正弦推 I2S，不依赖 WiFi/Muse。
// 听到 do-mi-so 三声 = ESP32→功放→喇叭 整条硬件通路 OK。
static void playTone(float freq, int ms, float vol) {
  const int sr = 24000;
  const int total = sr * ms / 1000;
  int16_t buf[480];  // 240 帧 * 2 声道（立体声，L=R）
  double phase = 0.0, dphase = 2.0 * M_PI * freq / sr;
  int done = 0;
  while (done < total) {
    int n = (total - done < 240) ? (total - done) : 240;
    for (int i = 0; i < n; i++) {
#if TEST_DAC
      // 内置DAC取每个16bit样本的高8位：无符号 0~255 → 高字节
      uint8_t d8 = (uint8_t)(128.0 + sin(phase) * 127.0 * vol);
      int16_t v = (int16_t)(((uint16_t)d8) << 8);
#else
      int16_t v = (int16_t)(sin(phase) * 32767.0 * vol);
#endif
      buf[i * 2] = v;
      buf[i * 2 + 1] = v;
      phase += dphase;
      if (phase > 2.0 * M_PI) phase -= 2.0 * M_PI;
    }
    size_t bw = 0;
    i2s_write(I2S_PORT, buf, n * 4, &bw, portMAX_DELAY);
    done += n;
  }
}

static void selfTest() {
  Serial.println("[selftest] 自检音（do-mi-so 循环3轮）…");
  for (int r = 0; r < 3; r++) {
    playTone(1000.0f, 700, 0.55f); delay(120);  // 持续音
    playTone(523.0f, 250, 0.55f); delay(70);    // do
    playTone(659.0f, 250, 0.55f); delay(70);    // mi
    playTone(784.0f, 400, 0.55f); delay(320);   // so
  }
  i2s_zero_dma_buffer(I2S_PORT);
  Serial.println("[selftest] 自检音结束");
}

// 引脚电学自检：查每根线是否对地/对电源短路，以及线间是否焊连短路。
// 注意：功放输入是高阻，"接没接功放"查不出；但连锡/短路(常见的没声元凶)能查。
static void diagPin(const char *nm, int pin) {
  pinMode(pin, INPUT_PULLUP);   delay(3); int up = digitalRead(pin);
  pinMode(pin, INPUT_PULLDOWN); delay(3); int dn = digitalRead(pin);
  const char *v;
  if (up == 1 && dn == 0)      v = "高阻(悬空或接功放输入，正常)";
  else if (up == 0 && dn == 0) v = "被强拉低！疑对地短路/连锡到 GND";
  else if (up == 1 && dn == 1) v = "被强拉高！疑对电源短路";
  else                          v = "异常";
  Serial.printf("[diag] %-4s(GPIO%2d): up=%d dn=%d -> %s\n", nm, pin, up, dn, v);
  pinMode(pin, INPUT);
}

static void selfDiagnose() {
  int pins[] = {PIN_I2S_DOUT, PIN_I2S_BCLK, PIN_I2S_LRC, PIN_AMP_SD, PIN_AMP_GAIN};
  const char *nm[] = {"DIN", "BCLK", "LRC", "SD", "GAIN"};
  Serial.println("[diag] === 引脚电学自检（查短路/连锡）===");
  for (int i = 0; i < 5; i++) diagPin(nm[i], pins[i]);
  bool anyShort = false;
  for (int i = 0; i < 5; i++) {
    for (int j = 0; j < 5; j++) if (j != i) pinMode(pins[j], INPUT_PULLDOWN);
    pinMode(pins[i], OUTPUT); digitalWrite(pins[i], HIGH); delay(3);
    for (int j = 0; j < 5; j++) {
      if (j == i) continue;
      if (digitalRead(pins[j]) == 1) {
        Serial.printf("[diag] 短路！%s(GPIO%d) 与 %s(GPIO%d) 连在一起\n",
                      nm[i], pins[i], nm[j], pins[j]);
        anyShort = true;
      }
    }
    digitalWrite(pins[i], LOW); pinMode(pins[i], INPUT);
  }
  Serial.println(anyShort ? "[diag] 发现线间短路，请检查焊点连锡！"
                          : "[diag] 线间无短路。");
  Serial.println("[diag] === 自检结束 ===");
}

// ---------------- WebSocket ----------------
static void sendHello() {
  JsonDocument doc;
  doc["type"] = "hello";
  doc["name"] = spkName.length() ? spkName : String("网络扬声器");
  doc["mac"] = WiFi.macAddress();
  String out;
  serializeJson(doc, out);
  webSocket.sendTXT(out);
}

static void onWsEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      wsConnected = true;
      Serial.printf("[ws] 已连接 %s:%u%s\n", museHost.c_str(), musePort, WS_PATH);
      sendHello();
      break;
    case WStype_DISCONNECTED:
      wsConnected = false;
      Serial.println("[ws] 断开，自动重连中…");
      i2s_zero_dma_buffer(I2S_PORT);
      break;
    case WStype_BIN:
      // 一段 TTS 的裸 PCM：直接出声。
      playPCM(payload, length);
      break;
    case WStype_TEXT: {
      // 控制帧：{"type":"start","sample_rate":24000} / {"type":"end"}
      JsonDocument doc;
      if (deserializeJson(doc, payload, length)) break;
      const char *t = doc["type"] | "";
      if (strcmp(t, "start") == 0) {
        int sr = doc["sample_rate"] | curSampleRate;
        i2sSetRate(sr);
      } else if (strcmp(t, "end") == 0) {
        // 收尾：无需特殊处理，DMA 放完即静音。
      }
      break;
    }
    default:
      break;
  }
}

// ---------------- 配置 ----------------
static void loadPrefs() {
  prefs.begin("spk", true);
  museHost = prefs.getString("host", "");
  musePort = prefs.getUShort("port", 8002);
  spkName = prefs.getString("name", "");
  prefs.end();
}

static void savePrefs() {
  prefs.begin("spk", false);
  prefs.putString("host", museHost);
  prefs.putUShort("port", musePort);
  prefs.putString("name", spkName);
  prefs.end();
}

static void saveConfigCallback() { shouldSaveConfig = true; }

static void runPortal(bool force) {
  WiFiManager wm;
  wm.setSaveConfigCallback(saveConfigCallback);

  char portBuf[8];
  snprintf(portBuf, sizeof(portBuf), "%u", musePort);
  WiFiManagerParameter pHost("host", "Muse 主机 IP", museHost.c_str(), 40);
  WiFiManagerParameter pPort("port", "Muse 端口", portBuf, 6);
  WiFiManagerParameter pName("name", "扬声器名", spkName.c_str(), 32);
  wm.addParameter(&pHost);
  wm.addParameter(&pPort);
  wm.addParameter(&pName);

  uint8_t mac[6];
  WiFi.macAddress(mac);
  char ap[24];
  snprintf(ap, sizeof(ap), "Muse-Speaker-%02X%02X", mac[4], mac[5]);

  bool ok;
  if (force) {
    wm.startConfigPortal(ap);
    ok = true;
  } else {
    wm.setConfigPortalTimeout(180);
    ok = wm.autoConnect(ap);
  }

  if (shouldSaveConfig) {
    museHost = pHost.getValue();
    musePort = (uint16_t)atoi(pPort.getValue());
    if (musePort == 0) musePort = 8002;
    spkName = pName.getValue();
    savePrefs();
  }
  if (!ok) {
    Serial.println("[wifi] 配置超时，重启");
    delay(1000);
    ESP.restart();
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(PIN_BOOT_BTN, INPUT_PULLUP);
  Serial.println("\n[boot] Muse 网络扬声器");

  loadPrefs();
  selfDiagnose();  // 先做引脚电学自检（须在 I2S 占用引脚之前）

  pinMode(PIN_AMP_SD, OUTPUT);
  digitalWrite(PIN_AMP_SD, HIGH);   // 使能功放（SD 接 GPIO33，高=开启）
  pinMode(PIN_AMP_GAIN, OUTPUT);
  digitalWrite(PIN_AMP_GAIN, LOW);  // GAIN 接 GPIO32，拉低=15dB
  Serial.printf("[amp] SD(GPIO%d)=HIGH 已使能功放；GAIN(GPIO%d)=LOW(15dB)\n",
                PIN_AMP_SD, PIN_AMP_GAIN);
  i2sInit(24000);
  selfTest();  // 再自检硬件出声通路

  // 上电时按住 BOOT → 强制进配置门户（改 WiFi/Muse 地址）
  bool forcePortal = (digitalRead(PIN_BOOT_BTN) == LOW);
  runPortal(forcePortal || museHost.length() == 0);
  Serial.printf("[wifi] 已连 %s  IP=%s\n",
                WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
  Serial.printf("[cfg] Muse=%s:%u  name=%s\n",
                museHost.c_str(), musePort, spkName.c_str());

  webSocket.begin(museHost, musePort, WS_PATH);
  webSocket.onEvent(onWsEvent);
  webSocket.setReconnectInterval(3000);
  webSocket.enableHeartbeat(15000, 3000, 2);  // 心跳保活，掉线快速重连
}

static uint32_t btnDownAt = 0;

void loop() {
  webSocket.loop();

  // 长按 BOOT 3 秒：清配置 + 重启（重新配网）
  if (digitalRead(PIN_BOOT_BTN) == LOW) {
    if (btnDownAt == 0) btnDownAt = millis();
    else if (millis() - btnDownAt > 3000) {
      Serial.println("[cfg] 清配置，重启…");
      prefs.begin("spk", false);
      prefs.clear();
      prefs.end();
      WiFiManager wm;
      wm.resetSettings();
      delay(300);
      ESP.restart();
    }
  } else {
    btnDownAt = 0;
  }
}
