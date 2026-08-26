// Audio recording module
import { buildMicConstraints } from './devices.js?v=0260';
import { log } from '../../utils/logger.js?v=0205';
import { initOpusEncoder } from './opus-codec.js?v=0258';
import { getAudioPlayer } from './player.js?v=0264';

// Audio recorder class
export class AudioRecorder {
    constructor() {
        this.isRecording = false;
        this.audioContext = null;
        this.analyser = null;
        this.audioProcessor = null;
        this.audioProcessorType = null;
        this.audioSource = null;
        this.opusEncoder = null;
        this.pcmDataBuffer = new Int16Array();
        this.audioBuffers = [];
        this.totalAudioSize = 0;
        this.visualizationRequest = null;
        this.recordingTimer = null;
        this.websocket = null;
        // Callback functions
        this.onRecordingStart = null;
        this.onRecordingStop = null;
        this.onVisualizerUpdate = null;
    }

    // Set WebSocket instance
    setWebSocket(ws) {
        this.websocket = ws;
    }

    // 播放用 AudioContext（可能 16k）；录音必须用独立上下文，避免和 TTS 混音/抢采样率
    getAudioContext() {
        return getAudioPlayer().getAudioContext();
    }

    getRecordAudioContext() {
        if (!this.recordAudioContext || this.recordAudioContext.state === 'closed') {
            const AC = window.AudioContext || window.webkitAudioContext;
            // 不强制 16000：iOS Safari 常忽略，用设备原生速率再重采样更稳
            this.recordAudioContext = new AC({ latencyHint: 'interactive' });
            log(`录音 AudioContext 采样率: ${this.recordAudioContext.sampleRate}Hz`, 'info');
        }
        return this.recordAudioContext;
    }

    // Initialize encoder
    initEncoder() {
        if (!this.opusEncoder) {
            this.opusEncoder = initOpusEncoder();
        }
        return this.opusEncoder;
    }

    // PCM processor code
    getAudioProcessorCode() {
        return `
            class AudioRecorderProcessor extends AudioWorkletProcessor {
                constructor() {
                    super();
                    this.buffers = [];
                    this.frameSize = 960;
                    this.buffer = new Int16Array(this.frameSize);
                    this.bufferIndex = 0;
                    this.isRecording = false;
                    this.port.onmessage = (event) => {
                        if (event.data.command === 'start') {
                            this.isRecording = true;
                            this.port.postMessage({ type: 'status', status: 'started' });
                        } else if (event.data.command === 'stop') {
                            this.isRecording = false;
                            if (this.bufferIndex > 0) {
                                const finalBuffer = this.buffer.slice(0, this.bufferIndex);
                                this.port.postMessage({ type: 'buffer', buffer: finalBuffer });
                                this.bufferIndex = 0;
                            }
                            this.port.postMessage({ type: 'status', status: 'stopped' });
                        }
                    };
                }
                process(inputs, outputs, parameters) {
                    if (!this.isRecording) return true;
                    const input = inputs[0][0];
                    if (!input) return true;
                    for (let i = 0; i < input.length; i++) {
                        if (this.bufferIndex >= this.frameSize) {
                            this.port.postMessage({ type: 'buffer', buffer: this.buffer.slice(0) });
                            this.bufferIndex = 0;
                        }
                        this.buffer[this.bufferIndex++] = Math.max(-32768, Math.min(32767, Math.floor(input[i] * 32767)));
                    }
                    return true;
                }
            }
            registerProcessor('audio-recorder-processor', AudioRecorderProcessor);
        `;
    }

    // Create audio processor
    async createAudioProcessor() {
        this.audioContext = this.getRecordAudioContext();
        try {
            if (this.audioContext.audioWorklet) {
                const blob = new Blob([this.getAudioProcessorCode()], { type: 'application/javascript' });
                const url = URL.createObjectURL(blob);
                await this.audioContext.audioWorklet.addModule(url);
                URL.revokeObjectURL(url);
                const audioProcessor = new AudioWorkletNode(this.audioContext, 'audio-recorder-processor');
                audioProcessor.port.onmessage = (event) => {
                    if (event.data.type === 'buffer') {
                        this.processPCMBuffer(event.data.buffer);
                    }
                };
                log('使用AudioWorklet处理音频', 'success');
                const silent = this.audioContext.createGain();
                silent.gain.value = 0;
                audioProcessor.connect(silent);
                silent.connect(this.audioContext.destination);
                return { node: audioProcessor, type: 'worklet' };
            } else {
                log('AudioWorklet不可用，使用ScriptProcessorNode作为后备方案', 'warning');
                return this.createScriptProcessor();
            }
        } catch (error) {
            log(`创建音频处理器失败: ${error.message}，尝试后备方案`, 'error');
            return this.createScriptProcessor();
        }
    }

