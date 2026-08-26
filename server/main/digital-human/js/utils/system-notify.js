/** 明确的连接/权限状态文案：走 status/notify，永不进对话。 */
const SYSTEM_CHAT = [
    /^⚠️/,
    /^🎤/,
    /^📱/,
    /^正在连接/,
    /^连接成功/,
    /^连接失败/,
    /^Disconnected/i,
    /^诊断:/,
    /^请输入OTA/,
    /^尚未连接/,
    /^检测到唤醒词/,
    /^未知消息类型/,
    /^开始聊天/,
    /WebSocket错误/,
    /^语音核心尚未连接/,
    /^请先点「连接/,
    /^请先绑定/,
    /^请允许麦克风/,
    /^麦克风启动失败/,
    /^麦克风不可用/,
    /^麦克风音频链路启动失败/,
    /^摄像头启动失败/,
    /^当前由于是http访问/,
];

export function isSystemChatMessage(text) {
    const s = String(text || '').trim();
    if (!s) return true;
    return SYSTEM_CHAT.some(re => re.test(s));
}

export function notifyLevel(text) {
    return /失败|不可用|错误|拒绝|异常|超时/i.test(String(text || '')) ? 'warn' : 'info';
}
