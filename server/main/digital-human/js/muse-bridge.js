import { getWebSocketHandler } from './core/network/websocket.js?v=0271';
import { isSystemChatMessage, notifyLevel } from './utils/system-notify.js?v=0265';

export function isMuseMode() {
    return new URLSearchParams(window.location.search).get('muse') === '1';
}

export function isRemoteMode() {
    return isMuseMode() && new URLSearchParams(window.location.search).get('remote') === '1';
}

/**
 * 桌面 EV 终端只做形象预览，不占浏览器麦、不连核心语音 WS。
 * 语音始终走本机 devices.voice.terminal；仅 remote=1（手机远程麦）才走浏览器链路。
 * preview=1 保留兼容；缺省桌面 muse 也视为预览，避免缓存旧 URL 又把终端语音拉起来。
 */
export function isPreviewMode() {
    if (!isMuseMode()) return false;
    if (isRemoteMode()) return false;
    return true;
}

function postToParent(payload) {
    window.parent?.postMessage({ source: 'muse-digital-human', ...payload }, window.location.origin);
}

function withTimeout(promise, ms, label) {
    return Promise.race([
        promise,
        new Promise((_, reject) => {
            setTimeout(() => reject(new Error(`${label}超时（${Math.round(ms / 1000)}秒）`)), ms);
        })
    ]);
}

export function markRemoteSessionJoined() {
    const ui = window.__MUSE_REMOTE_UI__;
    if (!ui || ui.btn.classList.contains('live')) return;
    window.parent?.postMessage({
        source: 'muse-digital-human',
        type: 'status',
        mic: !!(window.microphoneAvailable || window.__MUSE_MIC_STREAM__),
        cam: !!window.cameraAvailable,
        connected: true
    }, window.location.origin);
    ui.setPill('mic', !!(window.microphoneAvailable || window.__MUSE_MIC_STREAM__));
    ui.setPill('cam', !!window.cameraAvailable);
    ui.setPill('link', true);
    ui.btn.disabled = false;
    ui.btn.classList.add('live');
    ui.btn.textContent = '已并入电脑会话 · 可直接说话';
    ui.log('已连接，对着手机说话即可。');
    window.__MUSE_HOST_POLL_STOP__?.();
}


function remoteUi() {
    return window.__MUSE_REMOTE_UI__;
}

function agentIdFromPage() {
    const params = new URLSearchParams(window.location.search);
    return params.get('agent_id') || (window.__MUSE_TERMINAL__?.agent_id) || '1';
}

async function fetchHostStatus(agentId) {
    const r = await fetch(`/api/agents/${agentId}/session/host`, { cache: 'no-store' });
    if (!r.ok) return { registered: false, ready: false };
    return r.json();
}

async function waitForHostReady(agentId, timeoutMs = 60000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        const st = await fetchHostStatus(agentId);
        if (st.ready) return st;
        await new Promise(resolve => setTimeout(resolve, 1500));
    }
    return null;
}

function startHostStatusPoll(ui, agentId) {
    let stopped = false;
    window.__MUSE_HOST_POLL_STOP__ = () => { stopped = true; };

    const tick = async () => {
        if (stopped) return;
        try {
            const st = await fetchHostStatus(agentId);
            const hint = ui.panel.querySelector('.muse-remote-hint');
            if (st.ready) {
                ui.setPill('link', true);
                if (hint) hint.textContent = '电脑端已就绪，点击下方按钮连接。';
                ui.log('电脑端会话已就绪');
                if (!ui.btn.classList.contains('live')) {
                    ui.btn.disabled = false;
                    ui.btn.textContent = '连接麦克风与摄像头';
                }
                return;
            }
            ui.setPill('link', false, false);
            ui.btn.disabled = true;
            ui.btn.textContent = '等待电脑端连接…';
            if (st.registered) {
                if (hint) hint.textContent = '电脑端已连接，正在初始化语音核心…';
                ui.log('电脑端正在初始化…');
            } else {
                if (hint) hint.textContent = '电脑端正在自动连接，请稍候。';
                ui.log('等待电脑端自动连接…');
            }
        } catch (e) {
            ui.log('无法查询电脑状态，请确认网络');
        }
        if (!stopped) setTimeout(tick, 2000);
    };
    tick();
}