    // Create ScriptProcessor as fallback
    createScriptProcessor() {
        try {
            const frameSize = 4096;
            const scriptProcessor = this.audioContext.createScriptProcessor(frameSize, 1, 1);
            scriptProcessor.onaudioprocess = (event) => {
                if (!this.isRecording) return;
                const input = event.inputBuffer.getChannelData(0);
                const buffer = new Int16Array(input.length);
                for (let i = 0; i < input.length; i++) {
                    buffer[i] = Math.max(-32768, Math.min(32767, Math.floor(input[i] * 32767)));
                }
                this.processPCMBuffer(buffer);
            };
            const silent = this.audioContext.createGain();
            silent.gain.value = 0;
            scriptProcessor.connect(silent);
            silent.connect(this.audioContext.destination);
            log('使用ScriptProcessorNode作为后备方案成功', 'warning');
            return { node: scriptProcessor, type: 'processor' };
        } catch (fallbackError) {
            log(`后备方案也失败: ${fallbackError.message}`, 'error');
            return null;
        }
    }

    // Resample PCM to 16kHz (iPhone Safari 常为 44.1/48kHz，不重采样会导致 ASR 逐字重复)
    resampleTo16k(input, inputSampleRate) {
        const targetRate = 16000;
        if (!inputSampleRate || inputSampleRate === targetRate) {
            return input;
        }
        const ratio = inputSampleRate / targetRate;
        const outLen = Math.max(1, Math.floor(input.length / ratio));
        const output = new Int16Array(outLen);
        for (let i = 0; i < outLen; i++) {
            const srcIdx = i * ratio;
            const idx0 = Math.floor(srcIdx);
            const idx1 = Math.min(idx0 + 1, input.length - 1);
            const frac = srcIdx - idx0;
            output[i] = Math.round(input[idx0] * (1 - frac) + input[idx1] * frac);
        }
        return output;
    }

    // Process PCM buffer data
    processPCMBuffer(buffer) {
        if (!this.isRecording) return;
        const srcRate = this.audioContext?.sampleRate || 16000;
        const pcm = srcRate !== 16000 ? this.resampleTo16k(buffer, srcRate) : buffer;
        const newBuffer = new Int16Array(this.pcmDataBuffer.length + pcm.length);
        newBuffer.set(this.pcmDataBuffer);
        newBuffer.set(pcm, this.pcmDataBuffer.length);
        this.pcmDataBuffer = newBuffer;
        const samplesPerFrame = 960;
        while (this.pcmDataBuffer.length >= samplesPerFrame) {
            const frameData = this.pcmDataBuffer.slice(0, samplesPerFrame);
            this.pcmDataBuffer = this.pcmDataBuffer.slice(samplesPerFrame);
            this.encodeAndSendOpus(frameData);
        }
    }

    // Encode and send Opus data
    encodeAndSendOpus(pcmData = null) {
        // 仅远程麦克风在线时暂停主机本地麦克风，TTS 期间保持录音以支持打断
        if (window.__MUSE_PAUSE_MIC__) {
            return;
        }
        if (!this.opusEncoder) {
            log('Opus编码器未初始化', 'error');
            return;
        }
        try {
            if (pcmData) {
                const opusData = this.opusEncoder.encode(pcmData);
                if (opusData && opusData.length > 0) {
                    this.audioBuffers.push(opusData.buffer);
                    this.totalAudioSize += opusData.length;
                    if (this.websocket && this.websocket.readyState === WebSocket.OPEN) {
                        try {
                            this.websocket.send(opusData.buffer);
                            this._framesSent = (this._framesSent || 0) + 1;
                        } catch (error) {
                            log(`WebSocket发送错误: ${error.message}`, 'error');
                        }
                    }
                } else {
                    log('Opus编码失败，未返回有效数据', 'error');
                }
            } else {
                if (this.pcmDataBuffer.length > 0) {
                    const samplesPerFrame = 960;
                    if (this.pcmDataBuffer.length < samplesPerFrame) {
                        const paddedBuffer = new Int16Array(samplesPerFrame);
                        paddedBuffer.set(this.pcmDataBuffer);
                        this.encodeAndSendOpus(paddedBuffer);
                    } else {
                        this.encodeAndSendOpus(this.pcmDataBuffer.slice(0, samplesPerFrame));
                    }
                    this.pcmDataBuffer = new Int16Array(0);
                }
            }
        } catch (error) {
            log(`Opus编码错误: ${error.message}`, 'error');
        }
    }

