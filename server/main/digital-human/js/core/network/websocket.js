// WebSocket消息处理模块
import { getConfig, saveConnectionUrls } from '../../config/manager.js?v=0205';
import { uiController } from '../../ui/controller.js?v=0271';
import { log } from '../../utils/logger.js?v=0256';
import { getAudioPlayer } from '../audio/player.js?v=0264';
import { getAudioRecorder } from '../audio/recorder.js?v=0264';
import { executeMcpTool, getMcpTools, setWebSocket as setMcpWebSocket } from '../mcp/tools.js?v=0258';
import { webSocketConnect } from './ota-connector.js?v=0205';
import { isRemoteMode, isMuseMode, isPreviewMode, prewarmEvStack } from '../../muse-bridge.js?v=0271';
import { applySpeakerSink } from '../audio/devices.js?v=0258';

// WebSocket处理器类
export class WebSocketHandler {
    constructor() {
        this.websocket = null;
        this.onConnectionStateChange = null;
        this._pendingAssistantTexts = [];
        this._ttsAudioArrived = false;
        this._pendingTextTimer = null;
        this.onRecordButtonStateChange = null;
        this.onSessionStateChange = null;
        this.onSessionEmotionChange = null;
        this.onChatMessage = null; // 新增：聊天消息回调
        this.currentSessionId = null;
        this.isRemoteSpeaking = false;
        this._handshakePromise = null;
        this._handshakeResolve = null;
    }

    waitForHandshake(timeoutMs = 15000) {
        if (!this._handshakePromise) return Promise.resolve(false);
        return Promise.race([
            this._handshakePromise,
            new Promise(resolve => setTimeout(() => resolve(false), timeoutMs))
        ]);
    }

    _resetTtsTextSync() {
        this._pendingAssistantTexts = [];
        this._ttsAudioArrived = false;
        if (this._pendingTextTimer) {
            clearTimeout(this._pendingTextTimer);
            this._pendingTextTimer = null;
        }
    }

    _queueAssistantText(text) {
        const value = String(text || '').trim();
        if (!value || !this.onChatMessage) return;
        // Muse 宿主：仍发 utterance 作即时预览；父页以 DB id / 文本 dedupe，TTS 不再是唯一来源
        this.onChatMessage(value, false);
    }

    _flushAssistantTexts(_force = false) {
        if (this._pendingTextTimer) {
            clearTimeout(this._pendingTextTimer);
            this._pendingTextTimer = null;
        }
        this._pendingAssistantTexts = [];
    }

    _markTtsAudioArrived() {
        if (this._ttsAudioArrived) return;
        this._ttsAudioArrived = true;
        this._flushAssistantTexts(true);
    }

    async _ensurePlaybackReady() {
        try {
            const audioPlayer = getAudioPlayer();
            const ctx = audioPlayer.getAudioContext();
            if (ctx.state === 'suspended') {
                await ctx.resume();
            }
            await applySpeakerSink(ctx);
        } catch (_) {
            /* ignore */
        }
    }