export async function prepareRemoteMedia() {
        if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error('当前设备不支持麦克风/摄像头');
    }
    const ui = remoteUi();
    ui?.log('正在打开麦克风…');
    ui?.btn && (ui.btn.textContent = '正在打开麦克风…');

    let audioStream = null;
    try {
        const shared = window.top && window.top.__EV_SHARED_MIC__;
        if (shared && shared.getAudioTracks().some(t => t.readyState === 'live')) {
            audioStream = shared;
        }
    } catch (_) { /* ignore */ }
    if (!audioStream) {
        audioStream = await withTimeout(
            navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 }
            }),
            20000,
            '麦克风'
        );
    }
    window.__MUSE_MIC_STREAM__ = audioStream;
    try { window.top.__EV_SHARED_MIC__ = audioStream; } catch (_) { /* ignore */ }
    window.microphoneAvailable = audioStream.getAudioTracks().length > 0;
    ui?.setPill('mic', window.microphoneAvailable);

    ui?.log('麦克风已就绪，正在打开摄像头…');
    ui?.btn && (ui.btn.textContent = '正在打开摄像头…');

    let videoStream = null;
    const preview = document.getElementById('museRemotePreview');
    try {
        videoStream = await withTimeout(
            navigator.mediaDevices.getUserMedia({
                video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } }
            }),
            15000,
            '摄像头'
        );
        window.__MUSE_VIDEO_STREAM__ = videoStream;
        window.cameraAvailable = videoStream.getVideoTracks().length > 0;
        if (preview && videoStream) {
            preview.srcObject = videoStream;
            preview.classList.add('active');
        }
    } catch (e) {
        window.cameraAvailable = false;
        ui?.log(`摄像头未启用（${e.message}），仅使用麦克风`);
    }
    ui?.setPill('cam', !!window.cameraAvailable);

    window.__MUSE_MEDIA_STREAM__ = new MediaStream([
        ...audioStream.getAudioTracks(),
        ...(videoStream ? videoStream.getVideoTracks() : [])
    ]);

    ui?.log('正在并入电脑会话…');
    ui?.btn && (ui.btn.textContent = '正在并入电脑会话…');
    postToParent({
        type: 'status',
        mic: window.microphoneAvailable,
        cam: window.cameraAvailable,
        connected: false
    });
    return window.__MUSE_MEDIA_STREAM__;
}

export async function prepareMuseBridge() {
    if (!isMuseMode()) return null;
    const params = new URLSearchParams(window.location.search);
    const agentId = params.get('agent_id') || '1';
    const role = isRemoteMode() ? 'remote' : 'host';
    const terminal = await fetch(`/api/agents/${agentId}/terminal?role=${role}`, { cache: 'no-store' }).then(r => r.json());
    const device = terminal.device || {};
    localStorage.setItem('xz_tester_deviceMac', device.mac || '');
    localStorage.setItem('xz_tester_clientId', device.client_id || '');
    localStorage.setItem('xz_tester_deviceName', device.name || 'EV Terminal');
    localStorage.setItem('xz_tester_otaUrl', terminal.ota_url || `${location.origin}/xiaozhi/ota/`);
    localStorage.setItem('xz_tester_wakewordEnabled', 'false');
    localStorage.setItem('xz_tester_wakewordWsUrl', '');
    localStorage.setItem('xz_tester_emojiEnabled', 'true');
    const avatar = isRemoteMode() ? 'visualizer' : (terminal.avatar || 'visualizer');
    localStorage.setItem('xz_tester_avatar', avatar);
    if (terminal.avatar_model && !isRemoteMode()) {
        localStorage.setItem('xz_tester_avatarModel', terminal.avatar_model);
    } else {
        localStorage.removeItem('xz_tester_avatarModel');
    }
    window.__MUSE_TERMINAL__ = terminal;
    document.documentElement.classList.add('muse-mode');
    if (isRemoteMode()) {
        document.documentElement.classList.add('remote-mode');
    }
    if (!isRemoteMode()) {
        void prewarmEvStack(agentId);
    }
    return terminal;
}

export async function prewarmEvStack(agentId) {
    try {
        await fetch('/api/latency/prewarm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_id: Number(agentId) || 1 }),
        });
    } catch (_) {
        /* 预热失败不阻塞连接 */
    }
}

