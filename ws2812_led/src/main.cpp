#include <Arduino.h>
#include <ArduinoJson.h>
#include <FastLED.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <ESPmDNS.h>

constexpr uint8_t LED_PIN = 27;
constexpr uint16_t MAX_LEDS = 300;
constexpr char HOST_NAME[] = "ws2812";

CRGB leds[MAX_LEDS];
WebServer server(80);
Preferences preferences;

struct LightState {
  bool power = true;
  uint8_t red = 255;
  uint8_t green = 160;
  uint8_t blue = 60;
  uint8_t brightness = 30;  // 0..100
  uint16_t count = 60;
  String effect = "solid";
  uint8_t speed = 50;       // 1..100
} state;

uint32_t lastFrameAt = 0;
uint16_t animationStep = 0;

const char INDEX_HTML[] PROGMEM = R"HTML(
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#10131a"><title>WS2812 灯光控制</title>
<style>
:root{color-scheme:dark;font-family:ui-rounded,-apple-system,BlinkMacSystemFont,"SF Pro Display","PingFang SC",sans-serif}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 50% 0,#273148 0,#10131a 45%);color:#f5f7fb;padding:22px}
.app{max-width:520px;margin:auto}.head{display:flex;align-items:center;justify-content:space-between;margin:8px 0 22px}.title{font-size:24px;font-weight:750}.status{font-size:13px;color:#9da8bb}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#48d597;margin-right:7px;box-shadow:0 0 10px #48d597}
.card{background:rgba(27,32,44,.88);border:1px solid rgba(255,255,255,.09);border-radius:22px;padding:20px;margin-bottom:15px;box-shadow:0 16px 40px rgba(0,0,0,.25)}
.row{display:flex;align-items:center;justify-content:space-between;gap:14px}.label{font-size:14px;color:#aeb7c7;margin-bottom:12px}.power{appearance:none;width:60px;height:34px;border-radius:18px;background:#3b4250;position:relative;transition:.2s}.power:after{content:"";position:absolute;width:28px;height:28px;left:3px;top:3px;border-radius:50%;background:#fff;transition:.2s}.power:checked{background:#6b7cff}.power:checked:after{transform:translateX(26px)}
.color-wrap{display:grid;grid-template-columns:88px 1fr;gap:17px;align-items:center}input[type=color]{width:88px;height:88px;border:0;background:none;padding:0;border-radius:18px;overflow:hidden}input[type=color]::-webkit-color-swatch-wrapper{padding:0}input[type=color]::-webkit-color-swatch{border:0;border-radius:18px}
.value{font-size:30px;font-weight:700}.sub{font-size:13px;color:#929daf;margin-top:4px}input[type=range]{width:100%;accent-color:#8290ff}.quick{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-top:18px}.swatch{height:42px;border:0;border-radius:13px;background:var(--c);box-shadow:inset 0 0 0 1px rgba(255,255,255,.18)}
.effects{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.effect{border:1px solid #3b4353;background:#252b38;color:#dfe4ed;border-radius:13px;padding:12px;font-size:14px}.effect.active{background:#6575ff;border-color:#8390ff;color:white}
.settings{display:grid;grid-template-columns:1fr auto;gap:10px}input[type=number]{min-width:0;border:1px solid #3b4353;background:#202631;color:#fff;border-radius:12px;padding:12px;font-size:16px}.save{border:0;background:#6575ff;color:#fff;border-radius:12px;padding:0 18px;font-weight:650}.foot{text-align:center;color:#687386;font-size:12px;padding:8px}.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#f5f7fb;color:#161b24;padding:10px 16px;border-radius:20px;font-size:13px;opacity:0;transition:.2s;pointer-events:none}.toast.show{opacity:1}
</style></head><body><main class="app">
<div class="head"><div><div class="title">灯光控制</div><div class="status"><span class="dot"></span><span id="network">正在连接…</span></div></div><input id="power" class="power" type="checkbox" aria-label="开关"></div>
<section class="card"><div class="label">颜色</div><div class="color-wrap"><input id="color" type="color" value="#ffa03c"><div><div id="hex" class="value">#FFA03C</div><div class="sub">点击色块选择颜色</div></div></div><div class="quick">
<button class="swatch" style="--c:#ff3b30" data-color="#ff3b30" aria-label="红"></button><button class="swatch" style="--c:#ff9500" data-color="#ff9500" aria-label="橙"></button><button class="swatch" style="--c:#34c759" data-color="#34c759" aria-label="绿"></button><button class="swatch" style="--c:#0a84ff" data-color="#0a84ff" aria-label="蓝"></button><button class="swatch" style="--c:#af52de" data-color="#af52de" aria-label="紫"></button></div></section>
<section class="card"><div class="row"><div class="label">亮度</div><div id="brightnessValue">30%</div></div><input id="brightness" type="range" min="0" max="100" value="30"></section>
<section class="card"><div class="label">灯效</div><div class="effects"><button class="effect active" data-effect="solid">纯色</button><button class="effect" data-effect="rainbow">彩虹</button><button class="effect" data-effect="breathing">呼吸</button><button class="effect" data-effect="wipe">流水</button></div><div class="row" style="margin-top:18px"><div class="label" style="margin:0">速度</div><div id="speedValue">50%</div></div><input id="speed" type="range" min="1" max="100" value="50"></section>
<section class="card"><div class="label">灯珠数量（1–300）</div><div class="settings"><input id="count" type="number" min="1" max="300" value="60"><button id="saveCount" class="save">保存</button></div></section>
<div class="foot">ESP32 · GPIO27 · ws2812.local</div></main><div id="toast" class="toast">已保存</div>
<script>
const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s); let timer;
function toast(t){const e=$('#toast');e.textContent=t;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),1200)}
function rgbToHex(r,g,b){return '#'+[r,g,b].map(v=>Number(v).toString(16).padStart(2,'0')).join('').toUpperCase()}
function hexToRgb(h){return {red:parseInt(h.slice(1,3),16),green:parseInt(h.slice(3,5),16),blue:parseInt(h.slice(5,7),16)}}
async function send(p){try{const r=await fetch('/api/led/state',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});if(!r.ok)throw 0;return await r.json()}catch(e){toast('设备连接失败')}}
function later(p,ms=80){clearTimeout(timer);timer=setTimeout(()=>send(p),ms)}
function setEffect(name){$$('.effect').forEach(x=>x.classList.toggle('active',x.dataset.effect===name))}
async function load(){try{const s=await(await fetch('/api/led/status')).json();$('#network').textContent=s.ip;$('#power').checked=s.power;const h=rgbToHex(s.red,s.green,s.blue);$('#color').value=h;$('#hex').textContent=h;$('#brightness').value=s.brightness;$('#brightnessValue').textContent=s.brightness+'%';$('#speed').value=s.speed;$('#speedValue').textContent=s.speed+'%';$('#count').value=s.count;setEffect(s.effect)}catch(e){$('#network').textContent='设备离线'}}
$('#power').onchange=e=>send({power:e.target.checked});
$('#color').oninput=e=>{const h=e.target.value;$('#hex').textContent=h.toUpperCase();later({...hexToRgb(h),power:true})};
$$('.swatch').forEach(b=>b.onclick=()=>{const h=b.dataset.color;$('#color').value=h;$('#hex').textContent=h.toUpperCase();$('#power').checked=true;send({...hexToRgb(h),power:true})});
$('#brightness').oninput=e=>{$('#brightnessValue').textContent=e.target.value+'%';later({brightness:+e.target.value})};
$$('.effect').forEach(b=>b.onclick=()=>{setEffect(b.dataset.effect);send({effect:b.dataset.effect})});
$('#speed').oninput=e=>{$('#speedValue').textContent=e.target.value+'%';later({speed:+e.target.value})};
$('#saveCount').onclick=async()=>{const n=Math.max(1,Math.min(300,+$('#count').value||1));$('#count').value=n;await send({count:n});toast('灯珠数量已保存')};
load();
</script></body></html>
)HTML";

uint8_t percentToByte(uint8_t percent) {
  return map(constrain(percent, 0, 100), 0, 100, 0, 255);
}

void clearUnused() {
  for (uint16_t i = state.count; i < MAX_LEDS; ++i) leds[i] = CRGB::Black;
}

void renderSolid() {
  fill_solid(leds, state.count, CRGB(state.red, state.green, state.blue));
}

void renderFrame() {
  if (!state.power || state.brightness == 0) {
    FastLED.clear();
    FastLED.show();
    return;
  }

  const uint8_t baseBrightness = percentToByte(state.brightness);
  if (state.effect == "rainbow") {
    fill_rainbow(leds, state.count, animationStep, 255 / max<uint16_t>(state.count, 1));
    FastLED.setBrightness(baseBrightness);
  } else if (state.effect == "breathing") {
    renderSolid();
    const uint8_t breath = beatsin8(map(state.speed, 1, 100, 4, 24), 15, 255);
    FastLED.setBrightness(scale8(baseBrightness, breath));
  } else if (state.effect == "wipe") {
    fill_solid(leds, state.count, CRGB::Black);
    const uint16_t head = animationStep % max<uint16_t>(state.count, 1);
    const uint16_t tail = max<uint16_t>(2, state.count / 6);
    for (uint16_t i = 0; i < tail; ++i) {
      const uint16_t index = (head + state.count - i) % state.count;
      leds[index] = CRGB(state.red, state.green, state.blue);
      leds[index].nscale8(255 - (i * 220 / tail));
    }
    FastLED.setBrightness(baseBrightness);
  } else {
    renderSolid();
    FastLED.setBrightness(baseBrightness);
  }
  clearUnused();
  FastLED.show();
}

void saveState() {
  preferences.putBool("power", state.power);
  preferences.putUChar("red", state.red);
  preferences.putUChar("green", state.green);
  preferences.putUChar("blue", state.blue);
  preferences.putUChar("brightness", state.brightness);
  preferences.putUShort("count", state.count);
  preferences.putString("effect", state.effect);
  preferences.putUChar("speed", state.speed);
}

void loadState() {
  preferences.begin("ws2812", false);
  state.power = preferences.getBool("power", true);
  state.red = preferences.getUChar("red", 255);
  state.green = preferences.getUChar("green", 160);
  state.blue = preferences.getUChar("blue", 60);
  state.brightness = min<uint8_t>(preferences.getUChar("brightness", 30), 100);
  state.count = constrain(preferences.getUShort("count", 60), 1, MAX_LEDS);
  state.effect = preferences.getString("effect", "solid");
  state.speed = constrain(preferences.getUChar("speed", 50), 1, 100);
}

void addStateToJson(JsonDocument &doc) {
  doc["power"] = state.power;
  doc["red"] = state.red;
  doc["green"] = state.green;
  doc["blue"] = state.blue;
  doc["brightness"] = state.brightness;
  doc["effect"] = state.effect;
  doc["speed"] = state.speed;
  doc["count"] = state.count;
  doc["pin"] = LED_PIN;
  doc["ip"] = WiFi.localIP().toString();
  doc["hostname"] = String(HOST_NAME) + ".local";
  doc["rssi"] = WiFi.RSSI();
}

void sendJson(const JsonDocument &doc, int status = 200) {
  String body;
  serializeJson(doc, body);
  server.send(status, "application/json; charset=utf-8", body);
}

bool validEffect(const String &effect) {
  return effect == "solid" || effect == "rainbow" || effect == "breathing" || effect == "wipe";
}

void handleStatus() {
  JsonDocument doc;
  addStateToJson(doc);
  sendJson(doc);
}

void handleState() {
  JsonDocument input;
  const DeserializationError error = deserializeJson(input, server.arg("plain"));
  if (error) {
    JsonDocument response;
    response["error"] = "invalid_json";
    sendJson(response, 400);
    return;
  }

  if (!input["power"].isNull()) state.power = input["power"].as<bool>();
  if (!input["red"].isNull()) state.red = constrain(input["red"].as<int>(), 0, 255);
  if (!input["green"].isNull()) state.green = constrain(input["green"].as<int>(), 0, 255);
  if (!input["blue"].isNull()) state.blue = constrain(input["blue"].as<int>(), 0, 255);
  if (!input["brightness"].isNull()) state.brightness = constrain(input["brightness"].as<int>(), 0, 100);
  if (!input["speed"].isNull()) state.speed = constrain(input["speed"].as<int>(), 1, 100);
  if (!input["count"].isNull()) state.count = constrain(input["count"].as<int>(), 1, MAX_LEDS);
  if (!input["effect"].isNull()) {
    const String requestedEffect = input["effect"].as<String>();
    if (validEffect(requestedEffect)) state.effect = requestedEffect;
  }

  animationStep = 0;
  saveState();
  renderFrame();
  JsonDocument response;
  addStateToJson(response);
  sendJson(response);
}

void setupWebServer() {
  server.on("/", HTTP_GET, [] { server.send_P(200, "text/html; charset=utf-8", INDEX_HTML); });
  server.on("/api/led/status", HTTP_GET, handleStatus);
  server.on("/api/led/state", HTTP_POST, handleState);
  server.on("/health", HTTP_GET, [] { server.send(200, "text/plain", "ok"); });
  server.onNotFound([] { server.send(404, "application/json", "{\"error\":\"not_found\"}"); });
  server.begin();
}

void setup() {
  Serial.begin(115200);
  loadState();
  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, MAX_LEDS).setCorrection(TypicalLEDStrip);
  FastLED.setMaxPowerInVoltsAndMilliamps(5, 3000);
  renderFrame();

  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOST_NAME);
  WiFiManager wifiManager;
  wifiManager.setConfigPortalTimeout(180);
  if (!wifiManager.autoConnect("WS2812-Setup", "ws2812setup")) ESP.restart();

  MDNS.begin(HOST_NAME);
  MDNS.addService("http", "tcp", 80);
  setupWebServer();

  Serial.println();
  Serial.println("WS2812 controller ready");
  Serial.print("IP: http://");
  Serial.println(WiFi.localIP());
  Serial.println("mDNS: http://ws2812.local");
}

void loop() {
  server.handleClient();
  const uint16_t frameInterval = map(state.speed, 1, 100, 180, 18);
  if (millis() - lastFrameAt >= frameInterval) {
    lastFrameAt = millis();
    ++animationStep;
    if (state.effect != "solid") renderFrame();
  }
  delay(2);
}