    // 发送hello握手消息
    async sendHelloMessage() {
        if (!this.websocket || this.websocket.readyState !== WebSocket.OPEN) return false;

        try {
            const config = getConfig();

            const helloMessage = {
                type: 'hello',
                version: 1,
                transport: 'websocket',
                device_id: config.deviceId,
                device_name: config.deviceName,
                device_mac: config.deviceMac,
                token: config.token,
                audio_params: {
                    format: 'opus',
                    // 告知核心下行按 24kHz；上行录音仍编码为 16kHz Opus（VAD 固定 16k）
                    sample_rate: 24000,
                    channels: 1,
                    frame_duration: 60
                },
                features: {
                    mcp: true,
                    emoji: config.emojiEnabled
                }
            };

            log('发送hello握手消息', 'info');
            this.websocket.send(JSON.stringify(helloMessage));

            return new Promise(resolve => {
                const timeout = setTimeout(() => {
                    log('等待hello响应超时', 'error');
                    this.websocket.removeEventListener('message', onMessageHandler);
                    resolve(false);
                }, isRemoteMode() ? 35000 : 5000);

                const onMessageHandler = (event) => {
                    try {
                        const response = JSON.parse(event.data);
                        if (response.type === 'error') {
                            log(`服务器错误: ${response.message || response.text}`, 'error');
                            clearTimeout(timeout);
                            this.websocket.removeEventListener('message', onMessageHandler);
                            resolve(false);
                            return;
                        }
                        if (response.type === 'hello' && response.session_id) {
                            log(`服务器握手成功，会话ID: ${response.session_id}`, 'success');
                            clearTimeout(timeout);
                            this.websocket.removeEventListener('message', onMessageHandler);
                            resolve(true);
                        }
                    } catch (e) {
                        // 忽略非JSON消息
                    }
                };

                this.websocket.addEventListener('message', onMessageHandler);
            });
        } catch (error) {
            log(`发送hello消息错误: ${error.message}`, 'error');
            return false;
        }
    }

    _sendListenStartOnly(sessionId) {
        if (!this.websocket || this.websocket.readyState !== WebSocket.OPEN) return;
        this.websocket.send(JSON.stringify({
            session_id: sessionId,
            type: 'listen',
            state: 'start',
            mode: 'auto'
        }));
        log('已开启语音监听（不发送模拟唤醒词）', 'info');
    }

    _sendWakeupMessages(sessionId) {
        if (!this.websocket || this.websocket.readyState !== WebSocket.OPEN) return;

        // listen detect
        this.websocket.send(JSON.stringify({
            session_id: sessionId,
            type: 'listen',
            state: 'detect',
            text: '嘿，你好呀'
        }));
        log('发送listen detect消息，唤醒词: 嘿，你好呀', 'info');

        // listen start：开始监听
        this.websocket.send(JSON.stringify({
            session_id: sessionId,
            type: 'listen',
            state: 'start',
            mode: 'auto'
        }));
        log('发送listen start消息', 'info');
    }

    // 处理文本消息
    handleTextMessage(message) {
        if (message.type === 'hello') {
            log(`服务器回应：${JSON.stringify(message, null, 2)}`, 'success');
            window.cameraAvailable = true;
            log('连接成功，摄像头已可用', 'success');
            uiController.updateDialButton(true);

            if (isRemoteMode()) {
                this._handshakeResolve?.(true);
            } else if (isMuseMode()) {
                // Muse EV 终端：刷新/重连只开麦，不模拟唤醒词，避免触发「你好呀」等自动对话
                this._sendListenStartOnly(message.session_id);
            } else {
                this._sendWakeupMessages(message.session_id);
            }

            uiController.startAIChatSession();
            void this._ensurePlaybackReady();
        } else if (message.type === 'error') {
            const errText = message.message || message.text || '连接被拒绝';
            log(`服务器错误: ${errText}`, 'error');
            this._handshakeResolve?.(false);
            if (this.onChatMessage) {
                this.onChatMessage(errText, false);
            }
        } else if (message.type === 'muse_remote') {
            if (!isRemoteMode()) {
                window.__MUSE_PAUSE_MIC__ = !!message.active;
                if (message.active) {
                    import('../audio/recorder.js?v=0264').then(({ getAudioRecorder }) => {
                        getAudioRecorder().stop();
                    });
                }
            }
        } else if (message.type === 'tts') {
            this.handleTTSMessage(message);
        } else if (message.type === 'audio') {
            log(`收到音频控制消息: ${JSON.stringify(message)}`, 'info');
        } else if (message.type === 'stt') {
            log(`识别结果: ${message.text}`, 'info');
            // 检查是否需要绑定设备
            if (message.text && (message.text.includes('绑定') || message.text.includes('bind'))) {
                log('收到设备绑定提示，更新摄像头状态', 'warning');
                window.cameraAvailable = false;
                // 关闭摄像头
                if (typeof window.stopCamera === 'function') {
                    window.stopCamera();
                }
                // 更新摄像头按钮状态
                const cameraBtn = document.getElementById('cameraBtn');
                if (cameraBtn) {
                    cameraBtn.classList.remove('camera-active');
                    cameraBtn.querySelector('.btn-text').textContent = '摄像头';
                    cameraBtn.disabled = true;
                    cameraBtn.title = '请先绑定验证码';
                }
            }
            // 使用新的聊天消息回调显示STT消息
            if (this.onChatMessage && message.text) {
                this.onChatMessage(message.text, true);
            }
            if (isMuseMode() && !isRemoteMode()) {
                const params = new URLSearchParams(window.location.search);
                void prewarmEvStack(Number(params.get('agent_id')) || 1);
            }
        } else if (message.type === 'llm') {
            log(`大模型回复: ${message.text}`, 'info');
            // 助手正文：Muse 以 DB + 父页 poll 为准；此处只处理情绪/口型
            if (message.text && /[\u{1F300}-\u{1F9FF}]|[\u{2600}-\u{26FF}]|[\u{2700}-\u{27BF}]/u.test(message.text)) {
                const emojiMatch = message.text.match(/[\u{1F300}-\u{1F9FF}]|[\u{2600}-\u{26FF}]|[\u{2700}-\u{27BF}]/u);
                if (emojiMatch && this.onSessionEmotionChange) {
                    this.onSessionEmotionChange(emojiMatch[0]);
                }
            }
            if (message.emotion) {
                this.triggerLive2DEmotionAction(message.emotion);
            }
        } else if (message.type === 'mcp') {
            this.handleMCPMessage(message);
        } else {
            log(`未知消息类型: ${message.type}`, 'info');
            if (this.onChatMessage) {
                this.onChatMessage(`未知消息类型: ${message.type}\n${JSON.stringify(message, null, 2)}`, false);
            }
        }
    }