    _liveSharedMic() {
        const usable = (stream) => stream && stream.getAudioTracks && stream.getAudioTracks().some(t => t.readyState === 'live' && t.enabled !== false);
        // 只复用「本窗口」拿到的麦流。父页面 getUserMedia 的 track 塞进 iframe AudioContext
        // 在 Safari 上经常是静音，会导致「连着但 ASR 听不见」。
        if (usable(window.__MUSE_MIC_STREAM__)) return window.__MUSE_MIC_STREAM__;
        if (usable(this.mediaStream)) return this.mediaStream;
        return null;
    }

    _rememberSharedMic(stream) {
        this.mediaStream = stream;
        window.__MUSE_MIC_STREAM__ = stream;
    }

    _teardownGraph() {
        try {
            if (this.audioProcessor) {
                if (this.audioProcessorType === 'worklet' && this.audioProcessor.port) {
                    this.audioProcessor.port.postMessage({ command: 'stop' });
                }
                this.audioProcessor.disconnect();
            }
        } catch (_) { /* ignore */ }
        try { this.audioSource?.disconnect(); } catch (_) { /* ignore */ }
        this.audioProcessor = null;
        this.audioSource = null;
        this.analyser = null;
        if (this.visualizationRequest) {
            cancelAnimationFrame(this.visualizationRequest);
            this.visualizationRequest = null;
        }
        if (this.recordingTimer) {
            clearInterval(this.recordingTimer);
            this.recordingTimer = null;
        }
        this.isRecording = false;
    }

    // Start recording
    async start() {
        if (this._starting) return false;
        if (this.isRecording) {
            // 已在录：确保绑的是当前 WS
            if (this.websocket && this.websocket.readyState === WebSocket.OPEN) return true;
            this._teardownGraph();
        }
        this._starting = true;
        try {
            if (!this.initEncoder()) {
                log('无法开始录音: Opus编码器初始化失败', 'error');
                return false;
            }
            if (!this.websocket || this.websocket.readyState !== WebSocket.OPEN) {
                log('WebSocket未连接，无法开始录音', 'error');
                return false;
            }
            log('请至少录制1-2秒音频以确保收集足够的数据', 'info');
            // 复用本窗口已授权麦流；否则在 iframe 内重新 getUserMedia（权限已授则不再弹窗）
            let stream = this._liveSharedMic();
            if (!stream) {
                stream = await navigator.mediaDevices.getUserMedia(buildMicConstraints());
                this._rememberSharedMic(stream);
                log('已获取麦克风权限（本会话内将复用）', 'success');
            } else {
                this._rememberSharedMic(stream);
                log('复用已授权麦克风，跳过权限询问', 'info');
            }
            this.audioContext = this.getRecordAudioContext();
            if (this.audioContext.state === 'suspended') {
                try { await this.audioContext.resume(); } catch (_) { /* Safari 偶发 */ }
            }
            if (this.audioContext.state === 'suspended') {
                await new Promise(r => setTimeout(r, 50));
                try { await this.audioContext.resume(); } catch (_) { /* ignore */ }
            }
            if (this.audioContext.state === 'suspended') {
                log('AudioContext 仍处于 suspended，语音可能无输入（点一下页面后再说）', 'warning');
                return false;
            }
            const processorResult = await this.createAudioProcessor();
            if (!processorResult) {
                log('无法创建音频处理器', 'error');
                return false;
            }
            this.audioProcessor = processorResult.node;
            this.audioProcessorType = processorResult.type;
            this.audioSource = this.audioContext.createMediaStreamSource(stream);
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 2048;
            this.audioSource.connect(this.analyser);
            this.audioSource.connect(this.audioProcessor);
            this.pcmDataBuffer = new Int16Array();
            this.audioBuffers = [];
            this.totalAudioSize = 0;
            this.isRecording = true;
            this._framesSent = 0;
            const ctxRate = this.audioContext?.sampleRate || 16000;
            if (ctxRate !== 16000) {
                log(`录音采样率 ${ctxRate}Hz，将重采样到 16000Hz`, 'info');
            }
            if (this.audioProcessorType === 'worklet' && this.audioProcessor.port) {
                this.audioProcessor.port.postMessage({ command: 'start' });
            }
            if (this.onVisualizerUpdate) {
                const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
                this.startVisualization(dataArray);
            }
            if (this.onRecordingStart) {
                this.onRecordingStart(0);
            }
            let recordingSeconds = 0;
            this.recordingTimer = setInterval(() => {
                recordingSeconds += 0.1;
                if (this.onRecordingStart) {
                    this.onRecordingStart(recordingSeconds);
                }
            }, 100);
            log('已开始PCM直接录音', 'success');
            return true;
        } catch (error) {
            log(`直接录音启动错误: ${error.message}`, 'error');
            this._teardownGraph();
            return false;
        } finally {
            this._starting = false;
        }
    }

