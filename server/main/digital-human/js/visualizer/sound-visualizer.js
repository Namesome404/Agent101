import { log } from '../utils/logger.js?v=0205';

/**
 * 默认智能体形象：中性竖条声波，无涟漪/光晕特效。
 */
export class SoundVisualizer {
    constructor(canvasId = 'sound-visualizer-stage') {
        this.canvas = document.getElementById(canvasId);
        this.ctx = this.canvas ? this.canvas.getContext('2d') : null;
        this.analyser = null;
        this.dataArray = null;
        this.speaking = false;
        this.externalLevel = 0;
        this.phase = 0;
        this._raf = null;
        this._resize = () => this.resize();
        this.barCount = 11;
        this.heights = new Array(this.barCount).fill(0);
    }

    async start() {
        if (!this.canvas || !this.ctx) {
            throw new Error('声波可视化画布未找到');
        }
        this.canvas.classList.remove('hidden');
        this.resize();
        window.addEventListener('resize', this._resize);
        this.loop();
        log('声波可视化已启动', 'success');
    }

    destroy() {
        if (this._raf) cancelAnimationFrame(this._raf);
        this._raf = null;
        window.removeEventListener('resize', this._resize);
    }

    resize() {
        if (!this.canvas) return;
        const dpr = window.devicePixelRatio || 1;
        const w = window.innerWidth;
        const h = window.innerHeight;
        this.canvas.width = Math.floor(w * dpr);
        this.canvas.height = Math.floor(h * dpr);
        this.canvas.style.width = w + 'px';
        this.canvas.style.height = h + 'px';
        this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        this.w = w;
        this.h = h;
    }

    connectToAudioPlayer() {
        try {
            const streamingContext = window.chatApp?.audioPlayer?.streamingContext;
            const analyser = streamingContext?.getAnalyser?.();
            if (!analyser) return false;
            this.analyser = analyser;
            this.dataArray = new Uint8Array(analyser.frequencyBinCount);
            return true;
        } catch (e) {
            return false;
        }
    }

    setSpeaking(active) {
        this.speaking = !!active;
        if (this.speaking) this.connectToAudioPlayer();
        if (!this.speaking) this.externalLevel = 0;
    }

    /** 本机 TTS 旁路电平 0~1（预览模式无浏览器 AudioContext 时用） */
    setExternalLevel(level) {
        const n = Number(level);
        this.externalLevel = Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : 0;
        if (this.externalLevel > 0.03) this.speaking = true;
    }

    loop() {
        this._raf = requestAnimationFrame(() => this.loop());
        this.phase += 0.016;
        this.draw();
    }

    sampleBand(index) {
        if (!this.dataArray) return 0;
        const slice = Math.max(1, Math.floor(this.dataArray.length / this.barCount));
        const start = index * slice;
        let sum = 0;
        for (let i = start; i < start + slice && i < this.dataArray.length; i++) {
            sum += this.dataArray[i];
        }
        return sum / (slice * 255);
    }

    draw() {
        const ctx = this.ctx;
        const w = this.w;
        const h = this.h;
        if (!ctx || !w || !h) return;

        ctx.clearRect(0, 0, w, h);

        const cx = w * 0.5;
        const cy = h * 0.47;
        const barW = 4;
        const gap = 10;
        const minH = 18;
        const maxH = Math.min(w, h) * 0.22;
        const totalW = this.barCount * barW + (this.barCount - 1) * gap;
        const startX = cx - totalW / 2;

        const hasAnalyser = !!(this.speaking && this.analyser && this.dataArray);
        if (hasAnalyser) {
            this.analyser.getByteFrequencyData(this.dataArray);
        }
        const useExternal = this.speaking && !hasAnalyser && this.externalLevel > 0.01;

        let energy = 0;
        for (let i = 0; i < this.barCount; i++) {
            let target = minH;
            if (hasAnalyser) {
                const band = this.sampleBand(i);
                energy += band;
                target = minH + band * (maxH - minH) * 1.2;
            } else if (useExternal) {
                const jitter = 0.55 + 0.45 * Math.sin(this.phase * 7.2 + i * 0.9);
                const band = this.externalLevel * jitter;
                energy += band;
                target = minH + band * (maxH - minH) * 1.35;
            } else {
                const wave = Math.sin(this.phase * 1.1 + i * 0.55) * 0.5 + 0.5;
                target = minH + wave * 28;
            }
            this.heights[i] += (target - this.heights[i]) * (this.speaking ? 0.34 : 0.08);
        }
        energy /= this.barCount;

        for (let i = 0; i < this.barCount; i++) {
            const barH = this.heights[i];
            const x = startX + i * (barW + gap);
            const y = cy - barH * 0.5;
            const dist = Math.abs(i - (this.barCount - 1) / 2) / ((this.barCount - 1) / 2);
            const alpha = this.speaking
                ? 0.38 + (1 - dist * 0.35) * (0.35 + energy * 0.4)
                : 0.14 + (1 - dist * 0.4) * 0.1;

            ctx.fillStyle = `rgba(214, 210, 202, ${alpha})`;
            this.roundBar(ctx, x, y, barW, barH, barW);
            ctx.fill();
        }
    }

    roundBar(ctx, x, y, w, h, r) {
        const radius = Math.min(r, h / 2, w / 2);
        ctx.beginPath();
        ctx.moveTo(x + radius, y);
        ctx.lineTo(x + w - radius, y);
        ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
        ctx.lineTo(x + w, y + h - radius);
        ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
        ctx.lineTo(x + radius, y + h);
        ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
        ctx.lineTo(x, y + radius);
        ctx.quadraticCurveTo(x, y, x + radius, y);
        ctx.closePath();
    }
}