export function installMuseBridge(uiController) {
    if (!isMuseMode()) return;

    if (isRemoteMode()) {
        installRemotePanel(uiController);
    }

    const originalAdd = uiController.addChatMessage.bind(uiController);
    uiController.addChatMessage = (content, isUser = false) => {
        const text = String(content || '').trim();
        // 明确状态 → status；正常对话 → utterance（宿主助手正文以 DB poll 为准）
        if (isSystemChatMessage(text)) {
            postToParent({ type: 'status', text, level: notifyLevel(text) });
            if (!isRemoteMode()) return;
            originalAdd(content, isUser);
            const ui = window.__MUSE_REMOTE_UI__;
            if (ui && /连接失败|主机会话|尚未连接|麦克风启动失败/i.test(text)) {
                ui.log(text);
                ui.setPill('link', false, true);
                ui.btn.disabled = false;
                ui.btn.textContent = '连接麦克风与摄像头';
            }
            return;
        }
        originalAdd(content, isUser);
        if (!text) return;
        // 宿主/远程统一发 utterance；父页 dedupe。打字乐观气泡与 STT/TTS 预览可重叠。
        postToParent({ type: 'utterance', role: isUser ? 'user' : 'assistant', text });
    };
    document.documentElement.dataset.museBridge = 'ready';

    window.museStart = async () => {
        if (isPreviewMode()) {
            postToParent({ type: 'status', connected: false, preview: true });
            return false;
        }
        try {
            const agentId = agentIdFromPage();
            if (!isRemoteMode()) {
                void prewarmEvStack(agentId);
                // 父页仅用于预授权；录音流必须在本 iframe 内获取
                try {
                    const shared = window.top && window.top.__EV_SHARED_MIC__;
                    if (shared && shared.getAudioTracks().some(t => t.readyState === 'live')) {
                        window.__MUSE_MIC_PERMISSION_OK__ = true;
                        window.microphoneAvailable = true;
                    }
                } catch (_) { /* ignore */ }
            }
            const ui = remoteUi();
            if (isRemoteMode()) {
                ui?.log('检查电脑端会话…');
                ui?.btn && (ui.btn.textContent = '等待电脑端…');
                const host = await waitForHostReady(agentId, 90000);
                if (!host) {
                    throw new Error('电脑端会话未就绪，请确认 EV 核心服务在线');
                }
                await prepareRemoteMedia();
            }
            await uiController.handleConnect();
            // 连上后强制再启一次录音（Safari AudioContext 有时首连失败）
            if (!isRemoteMode()) {
                try {
                    const live = getWebSocketHandler();
                    if (live && live.isConnected()) {
                        await uiController.startAIChatSession();
                    }
                } catch (_) { /* ignore */ }
            }
            return true;
        } catch (error) {
            const ui = remoteUi();
            ui?.log(`连接失败: ${error.message}`);
            ui?.btn && (ui.btn.disabled = false);
            ui?.btn && (ui.btn.textContent = '连接麦克风与摄像头');
            ui?.setPill('link', false, true);
            uiController.addChatMessage(`连接失败: ${error.message}`, false);
            postToParent({ type: 'status', error: error.message });
            return false;
        }
    };

    window.museSendText = (text) => {
        const q = String(text || '').trim();
        if (!q) return false;
        if (isPreviewMode()) return false;
        const wsHandler = getWebSocketHandler();
        if (wsHandler && wsHandler.isConnected()) {
            wsHandler.sendTextMessage(q);
            return true;
        }
        // 尚未连上：静默排队并自动重连，不弹「正在自动连接」提示
        window.__MUSE_PENDING_TEXT__ = q;
        if (!isRemoteMode()) {
            if (!window.__MUSE_CONNECTING__) {
                window.__MUSE_CONNECTING__ = true;
                Promise.resolve(window.museStart()).finally(() => {
                    window.__MUSE_CONNECTING__ = false;
                    const pending = window.__MUSE_PENDING_TEXT__;
                    const live = getWebSocketHandler();
                    if (pending && live && live.isConnected()) {
                        window.__MUSE_PENDING_TEXT__ = '';
                        live.sendTextMessage(pending);
                    }
                });
            }
        } else {
            uiController.addChatMessage('尚未连接。请先点「连接麦克风与摄像头」。', false);
        }
        return true;
    };

    window.museUnlockAudio = async (force = false) => {
        if (isPreviewMode()) return false;
        try {
            const { getAudioPlayer } = await import('./core/audio/player.js?v=0264');
            const { getAudioRecorder } = await import('./core/audio/recorder.js?v=0264');
            await getAudioPlayer().resumeContext();
            const recorder = getAudioRecorder();
            for (const ctx of [recorder.recordAudioContext, recorder.audioContext]) {
                if (ctx && ctx.state === 'suspended') {
                    try { await ctx.resume(); } catch (_) { /* ignore */ }
                }
            }
            const live = getWebSocketHandler();
            if (!live?.isConnected?.()) return false;
            recorder.setWebSocket(live.websocket);
            const stats = recorder.getUplinkStats?.() || {};
            const trackDead = !recorder.mediaStream
                || !recorder.mediaStream.getAudioTracks?.().some(t => t.readyState === 'live' && t.enabled !== false);
            const needRestart = force
                || trackDead
                || !recorder.isRecording
                || stats.ctx === 'suspended'
                || !stats.wsOpen;
            if (needRestart && !recorder._starting) {
                if (recorder.isRecording) recorder.stop();
                // 丢掉已 ended / 可能被设备页探测弄死的流，强制本 iframe 重新开麦
                window.__MUSE_MIC_STREAM__ = null;
                recorder.mediaStream = null;
                try { await recorder.start(); } catch (_) { /* ignore */ }
            }
            return !!recorder.isRecording;
        } catch (_) {
            return false;
        }
    };

    // 麦上行巡检：连着但没在录音 / 上下文挂起 / 帧数停滞 → 自动拉起
    if (!window.__MUSE_MIC_WATCH__ && !isPreviewMode()) {
        window.__MUSE_MIC_WATCH_SENT__ = 0;
        window.__MUSE_MIC_WATCH__ = setInterval(async () => {
            try {
                // 父页 park 时 document.hidden 可能为 true；仍要巡检，否则从设置返回会一直哑麦
                if (window.__MUSE_MIC_WATCH_BUSY__) return;
                window.__MUSE_MIC_WATCH_BUSY__ = true;
                const live = getWebSocketHandler();
                if (!live?.isConnected?.()) return;
                const { getAudioRecorder } = await import('./core/audio/recorder.js?v=0264');
                const recorder = getAudioRecorder();
                const stats = recorder.getUplinkStats?.() || {};
                const sent = stats.framesSent || 0;
                const stalled = sent <= (window.__MUSE_MIC_WATCH_SENT__ || 0);
                window.__MUSE_MIC_WATCH_SENT__ = sent;
                const trackDead = !recorder.mediaStream
                    || !recorder.mediaStream.getAudioTracks?.().some(t => t.readyState === 'live');
                if (!stats.recording || stats.ctx === 'suspended' || stalled || trackDead) {
                    await window.museUnlockAudio?.(true);
                    window.__MUSE_MIC_WATCH_SENT__ = recorder.getUplinkStats?.().framesSent || 0;
                }
            } catch (_) { /* ignore */ }
            finally { window.__MUSE_MIC_WATCH_BUSY__ = false; }
        }, 3000);
    }

    window.addEventListener('message', async (event) => {
        if (event.origin !== window.location.origin) return;
        const msg = event.data || {};
        if (msg.source !== 'muse-parent') return;
        // 预览模式只接收声波旁路，不占麦/不连 WS
        if (isPreviewMode()) {
            if (msg.type === 'voice-stage') {
                const viz = window.chatApp?.soundVisualizer;
                if (!viz) return;
                if (msg.speaking != null) viz.setSpeaking(!!msg.speaking);
                if (msg.level != null) viz.setExternalLevel(msg.level);
            }
            return;
        }
        if (msg.type === 'start') await window.museStart();
        if (msg.type === 'sendText') window.museSendText(msg.text);
        if (msg.type === 'unlockAudio') await window.museUnlockAudio(!!msg.force);
        if (msg.type === 'audioDevicesChanged') {
            window.dispatchEvent(new CustomEvent('ev-audio-devices-changed'));
            void window.museUnlockAudio?.(true);
        }
    });

    // iframe 内自身手势也解锁（点侧栏/舞台时）
    ['pointerdown', 'keydown', 'touchstart'].forEach(ev => {
        window.addEventListener(ev, () => { void window.museUnlockAudio(); }, { capture: true, passive: true });
    });

    postToParent({ type: 'ready', terminal: window.__MUSE_TERMINAL__ || null });
}

