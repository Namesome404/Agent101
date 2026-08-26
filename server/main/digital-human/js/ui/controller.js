// UI controller module
import { loadConfig, saveConfig } from '../config/manager.js?v=0205';
import { getAudioPlayer } from '../core/audio/player.js?v=0264';
import { getAudioRecorder } from '../core/audio/recorder.js?v=0264';
import { requestWakewordBridge, stopWakewordBridgeListener, startWakewordBridgeListener, getWakewordBridgeUrl, onNextBridgeConnected } from '../core/network/wakeword-bridge.js?v=0205';
import { getWebSocketHandler } from '../core/network/websocket.js?v=0271';
import { isMuseMode, isRemoteMode, isPreviewMode, markRemoteSessionJoined } from '../muse-bridge.js?v=0271';
import { log } from '../utils/logger.js?v=0205';

// UI controller class
class UIController {
    constructor() {
        this.isEditing = false;
        this.visualizerCanvas = null;
        this.visualizerContext = null;
        this.audioStatsTimer = null;
        this.currentBackgroundIndex = 0;
        this.dialBtnDisabled = false;
        this.isConnecting = false;
        this.lastWakewordDialTime = 0;

        // Bind methods
        this.init = this.init.bind(this);
        this.initEventListeners = this.initEventListeners.bind(this);
        this.updateDialButton = this.updateDialButton.bind(this);
        this.addChatMessage = this.addChatMessage.bind(this);
        this.switchBackground = this.switchBackground.bind(this);
        this.switchLive2DModel = this.switchLive2DModel.bind(this);
        this.showModal = this.showModal.bind(this);
        this.hideModal = this.hideModal.bind(this);
        this.switchTab = this.switchTab.bind(this);
        this.applyWakewordConfig = this.applyWakewordConfig.bind(this);
        this.handleApplyWakeword = this.handleApplyWakeword.bind(this);
        this.triggerWakewordDial = this.triggerWakewordDial.bind(this);
    }

    // Initialize
    init() {
        console.log('UIController init started');

        this.visualizerCanvas = document.getElementById('audioVisualizer');
        if (this.visualizerCanvas) {
            this.visualizerContext = this.visualizerCanvas.getContext('2d');
            this.initVisualizer();
        }

        // Check if connect button exists during initialization
        const connectBtn = document.getElementById('connectBtn');
        console.log('connectBtn during init:', connectBtn);

        this.initEventListeners();
        this.startAudioStatsMonitor();
        loadConfig();

        // Register recording callback
        const audioRecorder = getAudioRecorder();
        audioRecorder.onRecordingStart = (seconds) => {
            this.updateRecordButtonState(true, seconds);
        };

        // Initialize status display
        this.updateConnectionUI(false);

        this.updateDialButton(false);

        console.log('UIController init completed');
    }

    // Initialize visualizer
    initVisualizer() {
        if (this.visualizerCanvas) {
            this.visualizerCanvas.width = this.visualizerCanvas.clientWidth;
            this.visualizerCanvas.height = this.visualizerCanvas.clientHeight;
            this.visualizerContext.fillStyle = '#fafafa';
            this.visualizerContext.fillRect(0, 0, this.visualizerCanvas.width, this.visualizerCanvas.height);
        }
    }