    // 处理TTS消息
    handleTTSMessage(message) {
        if (message.state === 'start') {
            log('服务器开始发送语音', 'info');
            this.currentSessionId = message.session_id;
            this.isRemoteSpeaking = true;
            this._resetTtsTextSync();
            // 新一轮才清空；stop 时清空会把未播完的 Opus 缓冲抹掉导致无声
            const audioPlayer = getAudioPlayer();
            audioPlayer.clearAllAudio();
            void audioPlayer.resumeContext();
            void this._ensurePlaybackReady();
            if (this.onSessionStateChange) {
                this.onSessionStateChange(true);
            }

            // 启动Live2D说话动画
            this.startLive2DTalking();
        } else if (message.state === 'sentence_start') {
            log(`服务器发送语音段: ${message.text}`, 'info');
            this.ttsSentenceCount = (this.ttsSentenceCount || 0) + 1;

            if (message.text) {
                this._queueAssistantText(message.text);
            }

            // 确保动画在句子开始时运行
            const live2dManager = window.chatApp?.live2dManager;
            if (live2dManager && !live2dManager.isTalking) {
                this.startLive2DTalking();
            }
        } else if (message.state === 'sentence_end') {
            log(`语音段结束: ${message.text}`, 'info');

            // 句子结束时不清除动画，等待下一个句子或最终停止
        } else if (message.state === 'stop') {
            log('服务器语音传输结束，等待本地缓冲播完', 'info');
            this._flushAssistantTexts(true);

            // stop = 发包结束，不是「立刻静音」。让队列自然播完。
            const audioPlayer = getAudioPlayer();
            audioPlayer.markEndOfStream();

            this.isRemoteSpeaking = false;
            if (this.onRecordButtonStateChange) {
                this.onRecordButtonStateChange(false);
            }
            if (this.onSessionStateChange) {
                this.onSessionStateChange(false);
            }

            // 延迟停止Live2D说话动画，确保所有句子都播放完毕
            setTimeout(() => {
                this.stopLive2DTalking();
                this.ttsSentenceCount = 0; // 重置计数器
            }, 1000); // 1秒延迟，确保所有句子都完成
        }
    }

