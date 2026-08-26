const MIC_KEY = 'ev_audio_input_id';
const SPK_KEY = 'ev_audio_output_id';
const DIS_MIC_KEY = 'ev_audio_disabled_inputs';
const DIS_SPK_KEY = 'ev_audio_disabled_outputs';

function _parseList(key) {
    try { return JSON.parse(localStorage.getItem(key) || '[]') || []; } catch (_) { return []; }
}

export function getDisabledMicIds() {
    return _parseList(DIS_MIC_KEY);
}

export function getDisabledSpeakerIds() {
    return _parseList(DIS_SPK_KEY);
}

export function getSelectedMicId() {
    const id = localStorage.getItem(MIC_KEY) || '';
    if (id && getDisabledMicIds().includes(id)) return '';
    return id;
}

export function getSelectedSpeakerId() {
    const id = localStorage.getItem(SPK_KEY) || '';
    if (id && getDisabledSpeakerIds().includes(id)) return '';
    return id;
}

export function setSelectedMicId(id) {
    if (id) localStorage.setItem(MIC_KEY, id);
    else localStorage.removeItem(MIC_KEY);
}

export function setSelectedSpeakerId(id) {
    if (id) localStorage.setItem(SPK_KEY, id);
    else localStorage.removeItem(SPK_KEY);
}

export function buildMicConstraints(extra = {}) {
    const selectedRaw = localStorage.getItem(MIC_KEY) || '';
    if (selectedRaw && getDisabledMicIds().includes(selectedRaw)) {
        throw new Error('本机麦克风已在设备页关闭');
    }
    const audio = {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
        ...extra,
    };
    const deviceId = getSelectedMicId();
    if (deviceId) audio.deviceId = { exact: deviceId };
    return { audio };
}

export async function ensureMediaPermission() {
    if (!navigator.mediaDevices?.getUserMedia) return false;
    // 优先复用父页/已有麦流，绝不「开麦再立刻 stop」——那会让系统每次都重问权限
    const usable = (s) => s && s.getAudioTracks?.().some(t => t.readyState === 'live');
    if (usable(window.__MUSE_MIC_STREAM__)) return true;
    try {
        if (window.top && usable(window.top.__EV_SHARED_MIC__)) {
            window.__MUSE_MIC_STREAM__ = window.top.__EV_SHARED_MIC__;
            return true;
        }
    } catch (_) { /* ignore */ }
    try {
        if (navigator.permissions?.query) {
            const st = await navigator.permissions.query({ name: 'microphone' });
            if (st.state === 'granted') return true;
            if (st.state === 'denied') return false;
        }
    } catch (_) { /* ignore */ }
    try {
        const stream = await navigator.mediaDevices.getUserMedia(buildMicConstraints());
        window.__MUSE_MIC_STREAM__ = stream;
        try { window.top.__EV_SHARED_MIC__ = stream; } catch (_) { /* ignore */ }
        return true;
    } catch (_) {
        return false;
    }
}

export async function listAudioDevices() {
    if (!navigator.mediaDevices?.enumerateDevices) {
        return { inputs: [], outputs: [] };
    }
    await ensureMediaPermission();
    const all = await navigator.mediaDevices.enumerateDevices();
    const inputs = all.filter(d => d.kind === 'audioinput' && d.deviceId);
    const outputs = all.filter(d => d.kind === 'audiooutput' && d.deviceId);
    return { inputs, outputs };
}

export function deviceLabel(device, fallback) {
    const label = String(device?.label || '').trim();
    if (label) return label;
    return fallback || '未命名设备';
}

export async function applySpeakerSink(audioContext) {
    const disabled = new Set(getDisabledSpeakerIds());
    const selectedRaw = localStorage.getItem(SPK_KEY) || '';
    if (selectedRaw && disabled.has(selectedRaw)) {
        throw new Error('本机扬声器已在设备页关闭');
    }
    // 未选用时：若 default 被禁用，或所有输出都被禁用，则拒播
    if (!selectedRaw && (disabled.has('default') || disabled.size >= 1)) {
        // 仅当明确禁用了 default，或禁用列表非空且没有可用选用时拒播
        // 更稳妥：未选用 + default 禁用 → 拒播；未选用且禁用了任意设备仍允许系统默认，除非 default 也禁用
        if (disabled.has('default')) {
            throw new Error('本机扬声器已在设备页关闭');
        }
    }
    const id = getSelectedSpeakerId();
    if (!id || !audioContext?.setSinkId) return;
    try {
        await audioContext.setSinkId(id);
    } catch (err) {
        if (String(err?.message || '').includes('设备页关闭')) throw err;
        setSelectedSpeakerId('');
        try {
            if (typeof audioContext.setSinkId === 'function') {
                await audioContext.setSinkId('');
            }
        } catch (_) {
            /* ignore */
        }
    }
}