    // Initialize event listeners
    initEventListeners() {
        // Settings button
        const settingsBtn = document.getElementById('settingsBtn');
        if (settingsBtn) {
            settingsBtn.addEventListener('click', () => {
                this.showModal('settingsModal');
            });
        }

        // Model select change event
        const modelSelect = document.getElementById('live2dModelSelect');
        if (modelSelect) {
            modelSelect.addEventListener('change', () => {
                this.switchLive2DModel();
            });
        }

        // Camera switch button
        const cameraSwitch = document.getElementById('cameraSwitch');
        const cameraSwitchMask = document.getElementById('cameraSwitchMask');
        if (cameraSwitchMask) {
            cameraSwitchMask.addEventListener('click', () => {
                const isCameraActive = cameraSwitch.classList.contains('active');
                if (isCameraActive) {
                    window.switchCamera();
                }
            })
        }

        // Dial button
        const dialBtn = document.getElementById('dialBtn');
        if (dialBtn) {
            dialBtn.addEventListener('click', () => {
                dialBtn.disabled = true;
                this.dialBtnDisabled = true;
                setTimeout(() => {
                    dialBtn.disabled = false;
                    this.dialBtnDisabled = false;
                }, 3000);

                const wsHandler = getWebSocketHandler();
                const isConnected = wsHandler.isConnected();

                if (isConnected) {
                    wsHandler.disconnect();
                    this.updateDialButton(false);
                    if (cameraSwitch) cameraSwitch.classList.remove('active');
                    this.addChatMessage('Disconnected, see you next time~😊', false);
                } else {
                    // Check if OTA URL is filled
                    const otaUrlInput = document.getElementById('otaUrl');
                    if (!otaUrlInput || !otaUrlInput.value.trim()) {
                        // If OTA URL is not filled, show settings modal and switch to device tab
                        this.showModal('settingsModal');
                        this.switchTab('device');
                        this.addChatMessage('Please fill in OTA server URL', false);
                        return;
                    }

                    // Start connection process
                    this.handleConnect();
                }
            });
        }

        // Camera button
        const cameraBtn = document.getElementById('cameraBtn');
        let cameraTimer = null;
        if (cameraBtn) {
            cameraBtn.addEventListener('click', () => {
                if (cameraTimer) {
                    clearTimeout(cameraTimer);
                    cameraTimer = null;
                }
                cameraTimer = setTimeout(() => {
                    const cameraContainer = document.getElementById('cameraContainer');
                    if (!cameraContainer) {
                        log('摄像头容器不存在', 'warning');
                        return;
                    }

                    const isActive = cameraContainer.classList.contains('active');
                    if (isActive) {
                        // 关闭摄像头
                        if (typeof window.stopCamera === 'function') {
                            if (cameraSwitch) cameraSwitch.classList.remove('active');
                            window.stopCamera();
                        }
                        cameraContainer.classList.remove('active');
                        cameraBtn.classList.remove('camera-active');
                        cameraBtn.querySelector('.btn-text').textContent = '摄像头';
                        log('摄像头已关闭', 'info');
                    } else {
                        // 打开摄像头
                        if (typeof window.startCamera === 'function') {
                            window.startCamera().then(success => {
                                if (success) {
                                    cameraBtn.classList.add('camera-active');
                                    cameraBtn.querySelector('.btn-text').textContent = '关闭';
                                } else {
                                    this.addChatMessage('⚠️ 摄像头启动失败，请检查浏览器权限', false);
                                }
                            }).catch(error => {
                                log(`启动摄像头异常: ${error.message}`, 'error');
                            });
                        } else {
                            log('startCamera函数未定义', 'warning');
                        }
                    }
                }, 300);
            });
        }

        // Record button
        const recordBtn = document.getElementById('recordBtn');
        if (recordBtn) {
            let recordTimer = null;
            recordBtn.addEventListener('click', () => {
                if (recordTimer) {
                    clearTimeout(recordTimer);
                    recordTimer = null;
                }
                recordTimer = setTimeout(() => {
                    const audioRecorder = getAudioRecorder();
                    if (audioRecorder.isRecording) {
                        audioRecorder.stop();
                        // Restore record button to normal state
                        recordBtn.classList.remove('recording');
                        recordBtn.querySelector('.btn-text').textContent = '录音';
                    } else {
                        // Update button state to recording
                        recordBtn.classList.add('recording');
                        recordBtn.querySelector('.btn-text').textContent = '录音中';

                        // Start recording, update button state after delay
                        setTimeout(() => {
                            audioRecorder.start();
                        }, 100);
                    }
                }, 300);
            });
        }

        // Chat input event listener
        const chatIpt = document.getElementById('chatIpt');
        if (chatIpt) {
            const wsHandler = getWebSocketHandler();
            chatIpt.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    if (e.target.value) {
                        wsHandler.sendTextMessage(e.target.value);
                        e.target.value = '';
                        return;
                    }
                }
            });
        }

        // Close button
        const closeButtons = document.querySelectorAll('.close-btn');
        closeButtons.forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const modal = e.target.closest('.modal');
                if (modal) {
                    if (modal.id === 'settingsModal') {
                        saveConfig();
                    }
                    this.hideModal(modal.id);
                }
            });
        });

        // Settings tab switch
        const tabBtns = document.querySelectorAll('.tab-btn');
        tabBtns.forEach(btn => {
            btn.addEventListener('click', (e) => {
                this.switchTab(e.target.dataset.tab);
            });
        });

        const applyWakewordBtn = document.getElementById('applyWakewordBtn');
        if (applyWakewordBtn) {
            applyWakewordBtn.addEventListener('click', this.handleApplyWakeword);
        }

        // 点击模态框背景关闭（仅对特定模态框禁用此功能）
        const modals = document.querySelectorAll('.modal');
        modals.forEach(modal => {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) {
                    // settingsModal、mcpToolModal、mcpPropertyModal 只能通过点击X关闭
                    const nonClosableModals = ['settingsModal', 'mcpToolModal', 'mcpPropertyModal'];
                    if (nonClosableModals.includes(modal.id)) {
                        return; // 禁止点击背景关闭
                    }
                    this.hideModal(modal.id);
                }
            });
        });

        // Add MCP tool button
        const addMCPToolBtn = document.getElementById('addMCPToolBtn');
        if (addMCPToolBtn) {
            addMCPToolBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.addMCPTool();
            });
        }

        // Connect button and send button are not removed, can be added to dial button later
    }

    // Update connection status UI
    updateConnectionUI(isConnected) {
        if (isMuseMode()) return;
        const connectionStatus = document.getElementById('connectionStatus');
        const statusDot = document.querySelector('.status-dot');

        if (connectionStatus) {
            if (isConnected) {
                connectionStatus.textContent = '已连接';
                if (statusDot) {
                    statusDot.className = 'status-dot status-connected';
                }
            } else {
                connectionStatus.textContent = '离线';
                if (statusDot) {
                    statusDot.className = 'status-dot status-disconnected';
                }
            }
        }
    }

    // Update dial button state
    updateDialButton(isConnected) {
        const dialBtn = document.getElementById('dialBtn');
        const recordBtn = document.getElementById('recordBtn');
        const cameraBtn = document.getElementById('cameraBtn');

        if (dialBtn) {
            if (isConnected) {
                dialBtn.classList.add('dial-active');
                dialBtn.querySelector('.btn-text').textContent = '挂断';
                // Update dial button icon to hang up icon
                dialBtn.querySelector('svg').innerHTML = `
                    <path d="M12,9C10.4,9 9,10.4 9,12C9,13.6 10.4,15 12,15C13.6,15 15,13.6 15,12C15,10.4 13.6,9 12,9M12,17C9.2,17 7,14.8 7,12C7,9.2 9.2,7 12,7C14.8,7 17,9.2 17,12C17,14.8 14.8,17 12,17M12,4.5C7,4.5 2.7,7.6 1,12C2.7,16.4 7,19.5 12,19.5C17,19.5 21.3,16.4 23,12C21.3,7.6 17,4.5 12,4.5Z"/>
                `;
            } else {
                dialBtn.classList.remove('dial-active');
                dialBtn.querySelector('.btn-text').textContent = '拨号';
                // Restore dial button icon
                dialBtn.querySelector('svg').innerHTML = `
                    <path d="M6.62,10.79C8.06,13.62 10.38,15.94 13.21,17.38L15.41,15.18C15.69,14.9 16.08,14.82 16.43,14.93C17.55,15.3 18.75,15.5 20,15.5A1,1 0 0,1 21,16.5V20A1,1 0 0,1 20,21A17,17 0 0,1 3,4A1,1 0 0,1 4,3H7.5A1,1 0 0,1 8.5,4C8.5,5.25 8.7,6.45 9.07,7.57C9.18,7.92 9.1,8.31 8.82,8.59L6.62,10.79Z"/>
                `;
            }
        }

        // Update camera button state - reset to default when disconnected
        if (cameraBtn && !isConnected) {
            const cameraContainer = document.getElementById('cameraContainer');
            if (cameraContainer && cameraContainer.classList.contains('active')) {
                cameraContainer.classList.remove('active');
            }
            cameraBtn.classList.remove('camera-active');
            cameraBtn.querySelector('.btn-text').textContent = '摄像头';
            cameraBtn.disabled = true;
            cameraBtn.title = '请先连接服务器';
            // 关闭摄像头
            if (typeof window.stopCamera === 'function') {
                window.stopCamera();
            }
        }

        // Update camera button state - enable when connected and camera is available
        if (cameraBtn && isConnected) {
            if (window.cameraAvailable) {
                cameraBtn.disabled = false;
                cameraBtn.title = '打开/关闭摄像头';
            } else {
                cameraBtn.disabled = true;
                cameraBtn.title = '请先绑定验证码';
            }
        }

        // Update record button state
        if (recordBtn) {
            const microphoneAvailable = window.microphoneAvailable !== false;
            if (isConnected && microphoneAvailable) {
                recordBtn.disabled = false;
                recordBtn.title = '开始录音';
                // Restore record button to normal state
                recordBtn.querySelector('.btn-text').textContent = '录音';
                recordBtn.classList.remove('recording');
            } else {
                recordBtn.disabled = true;
                if (!microphoneAvailable) {
                    recordBtn.title = window.isHttpNonLocalhost ? '当前由于是http访问，无法录音，只能用文字交互' : '麦克风不可用';
                } else {
                    recordBtn.title = '请先连接服务器';
                }
                // Restore record button to normal state
                recordBtn.querySelector('.btn-text').textContent = '录音';
                recordBtn.classList.remove('recording');
            }
        }
    }

    // Update record button state
    updateRecordButtonState(isRecording, seconds = 0) {
        const recordBtn = document.getElementById('recordBtn');
        if (recordBtn) {
            if (isRecording) {
                recordBtn.querySelector('.btn-text').textContent = `录音中`;
                recordBtn.classList.add('recording');
            } else {
                recordBtn.querySelector('.btn-text').textContent = '录音';
                recordBtn.classList.remove('recording');
            }
            // Only enable button when microphone is available
            recordBtn.disabled = window.microphoneAvailable === false;
        }
    }

    /**
     * Update microphone availability state
     * @param {boolean} isAvailable - Whether microphone is available
     * @param {boolean} isHttpNonLocalhost - Whether it is HTTP non-localhost access
     */
    updateMicrophoneAvailability(isAvailable, isHttpNonLocalhost) {
        const recordBtn = document.getElementById('recordBtn');
        if (!recordBtn) return;
        if (!isAvailable) {
            // Disable record button
            recordBtn.disabled = true;
            // Update button text and title
            recordBtn.querySelector('.btn-text').textContent = '录音';
            recordBtn.title = isHttpNonLocalhost ? '当前由于是http访问，无法录音，只能用文字交互' : '麦克风不可用';

        } else {
            // If connected, enable record button
            const wsHandler = getWebSocketHandler();
            if (wsHandler && wsHandler.isConnected()) {
                recordBtn.disabled = false;
                recordBtn.title = '开始录音';
            }
        }
    }

    // Add chat message
    addChatMessage(content, isUser = false) {
        const chatStream = document.getElementById('chatStream');
        if (!chatStream) return;

        const messageDiv = document.createElement('div');
        messageDiv.className = `chat-message ${isUser ? 'user' : 'ai'}`;
        messageDiv.innerHTML = `<div class="message-bubble">${content}</div>`;
        chatStream.appendChild(messageDiv);

        // Scroll to bottom
        chatStream.scrollTop = chatStream.scrollHeight;
    }

    // Switch agent avatar (visualizer or Live2D)
    switchLive2DModel() {
        const modelSelect = document.getElementById('live2dModelSelect');
        if (!modelSelect) return;

        const selectedModel = modelSelect.value;
        localStorage.setItem('xz_tester_avatar', selectedModel);
        localStorage.setItem('live2dModel', selectedModel);

        if (selectedModel === 'visualizer') {
            location.reload();
            return;
        }

        const app = window.chatApp;
        if (app && app.live2dManager) {
            app.live2dManager.switchModel(selectedModel)
                .then(success => {
                    if (success) {
                        this.addChatMessage(`已切换到 Live2D 形象: ${selectedModel}`, false);
                    } else {
                        this.addChatMessage('形象切换失败', false);
                    }
                })
                .catch(error => {
                    console.error('形象切换错误:', error);
                    this.addChatMessage('形象切换出错', false);
                });
        } else {
            location.reload();
        }
    }

    // Show modal
    showModal(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.style.display = 'flex';
        }
    }

    // Hide modal
    hideModal(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.style.display = 'none';
        }
    }

    // Switch tab
    switchTab(tabName) {
        // Remove active class from all tabs
        const tabBtns = document.querySelectorAll('.tab-btn');
        const tabContents = document.querySelectorAll('.tab-content');

        tabBtns.forEach(btn => btn.classList.remove('active'));
        tabContents.forEach(content => content.classList.remove('active'));

        // Activate selected tab
        const activeTabBtn = document.querySelector(`[data-tab="${tabName}"]`);
        const activeTabContent = document.getElementById(`${tabName}Tab`);

        if (activeTabBtn && activeTabContent) {
            activeTabBtn.classList.add('active');
            activeTabContent.classList.add('active');
        }
    }

    applyWakewordConfig(config = {}) {
        const wakewordEnabledInput = document.getElementById('wakewordEnabled');
        const wakewordListInput = document.getElementById('wakewordList');

        if (!wakewordEnabledInput || !wakewordListInput) {
            return;
        }

        const wakeWords = Array.isArray(config.wakeWords)
            ? config.wakeWords.filter(item => typeof item === 'string' && item.trim())
            : [];

        wakewordEnabledInput.value = config.enabled === false ? 'false' : 'true';
        wakewordListInput.value = wakeWords.join('\n');
        saveConfig();
    }

    async handleApplyWakeword() {
        const wakewordEnabledInput = document.getElementById('wakewordEnabled');
        const wakewordListInput = document.getElementById('wakewordList');
        const wakewordWsUrlInput = document.getElementById('wakewordWsUrl');
        if (!wakewordEnabledInput || !wakewordListInput) {
            return;
        }

        const wakeWords = wakewordListInput.value
            .split(/\r?\n/u)
            .map(item => item.trim())
            .filter(Boolean)
            .filter((item, index, items) => items.indexOf(item) === index);

        const payload = {
            enabled: wakewordEnabledInput.value !== 'false',
            wakeWords,
        };

        if (payload.enabled && payload.wakeWords.length === 0) {
            this.addChatMessage('启用唤醒词时，至少需要填写一个唤醒词。', false);
            return;
        }

        const applyWakewordBtn = document.getElementById('applyWakewordBtn');
        if (applyWakewordBtn) {
            applyWakewordBtn.disabled = true;
            applyWakewordBtn.textContent = '应用中...';
        }

        try {
            // 保存地址到 localStorage
            if (wakewordWsUrlInput && wakewordWsUrlInput.value.trim()) {
                localStorage.setItem('xz_tester_wakewordWsUrl', wakewordWsUrlInput.value.trim());
            }

            // 比较新地址和当前连接地址
            const newWsUrl = localStorage.getItem('xz_tester_wakewordWsUrl');
            const currentWsUrl = getWakewordBridgeUrl();
            const urlChanged = newWsUrl !== currentWsUrl;

            if (urlChanged) {
                // 地址变了，先确认
                const shouldRestart = window.confirm('地址已变更，是否继续？（将断开旧连接并重新连接）');
                if (!shouldRestart) {
                    // 恢复 localStorage 里的旧地址
                    localStorage.setItem('xz_tester_wakewordWsUrl', currentWsUrl);
                    this.addChatMessage('已取消地址变更。', false);
                    return;
                }

                // 断开旧连接
                stopWakewordBridgeListener();

                // 启动新连接（自动用新地址）
                startWakewordBridgeListener();

                // 等 bridge_connected
                await new Promise((resolve, reject) => {
                    const timeout = setTimeout(() => {
                        reject(new Error('连接新服务器超时'));
                    }, 5000);

                    onNextBridgeConnected(() => {
                        clearTimeout(timeout);
                        resolve();
                    });
                });

                // 发配置和重启
                await requestWakewordBridge('set_wakeword_config', payload);
                this.applyWakewordConfig(payload);
                await requestWakewordBridge('restart_wakeword_service');
                this.addChatMessage('唤醒词配置已保存，唤醒词服务正在重启。', false);
            } else {
                // 地址没变：直接在当前连接操作
                const response = await requestWakewordBridge('set_wakeword_config', payload);
                this.applyWakewordConfig(response.payload || payload);

                const shouldRestart = window.confirm('唤醒词已保存。是否现在重启唤醒词服务以立即生效？');
                if (!shouldRestart) {
                    this.addChatMessage('唤醒词配置已保存，可稍后手动重启服务后生效。', false);
                    return;
                }

                await requestWakewordBridge('restart_wakeword_service');
                this.addChatMessage('唤醒词配置已保存，唤醒词服务正在重启。', false);
            }
        } catch (error) {
            this.addChatMessage(`应用唤醒词失败: ${error.message}`, false);
        } finally {
            if (applyWakewordBtn) {
                applyWakewordBtn.disabled = false;
                applyWakewordBtn.textContent = '应用唤醒词';
            }
        }
    }

    // Start AI chat session after connection
    async startAIChatSession() {
        if (isRemoteMode()) {
            markRemoteSessionJoined();
        } else if (!isMuseMode()) {
            this.addChatMessage('连接成功，开始聊天吧~😊', false);
        }
        // 电脑端 host 默认用本机麦/摄像头；手机 remote 接入后由 __MUSE_PAUSE_MIC__ 暂停本机麦。
        // 以前用 !__MUSE_MIC_STREAM__ 误判成「等手机」，导致每次打开都卡在等待提示且不启本地麦。
        if (!window.microphoneAvailable && !window.__MUSE_MIC_STREAM__) {
            if (!isMuseMode()) {
                if (window.isHttpNonLocalhost) {
                    this.addChatMessage('⚠️ 当前由于是http访问，无法录音，只能用文字交互', false);
                } else {
                    this.addChatMessage('⚠️ 麦克风不可用，请检查权限设置，只能用文字交互', false);
                }
            }
        }
        if (window.microphoneAvailable || window.__MUSE_MIC_STREAM__ || isMuseMode()) {
            const { getAudioRecorder } = await import('../core/audio/recorder.js?v=0264');
            const { getWebSocketHandler } = await import('../core/network/websocket.js?v=0271');
            const recorder = getAudioRecorder();
            const wsHandler = getWebSocketHandler();
            if (!wsHandler?.websocket || wsHandler.websocket.readyState !== WebSocket.OPEN) {
                log('WS 未就绪，暂缓启动录音', 'warning');
            } else {
                recorder.setWebSocket(wsHandler.websocket);
                if (recorder.isRecording) {
                    log('录音已在进行，已换绑 WebSocket', 'info');
                } else if (!recorder._starting) {
                    let ok = false;
                    try { ok = await recorder.start(); }
                    catch (e) { log('录音启动异常: ' + (e && e.message), 'error'); }
                    if (ok) {
                        window.microphoneAvailable = true;
                    } else if (isMuseMode()) {
                        // 首次可能因 AudioContext 未解锁失败；等用户点一下页面后 museUnlockAudio 会重试
                        log('麦克风启动失败，等待用户手势后重试', 'warning');
                    } else {
                        this.addChatMessage('⚠️ 麦克风音频链路启动失败，请重新授权后重试', false);
                    }
                }
            }
        }
        // EV 电脑端默认只开麦；摄像头按需（或用户显式开启自动摄像头），避免每次进终端弹摄像头权限
        const autoCamera = isRemoteMode()
            || (!isMuseMode())
            || localStorage.getItem('ev_auto_camera') === '1';
        if (autoCamera && window.cameraAvailable && typeof window.startCamera === 'function') {
            window.startCamera().then(success => {
                if (!success && !isRemoteMode() && !isMuseMode()) {
                    this.addChatMessage('⚠️ 摄像头启动失败，可能被浏览器拒绝', false);
                } else if (!success && isMuseMode()) {
                    this.addChatMessage('⚠️ 摄像头启动失败，请检查摄像头权限', false);
                }
            }).catch(error => {
                log(`启动摄像头异常: ${error.message}`, 'error');
            });
        } else if (isMuseMode() && !isRemoteMode()) {
            log('EV 终端：跳过自动开摄像头（需要时再开，或设置 localStorage ev_auto_camera=1）', 'info');
        }
    }

    // Handle connect button click
    async handleConnect() {
        if (isPreviewMode()) {
            log('预览模式：跳过浏览器语音连接（本机麦克风链路独立运行）', 'info');
            return;
        }
        const wsHandler = getWebSocketHandler();
        if (this.isConnecting || (wsHandler && wsHandler.isConnected() && !isRemoteMode())) {
            log('连接已存在或正在进行，忽略本次拨号请求', 'info');
            return;
        }
        if (isRemoteMode() && window.__MUSE_REMOTE_UI__?.btn?.classList.contains('live')) {
            log('远程会话已并入，忽略重复连接', 'info');
            return;
        }

        this.isConnecting = true;
        console.log('handleConnect called');

        try {
            if (isRemoteMode()) {
                // 远程模式：不切换隐藏的配置页，直接连
            } else {
                this.switchTab('device');
                await new Promise(resolve => setTimeout(resolve, 50));
            }

            let otaUrl = document.getElementById('otaUrl')?.value?.trim() || '';
            if (!otaUrl) {
                otaUrl = localStorage.getItem('xz_tester_otaUrl') || `${location.origin}/xiaozhi/ota/`;
                const otaUrlInput = document.getElementById('otaUrl');
                if (otaUrlInput) otaUrlInput.value = otaUrl;
            }

            if (!otaUrl) {
                this.addChatMessage('请输入OTA服务器地址', false);
                return;
            }

            console.log('otaUrl value:', otaUrl);

            // Update dial button state to connecting
            const dialBtn = document.getElementById('dialBtn');
            if (dialBtn) {
                dialBtn.classList.add('dial-active');
                dialBtn.querySelector('.btn-text').textContent = '连接中...';
                dialBtn.disabled = true;
            }

            // Show connecting message
            this.addChatMessage('正在连接服务器...', false);

            const chatIpt = document.getElementById('chatIpt');
            if (chatIpt) {
                chatIpt.style.display = 'flex';
            }

            // Get WebSocket handler instance
            // Register connection state callback BEFORE connecting
            wsHandler.onConnectionStateChange = (isConnected) => {
                this.updateConnectionUI(isConnected);
                this.updateDialButton(isConnected);
            };

            // Register chat message callback BEFORE connecting
            wsHandler.onChatMessage = (text, isUser) => {
                this.addChatMessage(text, isUser);
            };

            // Register record button state callback BEFORE connecting
            wsHandler.onRecordButtonStateChange = (isRecording) => {
                const recordBtn = document.getElementById('recordBtn');
                if (recordBtn) {
                    if (isRecording) {
                        recordBtn.classList.add('recording');
                        recordBtn.querySelector('.btn-text').textContent = '录音中';
                    } else {
                        recordBtn.classList.remove('recording');
                        recordBtn.querySelector('.btn-text').textContent = '录音';
                    }
                }
            };

            if (isRemoteMode()) {
                window.__MUSE_REMOTE_UI__?.log('正在连接服务器…');
            }

            const isConnected = await wsHandler.connect();

            if (isConnected) {
                if (isRemoteMode()) {
                    window.__MUSE_REMOTE_UI__?.log('等待并入电脑会话…');
                    const joined = await wsHandler.waitForHandshake(35000);
                    if (!joined) {
                        throw new Error('未能并入电脑会话，请确认 EV 核心服务在线');
                    }
                    markRemoteSessionJoined();
                    void this.startAIChatSession();
                }
                if (!isRemoteMode() && !window.__MUSE_MIC_STREAM__) {
                    const { checkMicrophoneAvailability } = await import('../core/audio/recorder.js?v=0264');
                    const micAvailable = await checkMicrophoneAvailability();
                    if (!micAvailable) {
                        const isHttp = window.isHttpNonLocalhost;
                        if (isHttp) {
                            this.addChatMessage('⚠️ 当前由于是http访问，无法录音，只能用文字交互', false);
                        }
                        window.microphoneAvailable = false;
                    }
                } else if (window.__MUSE_MIC_STREAM__) {
                    window.microphoneAvailable = true;
                }

                // Update dial button state
                const dialBtn = document.getElementById('dialBtn');
                if (dialBtn) {
                    if (!this.dialBtnDisabled) {
                        dialBtn.disabled = false;
                    }
                    dialBtn.querySelector('.btn-text').textContent = '挂断';
                    dialBtn.classList.add('dial-active');
                }

                this.hideModal('settingsModal');
            } else {
                throw new Error('OTA连接失败');
            }
        } catch (error) {
            console.error('Connection error details:', {
                message: error.message,
                stack: error.stack,
                name: error.name
            });

            // Show error message
            const errorMessage = error.message.includes('Cannot set properties of null')
                ? '连接失败：请检查设备连接'
                : `连接失败: ${error.message}`;

            this.addChatMessage(errorMessage, false);

            // Restore dial button state
            const dialBtn = document.getElementById('dialBtn');
            if (dialBtn) {
                if (!this.dialBtnDisabled) {
                    dialBtn.disabled = false;
                }
                dialBtn.querySelector('.btn-text').textContent = '拨号';
                dialBtn.classList.remove('dial-active');
                console.log('Dial button state restored successfully');
            }
        } finally {
            this.isConnecting = false;
        }
    }

    async triggerWakewordDial(wakeWord = '唤醒词') {
        const wsHandler = getWebSocketHandler();
        const now = Date.now();

        if (wsHandler && wsHandler.isConnected()) {
            log('页面已连接，忽略自动拨号', 'info');
            return false;
        }

        if (this.isConnecting || this.dialBtnDisabled) {
            log('页面正在连接中，忽略重复唤醒', 'info');
            return false;
        }

        if (now - this.lastWakewordDialTime < 3000) {
            log('唤醒触发过于频繁，忽略本次自动拨号', 'warning');
            return false;
        }

        this.lastWakewordDialTime = now;
        this.addChatMessage(`检测到唤醒词“${wakeWord}”，准备连接服务器...`, false);
        await this.handleConnect();
        return true;
    }

    // Add MCP tool
    addMCPTool() {
        const mcpToolsList = document.getElementById('mcpToolsList');
        if (!mcpToolsList) return;

        const toolId = `mcp-tool-${Date.now()}`;
        const toolDiv = document.createElement('div');
        toolDiv.className = 'properties-container';
        toolDiv.innerHTML = `
            <div class="property-item">
                <input type="text" placeholder="工具名称" value="新工具">
                <input type="text" placeholder="工具描述" value="工具描述">
                <button class="remove-property" onclick="uiController.removeMCPTool('${toolId}')">删除</button>
            </div>
        `;

        mcpToolsList.appendChild(toolDiv);
    }

    // Remove MCP tool
    removeMCPTool(toolId) {
        const toolElement = document.getElementById(toolId);
        if (toolElement) {
            toolElement.remove();
        }
    }

    // Update audio statistics display
    updateAudioStats() {
        const audioPlayer = getAudioPlayer();
        if (!audioPlayer) return;

        const stats = audioPlayer.getAudioStats();
        // Here can add audio statistics UI update logic
    }

    // Start audio statistics monitor
    startAudioStatsMonitor() {
        // Update audio statistics every 100ms
        this.audioStatsTimer = setInterval(() => {
            this.updateAudioStats();
        }, 100);
    }

    // Stop audio statistics monitor
    stopAudioStatsMonitor() {
        if (this.audioStatsTimer) {
            clearInterval(this.audioStatsTimer);
            this.audioStatsTimer = null;
        }
    }

    // Draw audio visualizer waveform
    drawVisualizer(dataArray) {
        if (!this.visualizerContext || !this.visualizerCanvas) return;

        this.visualizerContext.fillStyle = '#fafafa';
        this.visualizerContext.fillRect(0, 0, this.visualizerCanvas.width, this.visualizerCanvas.height);

        const barWidth = (this.visualizerCanvas.width / dataArray.length) * 2.5;
        let barHeight;
        let x = 0;

        for (let i = 0; i < dataArray.length; i++) {
            barHeight = dataArray[i] / 2;

            // Create gradient color: from purple to blue to green
            const gradient = this.visualizerContext.createLinearGradient(0, 0, 0, this.visualizerCanvas.height);
            gradient.addColorStop(0, '#8e44ad');
            gradient.addColorStop(0.5, '#3498db');
            gradient.addColorStop(1, '#1abc9c');

            this.visualizerContext.fillStyle = gradient;
            this.visualizerContext.fillRect(x, this.visualizerCanvas.height - barHeight, barWidth, barHeight);
            x += barWidth + 1;
        }
    }

    // Update session status UI
    updateSessionStatus(isSpeaking) {
        // Here can add session status UI update logic
        // For example: update Live2D model's mouth movement status
    }

    // Update session emotion
    updateSessionEmotion(emoji) {
        // Here can add emotion update logic
        // For example: display emoji in status indicator
    }
}

// Create singleton instance
export const uiController = new UIController();

// Export class for module usage
export { UIController };