    // 启动说话形象动画（Live2D 或声波可视化）
    startLive2DTalking() {
        try {
            const viz = window.chatApp?.soundVisualizer;
            if (viz) {
                viz.setSpeaking(true);
                log('声波可视化说话态已启动', 'info');
                return;
            }
            const live2dManager = window.chatApp?.live2dManager;
            if (live2dManager && live2dManager.live2dModel) {
                live2dManager.startTalking();
                log('Live2D说话动画已启动', 'info');
            }
        } catch (error) {
            log(`启动说话形象动画失败: ${error.message}`, 'error');
        }
    }

    // 停止说话形象动画
    stopLive2DTalking() {
        try {
            const viz = window.chatApp?.soundVisualizer;
            if (viz) {
                viz.setSpeaking(false);
                log('声波可视化说话态已停止', 'info');
                return;
            }
            const live2dManager = window.chatApp?.live2dManager;
            if (live2dManager) {
                live2dManager.stopTalking();
                log('Live2D说话动画已停止', 'info');
            }
        } catch (error) {
            log(`停止说话形象动画失败: ${error.message}`, 'error');
        }
    }

    // 初始化音频分析器（Live2D 嘴型 / 声波可视化）
    initializeLive2DAudioAnalyzer() {
        try {
            const viz = window.chatApp?.soundVisualizer;
            if (viz) {
                if (viz.connectToAudioPlayer()) {
                    log('声波可视化已连接音频分析器', 'success');
                }
                return;
            }
            const live2dManager = window.chatApp?.live2dManager;
            if (live2dManager) {
                if (live2dManager.initializeAudioAnalyzer()) {
                    log('Live2D音频分析器初始化完成，已连接到音频播放器', 'success');
                } else {
                    log('Live2D音频分析器初始化失败，将使用模拟动画', 'warning');
                }
            }
        } catch (error) {
            log(`初始化音频分析器失败: ${error.message}`, 'error');
        }
    }

    // 处理MCP消息
    handleMCPMessage(message) {
        const payload = message.payload || {};
        log(`服务器下发: ${JSON.stringify(message)}`, 'info');

        if (payload.method === 'tools/list') {
            const tools = getMcpTools();

            const replyMessage = JSON.stringify({
                "session_id": message.session_id || "",
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "id": payload.id,
                    "result": {
                        "tools": tools
                    }
                }
            });
            log(`客户端上报: ${replyMessage}`, 'info');
            this.websocket.send(replyMessage);
            log(`回复MCP工具列表: ${tools.length} 个工具`, 'info');

        } else if (payload.method === 'tools/call') {
            const toolName = payload.params?.name;
            const toolArgs = payload.params?.arguments;

            log(`调用工具: ${toolName} 参数: ${JSON.stringify(toolArgs)}`, 'info');

            executeMcpTool(toolName, toolArgs).then(result => {
                const replyMessage = JSON.stringify({
                    "session_id": message.session_id || "",
                    "type": "mcp",
                    "payload": {
                        "jsonrpc": "2.0",
                        "id": payload.id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": JSON.stringify(result)
                                }
                            ],
                            "isError": false
                        }
                    }
                });