    // Start visualization
    startVisualization(dataArray) {
        const draw = () => {
            this.visualizationRequest = requestAnimationFrame(() => draw());
            if (!this.isRecording) return;
            this.analyser.getByteFrequencyData(dataArray);
            if (this.onVisualizerUpdate) {
                this.onVisualizerUpdate(dataArray);
            }
        };
        draw();
    }

    // Stop recording
    stop() {
        if (!this.isRecording && !this.audioProcessor && !this.audioSource) return false;
        try {
            // Encode and send remaining data before tearing down
            if (this.isRecording) this.encodeAndSendOpus();
            if (this.websocket && this.websocket.readyState === WebSocket.OPEN) {
                const emptyOpusFrame = new Uint8Array(0);
                this.websocket.send(emptyOpusFrame);
                log('已发送录音停止信号', 'info');
            }
            this._teardownGraph();
            if (this.onRecordingStop) {
                this.onRecordingStop();
            }
            log('已停止PCM直接录音', 'success');
            return true;
        } catch (error) {
            log(`直接录音停止错误: ${error.message}`, 'error');
            this._teardownGraph();
            return false;
        }
    }

    /** 供 bridge 巡检：是否真的在往 WS 送麦 */
    getUplinkStats() {
        return {
            recording: !!this.isRecording,
            ctx: this.audioContext?.state || this.recordAudioContext?.state || 'none',
            framesSent: this._framesSent || 0,
            wsOpen: !!(this.websocket && this.websocket.readyState === WebSocket.OPEN),
        };
    }

    // Get analyser
    getAnalyser() {
        return this.analyser;
    }
}

// Create singleton instance
let audioRecorderInstance = null;

export function getAudioRecorder() {
    if (!audioRecorderInstance) {
        audioRecorderInstance = new AudioRecorder();
    }
    return audioRecorderInstance;
}

/**
 * Check if microphone is available without forcing a permission prompt.
 * Opening+stopping getUserMedia on every page enter re-triggers OS/browser prompts.
 */
export async function checkMicrophoneAvailability() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        log('浏览器不支持getUserMedia API', 'warning');
        return false;
    }
    try {
        if (navigator.permissions && navigator.permissions.query) {
            const status = await navigator.permissions.query({ name: 'microphone' });
            if (status.state === 'denied') {
                log('麦克风权限已被拒绝', 'warning');
                return false;
            }
            // granted / prompt：设备可用；真正开麦留给录音启动，避免探测时抢权限
            log(`麦克风权限状态: ${status.state}`, 'info');
            return true;
        }
    } catch (_) {
        /* Safari 等可能不支持 microphone permission query */
    }
    try {
        const devices = await navigator.mediaDevices.enumerateDevices();
        const mics = devices.filter(d => d.kind === 'audioinput');
        if (!mics.length) {
            log('未发现麦克风设备', 'warning');
            return false;
        }
        log('麦克风设备已枚举，延迟到录音时申请权限', 'info');
        return true;
    } catch (error) {
        log(`麦克风可用性检查失败: ${error.message}`, 'warning');
        return true; // 乐观：真正失败会在 start() 暴露
    }
}

/**
 * Check if it is HTTP non-localhost access
 * @returns {boolean} Returns true if it is HTTP non-localhost access
 */
export function isHttpNonLocalhost() {
    const protocol = window.location.protocol;
    const hostname = window.location.hostname;
    // Check if it is HTTP protocol
    if (protocol !== 'http:') {
        return false;
    }
    // localhost and 127.0.0.1 can use microphone
    if (hostname === 'localhost' || hostname === '127.0.0.1') {
        return false;
    }
    // Private IP addresses can also use microphone (browser allows)
    if (hostname.startsWith('192.168.') || hostname.startsWith('10.') || hostname.startsWith('172.')) {
        return false;
    }
    // Other HTTP access is considered non-localhost
    return true;
}