function installRemotePanel(uiController) {
    if (document.getElementById('museRemotePanel')) return;
    const t = window.__MUSE_TERMINAL__ || {};
    const panel = document.createElement('div');
    panel.id = 'museRemotePanel';
    panel.className = 'muse-remote-panel';
    panel.innerHTML = `
      <div class="muse-remote-inner">
        <p class="muse-remote-title">${t.agent_name || 'EV'} · 远程输入</p>
        <div class="muse-remote-status">
          <span data-k="mic"><i></i>麦克风</span>
          <span data-k="cam"><i></i>摄像头</span>
          <span data-k="link"><i></i>会话</span>
        </div>
        <button type="button" id="museRemoteStart">连接麦克风与摄像头</button>
        <video id="museRemotePreview" class="muse-remote-preview" autoplay playsinline muted></video>
        <p class="muse-remote-hint">并入电脑端同一会话；电脑端会自动连接。</p>
        <p class="muse-remote-log" id="museRemoteLog"></p>
      </div>`;
    document.body.appendChild(panel);

    const style = document.createElement('style');
    style.textContent = `
      html.remote-mode, html.remote-mode body { background:#0a0a0a!important; overflow:hidden; }
      html.remote-mode .container, html.remote-mode .background-container { opacity:0!important; pointer-events:none!important; }
      .muse-remote-panel { position:fixed; inset:0; z-index:99999; display:flex; align-items:center; justify-content:center;
        background:#0a0a0a; color:rgba(232,228,220,.92); font-family:-apple-system,BlinkMacSystemFont,sans-serif; }
      .muse-remote-inner { width:min(360px,92vw); text-align:center; display:flex; flex-direction:column; gap:16px; }
      .muse-remote-title { margin:0; font-size:14px; letter-spacing:.08em; color:rgba(168,164,156,.7); text-transform:uppercase; }
      .muse-remote-status { display:flex; justify-content:center; gap:10px; flex-wrap:wrap; }
      .muse-remote-status span { display:inline-flex; align-items:center; gap:6px; padding:8px 10px; font-size:13px;
        background:rgba(255,255,255,.04); color:rgba(168,164,156,.85); }
      .muse-remote-status span.on { color:rgba(220,240,220,.95); }
      .muse-remote-status span.on i { background:#6ecf8a; box-shadow:0 0 8px rgba(110,207,138,.45); }
      .muse-remote-status span.err i { background:#e06c6c; }
      .muse-remote-status i { width:8px; height:8px; border-radius:50%; background:rgba(255,255,255,.18); display:inline-block; }
      #museRemoteStart { border:0; padding:16px 24px; font-size:16px; font-weight:600; background:rgba(232,228,220,.92); color:#141414; }
      #museRemoteStart:disabled { opacity:.55; }
      #museRemoteStart.live { background:rgba(110,207,138,.22); color:rgba(220,240,220,.95); }
      .muse-remote-preview { width:120px; height:160px; object-fit:cover; margin:0 auto; display:none;
        background:#111; border:1px solid rgba(255,255,255,.08); }
      .muse-remote-preview.active { display:block; }
      .muse-remote-hint, .muse-remote-log { margin:0; font-size:13px; line-height:1.55; color:rgba(168,164,156,.85); }
      .muse-remote-log { min-height:1.4em; color:rgba(232,228,220,.88); }
    `;
    document.head.appendChild(style);

    const log = (text) => {
        const n = document.getElementById('museRemoteLog');
        if (n) n.textContent = String(text || '');
    };
    const setPill = (key, on, err) => {
        const el = panel.querySelector(`[data-k="${key}"]`);
        if (!el) return;
        el.classList.toggle('on', !!on && !err);
        el.classList.toggle('err', !!err);
    };

    const btn = panel.querySelector('#museRemoteStart');
    btn.disabled = true;
    btn.textContent = '等待电脑端连接…';
    btn.onclick = async () => {
        if (btn.classList.contains('live')) return;
        btn.disabled = true;
        btn.textContent = '准备中…';
        log('检查电脑端会话…');
        try {
            const agentId = agentIdFromPage();
            const st = await fetchHostStatus(agentId);
            if (!st.ready) {
                log(st.registered ? '电脑端正在初始化，请稍候…' : '等待电脑端自动连接…');
                btn.disabled = false;
                btn.textContent = '连接麦克风与摄像头';
                return;
            }
            await window.museStart();
        } catch (e) {
            log(e.message || '连接失败');
            btn.disabled = false;
            btn.textContent = '连接麦克风与摄像头';
            setPill('link', false, true);
        }
    };

    window.__MUSE_REMOTE_UI__ = { setPill, log, btn, panel };
    startHostStatusPoll(window.__MUSE_REMOTE_UI__, agentIdFromPage());
}