                log(`客户端上报: ${replyMessage}`, 'info');
                this.websocket.send(replyMessage);
            }).catch(error => {
                log(`工具执行失败: ${error.message}`, 'error');
                const errorReply = JSON.stringify({
                    "session_id": message.session_id || "",
                    "type": "mcp",
                    "payload": {
                        "jsonrpc": "2.0",
                        "id": payload.id,
                        "error": {
                            "code": -32603,
                            "message": error.message
                        }
                    }
                });
                this.websocket.send(errorReply);
            });
        } else if (payload.method === 'initialize') {
            log(`收到工具初始化请求: ${JSON.stringify(payload.params)}`, 'info');
            // 保存视觉分析接口地址
            const visionUrl = document.getElementById('visionUrl');
            const visionConfig = payload?.params?.capabilities?.vision;
            if (visionConfig && typeof visionConfig === 'object' && visionConfig.url && visionConfig.token) {
                const visionConfigStr = JSON.stringify(visionConfig);
                localStorage.setItem('xz_tester_vision', visionConfigStr);
                if (visionUrl) visionUrl.value = visionConfig.url;
            } else {
                localStorage.removeItem('xz_tester_vision');
                if (visionUrl) visionUrl.value = '';
            }

            const replyMessage = JSON.stringify({
                "session_id": message.session_id || "",
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "id": payload.id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "tools": {}
                        },
                        "serverInfo": {
                            "name": "xiaozhi-web-test",
                            "version": "2.1.0"
                        }
                    }
                }
            });
            log(`回复初始化响应`, 'info');
            this.websocket.send(replyMessage);
        } else {
            log(`未知的MCP方法: ${payload.method}`, 'warning');
        }
    }

    // 处理二进制消息
    async handleBinaryMessage(data) {
        try {
            let arrayBuffer;
            if (data instanceof ArrayBuffer) {
                arrayBuffer = data;
            } else if (data instanceof Blob) {
                arrayBuffer = await data.arrayBuffer();
                log(`收到Blob音频数据，大小: ${arrayBuffer.byteLength}字节`, 'debug');
            } else {
                log(`收到未知类型的二进制数据: ${typeof data}`, 'warning');
                return;
            }

            const opusData = new Uint8Array(arrayBuffer);
            const audioPlayer = getAudioPlayer();
            if (isMuseMode() && !isRemoteMode() && opusData.length > 0) {
                this._markTtsAudioArrived();
            }
            if (opusData.length > 0) {
                void this._ensurePlaybackReady();
            }
            audioPlayer.enqueueAudioData(opusData);
        } catch (error) {
            log(`处理二进制消息出错: ${error.message}`, 'error');
        }
    }

    // 连接WebSocket服务器
    async connect() {
        if (isPreviewMode()) {
            log('预览模式：拒绝浏览器语音 WS（只用本机麦克风）', 'info');
            return false;
        }
        const config = getConfig();
        log('正在检查OTA状态...', 'info');
        saveConnectionUrls();

        try {
            const otaUrl = document.getElementById('otaUrl').value.trim();
            const ws = await webSocketConnect(otaUrl, config);
            if (ws === undefined) {
                return false;
            }
            this.websocket = ws;
            this._handshakePromise = new Promise(resolve => { this._handshakeResolve = resolve; });

            // 设置接收二进制数据的类型为ArrayBuffer
            this.websocket.binaryType = 'arraybuffer';

            // 设置 MCP 模块的 WebSocket 实例
            setMcpWebSocket(this.websocket);

            // 设置录音器的WebSocket
            const audioRecorder = getAudioRecorder();
            audioRecorder.setWebSocket(this.websocket);

            this.setupEventHandlers();

            return true;
        } catch (error) {
            log(`连接错误: ${error.message}`, 'error');
            if (this.onConnectionStateChange) {
                this.onConnectionStateChange(false);
            }
            return false;
        }
    }

    // 设置事件处理器
    setupEventHandlers() {
        this.websocket.onopen = async () => {
            const url = document.getElementById('serverUrl').value;
            log(`已连接到服务器: ${url}`, 'success');

            if (this.onConnectionStateChange) {
                this.onConnectionStateChange(true);
            }

            // 连接成功后，默认状态为聆听中
            this.isRemoteSpeaking = false;
            if (this.onSessionStateChange) {
                this.onSessionStateChange(false);
            }

            // 在WebSocket连接成功时初始化Live2D音频分析器
            this.initializeLive2DAudioAnalyzer();

            await this.sendHelloMessage().then(ok => {
                this._handshakeResolve?.(ok);
            });
        };

        this.websocket.onclose = () => {
            log('已断开连接', 'info');

            if (this.onConnectionStateChange) {
                this.onConnectionStateChange(false);
            }

            const audioRecorder = getAudioRecorder();
            audioRecorder.stop();

            // 关闭摄像头
            if (typeof window.stopCamera === 'function') {
                window.stopCamera();
            }

            // 隐藏摄像头显示区域
            const cameraContainer = document.getElementById('cameraContainer');
            if (cameraContainer) {
                cameraContainer.classList.remove('active');
            }
        };

        this.websocket.onerror = (error) => {
            log(`WebSocket错误: ${error.message || '未知错误'}`, 'error');
            uiController.addChatMessage(`⚠️ WebSocket错误: ${error.message || '未知错误'}`, false);
            if (this.onConnectionStateChange) {
                this.onConnectionStateChange(false);
            }
        };

        this.websocket.onmessage = (event) => {
            try {
                if (typeof event.data === 'string') {
                    const message = JSON.parse(event.data);
                    this.handleTextMessage(message);
                } else {
                    this.handleBinaryMessage(event.data);
                }
            } catch (error) {
                log(`WebSocket消息处理错误: ${error.message}`, 'error');
                // 不再使用旧的addMessage函数，因为conversationDiv元素不存在
                // 错误消息将通过其他方式显示
            }
        };
    }

    // 断开连接
    disconnect() {
        if (!this.websocket) return;

        this.websocket.close();
        const audioRecorder = getAudioRecorder();
        audioRecorder.stop();

        // 关闭摄像头
        if (typeof window.stopCamera === 'function') {
            window.stopCamera();
        }

        // 隐藏摄像头显示区域
        const cameraContainer = document.getElementById('cameraContainer');
        if (cameraContainer) {
            cameraContainer.classList.remove('active');
        }
    }

    // 发送文本消息
    sendTextMessage(text) {
        if (text === '' || !this.websocket || this.websocket.readyState !== WebSocket.OPEN) {
            return false;
        }

        try {
            // 点击发送时顺带唤醒 AudioContext（避免自动播放策略卡住）
            void this._ensurePlaybackReady();
            const audioPlayer = getAudioPlayer();
            void audioPlayer.resumeContext();

            // 如果对方正在说话，先发送打断消息并清空旧缓冲
            if (this.isRemoteSpeaking && this.currentSessionId) {
                const abortMessage = {
                    session_id: this.currentSessionId,
                    type: 'abort',
                    reason: 'wake_word_detected'
                };
                this.websocket.send(JSON.stringify(abortMessage));
                audioPlayer.clearAllAudio();
                log('发送打断消息', 'info');
            }

            const listenMessage = {
                type: 'listen',
                state: 'detect',
                text: text
            };

            this.websocket.send(JSON.stringify(listenMessage));
            log(`发送文本消息: ${text}`, 'info');

            return true;
        } catch (error) {
            log(`发送消息错误: ${error.message}`, 'error');
            return false;
        }
    }

    /**
     * 触发Live2D情绪动作
     * @param {string} emotion - 情绪名称
     */
    triggerLive2DEmotionAction(emotion) {
        try {
            const live2dManager = window.chatApp?.live2dManager;
            if (live2dManager && typeof live2dManager.triggerEmotionAction === 'function') {
                live2dManager.triggerEmotionAction(emotion);
                log(`触发Live2D情绪动作: ${emotion}`, 'info');
            } else {
                log(`无法触发Live2D情绪动作: Live2D管理器未找到或方法不可用`, 'warning');
            }
        } catch (error) {
            log(`触发Live2D情绪动作失败: ${error.message}`, 'error');
        }
    }

    // 获取WebSocket实例
    getWebSocket() {
        return this.websocket;
    }

    // 检查是否已连接
    isConnected() {
        return this.websocket && this.websocket.readyState === WebSocket.OPEN;
    }
}

// 创建单例
let wsHandlerInstance = null;

export function getWebSocketHandler() {
    if (!wsHandlerInstance) {
        wsHandlerInstance = new WebSocketHandler();
    }
    return wsHandlerInstance;
}
