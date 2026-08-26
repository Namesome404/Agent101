/**
 * Android APK bridge for Jarvis Assistant.
 * Loads when ?android=1 is present. Receives MediaPipe gestures from native layer
 * and maps them into assistant actions / MCP-visible state.
 */
import { getWebSocketHandler } from './core/network/websocket.js?v=0258';
import { log } from './utils/logger.js?v=0205';

const GESTURE_TEXT = {
    Open_Palm: '你好',
    Thumb_Up: '好的',
    Thumb_Down: '取消',
    Victory: '拍照看看',
    Pointing_Up: '提高音量',
    Closed_Fist: '停止',
    ILoveYou: '我喜欢你'
};

let latestGesture = null;
let lastSentGesture = '';
let lastSentAt = 0;

export function isAndroidMode() {
    return new URLSearchParams(window.location.search).get('android') === '1'
        || typeof window.JarvisAndroid !== 'undefined';
}

export function getLatestAndroidGesture() {
    return latestGesture;
}

function applyNativeConfig() {
    if (typeof window.JarvisAndroid === 'undefined') return;
    try {
        const cfg = JSON.parse(window.JarvisAndroid.getConfigJson());
        window.__JARVIS_ANDROID_CONFIG__ = cfg;
        const ota = document.getElementById('otaUrl');
        if (ota && cfg.otaUrl) ota.value = cfg.otaUrl;
        const mac = document.getElementById('deviceMac');
        if (mac && cfg.deviceMac) mac.value = cfg.deviceMac;
        const clientId = document.getElementById('clientId');
        if (clientId && cfg.clientId) clientId.value = cfg.clientId;
        const deviceName = document.getElementById('deviceName');
        if (deviceName && cfg.deviceName) deviceName.value = cfg.deviceName;
        log(`Android 配置已加载: ${cfg.serverHost}`, 'success');
    } catch (e) {
        log(`Android 配置加载失败: ${e.message}`, 'warning');
    }
}

function showGestureToast(name, score) {
    let el = document.getElementById('androidGestureToast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'androidGestureToast';
        el.style.cssText = [
            'position:fixed',
            'left:50%',
            'bottom:96px',
            'transform:translateX(-50%)',
            'background:rgba(108,99,255,0.9)',
            'color:#fff',
            'padding:8px 14px',
            'border-radius:16px',
            'font-size:13px',
            'z-index:9999',
            'pointer-events:none',
            'transition:opacity .3s'
        ].join(';');
        document.body.appendChild(el);
    }
    el.textContent = `手势: ${name} (${Math.round(score * 100)}%)`;
    el.style.opacity = '1';
    clearTimeout(showGestureToast._timer);
    showGestureToast._timer = setTimeout(() => {
        el.style.opacity = '0';
    }, 1500);
}

function handleGesture(event) {
    if (!event || !event.name) return;
    latestGesture = {
        name: event.name,
        score: event.score || 0,
        handedness: event.handedness || 'Unknown',
        at: Date.now()
    };
    showGestureToast(event.name, event.score || 0);
    log(`识别到手势: ${event.name} score=${event.score}`, 'info');

    const text = GESTURE_TEXT[event.name];
    if (!text) return;

    const now = Date.now();
    if (event.name === lastSentGesture && now - lastSentAt < 2500) return;
    lastSentGesture = event.name;
    lastSentAt = now;

    const ws = getWebSocketHandler();
    if (ws && ws.isConnected()) {
        ws.sendTextMessage(text);
        if (typeof window.JarvisAndroid !== 'undefined') {
            window.JarvisAndroid.updateStatus(`手势指令已发送: ${text}`);
        }
    }
}

export function installAndroidBridge() {
    if (!isAndroidMode()) return;

    document.documentElement.classList.add('android-mode');
    applyNativeConfig();

    window.__onJarvisGesture = (event) => {
        try {
            handleGesture(typeof event === 'string' ? JSON.parse(event) : event);
        } catch (e) {
            log(`手势回调失败: ${e.message}`, 'error');
        }
    };

    // Prefer Live2D avatar on phone if available
    try {
        if (!localStorage.getItem('xz_tester_avatar')) {
            localStorage.setItem('xz_tester_avatar', 'hiyori_pro_zh');
        }
    } catch (_) { /* ignore */ }

    log('Android 智能助手桥接已启用', 'success');
}
