// 智能体形象模式：默认声波可视化，Live2D 为可选展示
export const VISUALIZER_AVATAR = 'visualizer';

export function isLive2dAvatarName(name) {
    const n = String(name || '').trim();
    return n && n !== VISUALIZER_AVATAR && n !== 'default' && n !== 'sound_visualizer';
}

export function resolveAvatarConfig() {
    const name = (localStorage.getItem('xz_tester_avatar') || VISUALIZER_AVATAR).trim();
    const model = (localStorage.getItem('xz_tester_avatarModel') || '').trim();
    if (!isLive2dAvatarName(name)) {
        return { mode: 'visualizer', name: VISUALIZER_AVATAR, model: '' };
    }
    return { mode: 'live2d', name, model };
}
