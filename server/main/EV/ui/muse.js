// Muse Console SPA
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (t, cls, html) => { const e = document.createElement(t); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const esc = s => (s == null ? '' : String(s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
let BOOT = {};
/** 浏览器终端只做形象预览 + 设置/文字旁路；语音始终由本机麦克风链路处理 */
const USE_BROWSER_VOICE = false;

function parseRoute() {
  const h = location.hash.replace(/^#\//, '') || 'terminal';
  const [seg, id, sub] = h.split('/');
  return { seg, id, sub };
}

function isMobileClient() {
  return /iPhone|iPad|iPod|Android/i.test(navigator.userAgent)
    || (navigator.maxTouchPoints > 1 && window.innerWidth < 900);
}

function syncRouteClasses() {
  const { seg, sub } = parseRoute();
  const remote = seg === 'remote';
  const immersive = seg === 'terminal' || remote;
  const setup = seg === 'terminal' && sub === 'setup';
  document.body.classList.toggle('route-terminal', immersive && !setup && !remote);
  document.body.classList.toggle('route-remote', remote);
  document.body.classList.toggle('route-setup', setup);
  document.body.classList.toggle('route-config', ['agents', 'settings'].includes(seg));
  document.documentElement.classList.toggle('pre-terminal', (immersive && !setup) || remote);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/json')) { const j = await r.json(); if (!r.ok) throw new Error(j.error || j.detail || r.status); return j; }
  if (!r.ok) throw new Error(await r.text()); return r;
}
function toast(msg) { let t = $('#toast'); if (!t) { t = el('div', 'toast'); t.id = 'toast'; document.body.appendChild(t); } t.textContent = msg; t.classList.add('show'); clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('show'), 2200); }

/** Safari：父页面手势才能解锁 iframe 内 AudioContext，否则麦不上行、TTS 无声 */
function unlockAgentAudio() {
  const frame = getEngineFrame?.() || document.getElementById('agentEngineFrame');
  try {
    if (typeof frame?.contentWindow?.museUnlockAudio === 'function') {
      void frame.contentWindow.museUnlockAudio();
    } else {
      frame?.contentWindow?.postMessage({ source: 'muse-parent', type: 'unlockAudio' }, location.origin);
    }
  } catch (_) { /* ignore */ }
}

/** 从设置/设备页返回：WS 可能还在，但麦流/AudioContext 已被挂起或探测掐断 */
function resumeVoiceSession(ui) {
  // 浏览器不再占麦；本机语音链路独立运行，无需在终端页拉起 WS 录音
  if (!USE_BROWSER_VOICE) {
    if (ui) {
      ui.parked = false;
      ui.active = false;
      ui.pendingStart = false;
      ui.previewOnly = true;
    }
    $('.agent-stage')?.classList.remove('session-live');
    return;
  }
  const frame = (ui && ui.frame) || getEngineFrame();
  if (!frame) return;
  if (ui) ui.parked = false;
  injectHostMediaIntoFrame(frame);
  unlockAgentAudio();
  try {
    const w = frame.contentWindow;
    if (!w) return;
    let connected = false;
    try { connected = !!w.getWebSocketHandler?.()?.isConnected?.(); } catch (_) {}
    if (!connected && (ui?.active || ui?.pendingStart)) {
      if (typeof w.museStart === 'function') void w.museStart();
      else w.postMessage({ source: 'muse-parent', type: 'start' }, location.origin);
    } else if (connected) {
      w.postMessage({ source: 'muse-parent', type: 'unlockAudio', force: true }, location.origin);
      if (typeof w.museUnlockAudio === 'function') void w.museUnlockAudio(true);
    }
  } catch (_) { /* ignore */ }
}
function wireAgentAudioUnlock() {
  if (window.__EV_AUDIO_UNLOCK_WIRED__) return;
  window.__EV_AUDIO_UNLOCK_WIRED__ = true;
  const bump = () => unlockAgentAudio();
  ['pointerdown', 'keydown', 'touchstart'].forEach(ev => {
    window.addEventListener(ev, bump, { capture: true, passive: true });
  });
}

function showAgentNotify(text, level = 'warn') {
  const bar = $('#agentNotify');
  const label = $('#agentNotifyText');
  if (!bar || !label) return;
  let msg = String(text || '').trim();
  if (/麦克风|扬声器|摄像头/.test(msg) && !/设置/.test(msg)) {
    msg += ' · 可在 ⚙ 设置 → 设备 中更换音频输入/输出';
  }
  label.textContent = msg;
  if (/正在连接|连接中|请稍候|自动连接|已连接|连接成功|开始聊天/i.test(msg)) return;
  bar.classList.remove('hidden', 'info', 'warn');
  bar.classList.add(level === 'info' || level === 'ok' ? 'info' : 'warn');
  clearTimeout(showAgentNotify._t);
  if (level === 'ok') {
    showAgentNotify._t = setTimeout(() => hideAgentNotify(), 1600);
  }
}

function hideAgentNotify() {
  const bar = $('#agentNotify');
  if (!bar) return;
  bar.classList.add('hidden');
  const label = $('#agentNotifyText');
  if (label) label.textContent = '';
}

// ---------- shell ----------
function mountShell() {
  const app = $('#app'); app.innerHTML = '';
  app.appendChild($('#tpl-shell').content.cloneNode(true));
  $('#nav').addEventListener('click', e => { const a = e.target.closest('[data-route]'); if (a) { location.hash = '#/' + a.dataset.route; $('#sidebar').classList.remove('open'); } });
  $('#themeBtn').onclick = toggleTheme;
  $('#menuBtn').onclick = () => $('#sidebar').classList.toggle('open');
  pollStatus(); if (!mountShell._poll) mountShell._poll = setInterval(pollStatus, 6000);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next); localStorage.setItem('muse-theme', next);
}
function setNav(route) { $$('.nav a').forEach(a => a.classList.toggle('active', a.dataset.route === route)); }
function setCrumb(parts) { const c = $('#crumb'); if (c) c.innerHTML = parts.map((p, i) => i === parts.length - 1 ? `<b>${esc(p)}</b>` : esc(p)).join(' <span style="opacity:.4">/</span> '); }
async function pollStatus() {
  try {
    const s = await api('/api/status');
    const cl = $('#coreLed'), ml = $('#museLed'), ca = $('#c-agents');
    if (cl) { cl.className = 'led ' + (s.core_up ? 'ok' : 'err'); cl.textContent = '核心 ' + (s.core_up ? 'ONLINE' : 'OFFLINE'); }
    if (ml) { ml.className = 'led ok'; ml.textContent = 'EV ONLINE'; }
    if (ca) ca.textContent = s.agents;
  } catch (e) { const ml = $('#museLed'); if (ml) { ml.className = 'led err'; ml.textContent = 'EV OFFLINE'; } }
}

// ---------- router ----------
async function route() {
  const { seg, id, sub } = parseRoute();
  syncRouteClasses();
  if (!$('#view')) mountShell();
  const v = $('#view');
  const navRoute = seg === 'remote' ? 'terminal' : (seg === 'terminal' ? (sub === 'setup' ? 'agents' : 'terminal') : seg);
  setNav(navRoute);
  v.classList.toggle('wide', seg === 'terminal' || seg === 'remote' || (seg === 'agents' && !!id && sub === 'avatar'));
  if (seg === 'remote') setCrumb(['iPhone', '麦克风']);
  else if (seg === 'terminal') setCrumb(sub === 'setup' ? ['智能体', '设置'] : ['EV', '智能体']);
  else if (seg === 'workshop') setCrumb(['设备工坊', 'ESP-Claw']);
  try {
    // Must await: bare `return renderX()` lets async rejections escape try/catch,
    // leaving a half-wired page that looks fine but clicks do nothing.
    const leavingVoice = !(seg === 'terminal' && (!sub || sub === ''));
    // setup / 设备总览等会换掉 #view，先把语音 iframe 寄存在 park，保留已授权麦流与会话
    if (leavingVoice && (seg !== 'terminal' || sub === 'setup')) parkVoiceEngine();
    if (seg === 'workshop') await renderWorkshop(v);
    else if (seg === 'remote') await renderRemoteSurface(v, id);
    else if (seg === 'terminal' && !id && isMobileClient()) {
      location.replace('/remote');
    } else if (seg === 'terminal' && id && sub === 'setup') {
      await renderAgentEditor(v, id, { immersive: true });
    } else if (seg === 'terminal') await renderAgentSurface(v, id);
    else if (seg === 'agents' && id) { location.replace('#/terminal/' + id + '/setup'); }
    else if (seg === 'agents') await renderAgents(v);
    else if (seg === 'settings') await renderSettings(v);
    else location.hash = '#/terminal';
  } catch (e) {
    console.error('[EV route]', e);
    setCrumb(['出错']);
    v.innerHTML = `<div class="empty"><div class="big">出错了</div><div class="mono">${esc(e.message || String(e))}</div><p class="hint">刷新页面重试；若反复出现请检查浏览器控制台。</p></div>`;
  }
}

// ---------- terminal / operational cockpit ----------
function pickAgent(agents, id) {
  if (!agents.length) return null;
  return agents.find(a => String(a.id) === String(id)) || agents[0];
}
function moduleChip(modules, key) {
  const selected = (modules[key] || {}).selected;
  return `<div class="sys-chip"><span>${esc(key)}</span><b>${esc(selected || '未配置')}</b></div>`;
}
function timeAgo(ts) {
  if (!ts) return '未连接';
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (sec < 60) return sec + ' 秒前';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
  return Math.floor(sec / 3600) + ' 小时前';
}
function intelligenceRows(agent, devices, status) {
  const mods = agent.modules || {};
  const mine = devices.filter(d => String(d.agent_id) === String(agent.id));
  const missing = ['ASR', 'LLM', 'TTS', 'Intent'].filter(k => !(mods[k] || {}).selected);
  const rows = [
    ['Core', status.core_up ? '核心服务在线，WebSocket 可接入。' : '核心服务离线，设备无法建立实时会话。', status.core_up ? 'ok' : 'err'],
    ['Agent', `${agent.name} 管理 ${mine.length} 台设备。`, 'ok'],
    ['Voice', missing.length ? `缺少 ${missing.join(' / ')} 配置。` : '语音链路配置完整。', missing.length ? 'warn' : 'ok'],
    ['窗口 MCP', '语音中可随时打开/移动/调整浮动面板（muse.ui.* 工具）。', 'ok']
  ];
  return rows.map(r => `<div class="intel-row ${r[2]}"><span>${r[0]}</span><p>${esc(r[1])}</p></div>`).join('');
}

/**
 * 语音引擎 iframe 永久挂在 body 上，只改定位/显隐，绝不在 DOM 里搬移。
 * 搬移 iframe 会触发浏览器重载 → WebSocket 断开 → 出现「正在自动连接」假提示。
 */
function getEngineHost() {
  let host = document.getElementById('ev-engine-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'ev-engine-host';
    // 本机语音不占浏览器麦；iframe 仅预览形象，无需 microphone 权限
    host.innerHTML = '<iframe id="agentEngineFrame" class="agent-engine" title="EV voice agent" allow="autoplay; clipboard-write"></iframe>';
    document.body.appendChild(host);
  }
  return host;
}

function getEngineFrame() {
  return getEngineHost().querySelector('#agentEngineFrame');
}

function hideVoiceEngine() {
  const host = document.getElementById('ev-engine-host');
  if (!host) return;
  // 只隐藏，不把宽高清零——Safari 对 0 尺寸 iframe 会掐断 WS/麦
  host.classList.add('is-parked');
  host.setAttribute('aria-hidden', 'true');
  const ui = window.MUSE_SURFACE_UI;
  if (ui) ui.parked = true;
  // 不中断对话旁路轮询：从设置返回后侧栏仍能跟上本机语音
}

function showVoiceEngineOverStage(stage) {
  const host = getEngineHost();
  const frame = getEngineFrame();
  if (!stage || !host || !frame) return frame;
  const place = () => {
    const r = stage.getBoundingClientRect();
    host.style.top = r.top + 'px';
    host.style.left = r.left + 'px';
    host.style.width = Math.max(0, r.width) + 'px';
    host.style.height = Math.max(0, r.height) + 'px';
  };
  place();
  host.classList.remove('is-parked');
  host.setAttribute('aria-hidden', 'false');
  if (window._evEnginePlace) {
    window.removeEventListener('resize', window._evEnginePlace);
    cancelAnimationFrame(window._evEngineRaf);
  }
  window._evEnginePlace = () => {
    cancelAnimationFrame(window._evEngineRaf);
    window._evEngineRaf = requestAnimationFrame(place);
  };
  window.addEventListener('resize', window._evEnginePlace);
  return frame;
}

/** 兼容旧调用名：离开语音页时只隐藏，不搬 DOM */
function parkVoiceEngine() {
  hideVoiceEngine();
}

function liveMediaStream(stream) {
  return !!(stream && stream.getAudioTracks && stream.getAudioTracks().some(t => t.readyState === 'live'));
}

/** 仅浏览器语音模式才会申请麦；本机语音链路永不弹窗 */
async function ensureHostMicStream() {
  if (!USE_BROWSER_VOICE) {
    throw new Error('本机语音模式不使用浏览器麦克风');
  }
  if (liveMediaStream(window.__EV_SHARED_MIC__)) return window.__EV_SHARED_MIC__;
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('当前浏览器不支持麦克风');
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  });
  window.__EV_SHARED_MIC__ = stream;
  return stream;
}

function injectHostMediaIntoFrame(frame) {
  try {
    const w = frame && frame.contentWindow;
    if (!w) return;
    // 只告诉 iframe「父页已拿到麦权限」；真正录音必须在 iframe 内 getUserMedia。
    // 把父页 MediaStream 塞进 iframe AudioContext 在 Safari 上经常是静音。
    if (liveMediaStream(window.__EV_SHARED_MIC__)) {
      w.__MUSE_MIC_PERMISSION_OK__ = true;
      w.microphoneAvailable = true;
    }
  } catch (_) { /* ignore */ }
}

function updateLocalVoiceChip(localVoice) {
  const chip = $('#localVoiceChip');
  if (!chip) return;
  const on = !!(localVoice && localVoice.running);
  const listening = !!(localVoice && localVoice.listening);
  const standby = !!(localVoice && localVoice.standby);
  chip.classList.toggle('on', on);
  chip.classList.toggle('off', !on);
  chip.classList.toggle('listening', on && listening);
  chip.classList.toggle('standby', on && standby);
  if (!on) {
    chip.textContent = '本机语音 · 离线';
    chip.title = '本机语音进程未运行或心跳超时；浏览器不会占麦';
    return;
  }
  if (standby) {
    chip.textContent = '本机语音 · 待机中';
    chip.title = '兼容状态；当前版本不使用本地唤醒模型，设备页关闭麦克风即可静音';
  } else if (listening) {
    chip.textContent = '本机语音 · 聆听中';
    chip.title = localVoice.pid
      ? `本机麦克风链路在线（pid ${localVoice.pid}）；终端仅预览，不占浏览器麦`
      : '本机麦克风链路在线；终端仅预览，不占浏览器麦';
  } else {
    chip.textContent = '本机语音 · 在线';
    chip.title = localVoice.pid
      ? `本机麦克风链路在线（pid ${localVoice.pid}）；终端仅预览，不占浏览器麦`
      : '本机麦克风链路在线；终端仅预览，不占浏览器麦';
  }
}

function wireSurfaceChrome(ui, agent, mine, modules) {
  $('#surfaceSend').onclick = () => sendSurfaceText(ui, agent);
  $('#surfaceInput').onkeydown = e => { if (e.key === 'Enter') sendSurfaceText(ui, agent); };
  $$('.surface-tools button').forEach(b => b.onclick = () => handleSurfaceCommand(b.dataset.surfaceQ, ui, agent, mine, modules));
  $('#agentNotifyClose')?.addEventListener('click', () => hideAgentNotify());
}

async function renderAgentSurface(v, id) {
  setCrumb(['EV', '智能体']);
  const [{ agents }, { devices }, status] = await Promise.all([api('/api/agents'), api('/api/devices'), api('/api/status')]);
  if (!agents.length) {
    hideVoiceEngine();
    v.innerHTML = `<div class="terminal-empty"><h1>EV 没有可运行的智能体</h1><a class="btn" href="#/agents">进入设置模式</a></div>`;
    return;
  }
  const agent = pickAgent(agents, id);
  const modules = agent.modules || {};
  const mine = devices.filter(d => String(d.agent_id) === String(agent.id));
  const prev = window.MUSE_SURFACE_UI;
  const frame = getEngineFrame();
  const canReuse = prev
    && frame
    && String(prev.agent?.id) === String(agent.id)
    && String(frame.dataset.agentId || agent.id) === String(agent.id)
    && !prev.abort?.signal?.aborted
    && frame.dataset.booted === '1';

  v.innerHTML = `<div class="agent-surface">
    <section class="agent-stage" id="agentStage">
      <div class="agent-hud top">
        <div class="agent-state agent-state-main">
          <span class="led ${status.core_up ? 'ok' : 'err'}" id="surfaceCore" title="${status.core_up ? '核心在线' : '核心离线'}"></span>
          <span class="voice-chip off" id="localVoiceChip" title="本机语音进程未上报心跳">本机语音 · 离线</span>
          <select id="surfaceAgentPick" aria-label="切换智能体"></select>
          <a class="iconbtn" href="#/terminal/${agent.id}/setup" title="设置">⚙</a>
        </div>
      </div>
      <div class="agent-notify hidden" id="agentNotify" role="status">
        <span id="agentNotifyText"></span>
        <button type="button" class="agent-notify-close" id="agentNotifyClose" aria-label="关闭">×</button>
      </div>
      <div class="window-layer" id="agentWindows"></div>
      <aside class="agent-sidechat">
      <div class="sidechat-log" id="surfaceLog"></div>
      <div class="sidechat-input">
        <input type="text" id="surfaceInput" placeholder="文字试聊；语音请用本机麦克风（设备页）">
        <button class="btn" id="surfaceSend">发送</button>
      </div>
      <div class="surface-tools">
        <button data-surface-q="帮我打开设备面板">设备</button>
        <button data-surface-q="打开模型配置面板">模型</button>
        <button data-surface-q="在右侧打开状态面板">窗口</button>
      </div>
      </aside>
    </section>
  </div>`;

  const stage = $('#agentStage');
  const pick = $('#surfaceAgentPick');
  agents.forEach(a => pick.appendChild(new Option(a.name, a.id)));
  pick.value = agent.id;
  pick.onchange = () => location.hash = '#/terminal/' + pick.value;

  showVoiceEngineOverStage(stage);
  if (USE_BROWSER_VOICE) wireAgentAudioUnlock();

  if (canReuse) {
    prev.parked = false;
    prev.frame = frame;
    prev.agent = agent;
    prev.devices = mine;
    prev.modules = modules;
    const log = $('#surfaceLog');
    if (log && prev.nodes) {
      // 只重挂气泡节点（跳过 id: 索引），避免顺序错乱
      const seen = new Set();
      prev.nodes.forEach((node, key) => {
        if (!node || typeof key !== 'string' || key.startsWith('id:') || seen.has(node)) return;
        seen.add(node);
        if (!node.isConnected) log.appendChild(node);
      });
      requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
    }
    wireSurfaceChrome(prev, agent, mine, modules);
    prev._syncStopped = false;
    if (!prev._conversationTimer) prev.syncConversation?.();
    startAgentEngine(prev); // preview-only：不占麦
    stage.classList.toggle('session-live', !!(USE_BROWSER_VOICE && prev.active));
    return;
  }

  if (prev?.abort) prev.abort.abort();
  // 换智能体：重载 iframe src（这是唯一允许的重建）
  const needReload = String(frame.dataset.agentId || '') !== String(agent.id) || frame.dataset.booted !== '1';
  frame.dataset.agentId = String(agent.id);
  const ui = createSurfaceChat(agent, mine, modules);
  ui.frame = frame;
  ui.previewOnly = !USE_BROWSER_VOICE;
  window.MUSE_SURFACE_UI = ui;
  // 对话监听与 iframe 是否重载无关：每次新建 UI 都必须绑定
  bindEngineMessages(ui);
  if (needReload) {
    frame.dataset.booted = '0';
    await mountAgentEngine(agent.id, ui);
    startAgentEngine(ui);
  } else {
    ui.engineReady = true;
    startAgentEngine(ui);
  }
  wireSurfaceChrome(ui, agent, mine, modules);
}

async function renderRemoteSurface(v, id) {
  const [{ agents }, status] = await Promise.all([api('/api/agents'), api('/api/status')]);
  if (!agents.length) {
    v.innerHTML = `<div class="remote-empty"><h1>请先在电脑端创建智能体</h1></div>`;
    return;
  }
  const agent = pickAgent(agents, id);
  v.innerHTML = `<div class="remote-surface">
    <iframe id="remoteEngineFrame" class="remote-engine" title="EV remote mic/cam" allow="microphone; camera; autoplay; clipboard-write"></iframe>
    <div class="remote-panel">
      <p class="remote-agent">${esc(agent.name)}</p>
      <div class="remote-status" id="remoteStatus">
        <span class="remote-pill" data-k="core"><i class="led ${status.core_up ? 'ok' : 'err'}"></i>核心</span>
        <span class="remote-pill" data-k="mic"><i class="led"></i>麦克风</span>
        <span class="remote-pill" data-k="cam"><i class="led"></i>摄像头</span>
        <span class="remote-pill" data-k="link"><i class="led"></i>会话</span>
      </div>
      <button type="button" class="remote-start" id="remoteStartBtn">连接麦克风与摄像头</button>
      <p class="remote-note">iPhone 仅作语音输入与摄像头，模型/设备配置请在电脑端完成。</p>
      <p class="remote-log" id="remoteLog"></p>
    </div>
  </div>`;

  const ui = { frame: $('#remoteEngineFrame'), engineReady: false, pendingStart: false, active: false, abort: new AbortController() };
  if (window.MUSE_REMOTE_UI?.abort) window.MUSE_REMOTE_UI.abort.abort();
  window.MUSE_REMOTE_UI = ui;

  const setPill = (key, on, err) => {
    const pill = $(`.remote-pill[data-k="${key}"]`);
    if (!pill) return;
    const led = pill.querySelector('.led');
    if (!led) return;
    led.className = 'led ' + (err ? 'err' : on ? 'ok' : '');
  };
  const log = (text) => {
    const n = $('#remoteLog');
    if (n) n.textContent = String(text || '');
  };

  window.addEventListener('message', e => {
    if (e.origin !== location.origin) return;
    const msg = e.data || {};
    if (msg.source !== 'muse-digital-human') return;
    if (msg.type === 'ready') {
      ui.engineReady = true;
      if (ui.pendingStart) {
        ui.pendingStart = false;
        doRemoteConnect(ui);
      }
      return;
    }
    if (msg.type === 'status') {
      if (msg.mic != null) setPill('mic', !!msg.mic);
      if (msg.cam != null) setPill('cam', !!msg.cam);
      if (msg.connected) {
        setPill('link', true);
        ui.active = true;
        $('#remoteStartBtn')?.classList.add('live');
        $('#remoteStartBtn').textContent = '已连接 · 可直接说话';
        log('已连接，对着手机说话即可。');
      }
      if (msg.error) {
        setPill('link', false, true);
        log(msg.error);
      }
    }
    if (msg.type === 'chat' && msg.role === 'assistant') {
      const t = String(msg.text || '').trim();
      if (t && !/正在连接|连接成功|开始聊天/i.test(t)) log(t);
    }
  }, { signal: ui.abort.signal });

  const t = await api('/api/agents/' + agent.id + '/terminal');
  ui.frame.src = t.terminal_url + '&remote=1&v=0258';

  const begin = () => {
    if (!USE_BROWSER_VOICE) {
      log('语音已改为电脑本机麦克风；手机远程麦已关闭。请在电脑设备页使用本机对话。');
      setPill('link', false, true);
      return;
    }
    if (ui.active) return;
    setPill('link', false);
    log('正在请求权限并连接…');
    $('#remoteStartBtn').disabled = true;
    $('#remoteStartBtn').textContent = '连接中…';
    if (ui.engineReady) doRemoteConnect(ui);
    else ui.pendingStart = true;
  };
  $('#remoteStartBtn').onclick = begin;
  if (!USE_BROWSER_VOICE) {
    const btn = $('#remoteStartBtn');
    if (btn) {
      btn.textContent = '请使用电脑本机麦克风';
      btn.disabled = true;
    }
    log('语音始终走电脑本机链路；此页不再占麦。');
  }
}

function doRemoteConnect(ui) {
  try {
    if (typeof ui.frame?.contentWindow?.museStart === 'function') {
      ui.frame.contentWindow.museStart().finally(() => {
        const btn = $('#remoteStartBtn');
        if (btn && !ui.active) {
          btn.disabled = false;
          btn.textContent = '连接麦克风与摄像头';
        }
      });
      return;
    }
  } catch (e) {}
  ui.frame?.contentWindow?.postMessage({ source: 'muse-parent', type: 'start' }, location.origin);
}

function createSurfaceChat(agent, devices, modules) {
  const ui = {
    active: false,
    frame: null,
    mirrored: new Set(),
    seenIds: new Set(),
    windows: new Map(),
    z: 20,
    agent,
    devices,
    modules,
    abort: new AbortController(),
    nodes: new Map(),
  };
  ui.logEl = () => $('#surfaceLog');
  // STT 去标点、DB 带标点：无 id 时用去标点文本去重
  ui.norm = (text) => String(text || '')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[\s\u3000]*[.。!！?？…~～,，、;；:：'"”’)\]]+$/u, '')
    .toLowerCase();
  ui.key = (role, text) => role + ':' + ui.norm(text);
  ui.pulse = () => {
    const surface = $('.agent-surface');
    if (!surface) return;
    surface.classList.add('speaking');
    clearTimeout(ui._pulseTimer);
    ui._pulseTimer = setTimeout(() => surface.classList.remove('speaking'), 900);
  };
  ui.scrollLog = () => {
    const log = ui.logEl();
    if (!log) return;
    requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  };
  /** @param {string} role @param {string} text @param {string|number|null} [id] server message id */
  ui.add = (role, text, id = null) => {
    text = String(text || '').trim();
    if (!text) return;
    if (id != null && id !== '') {
      const sid = String(id);
      if (ui.seenIds.has(sid)) {
        const byId = ui.nodes.get('id:' + sid);
        if (byId) {
          const p = byId.querySelector('p');
          if (p && text.length > String(p.textContent || '').trim().length) p.textContent = text;
        }
        ui.scrollLog();
        return;
      }
      ui.seenIds.add(sid);
    }
    const key = ui.key(role, text);
    const existing = ui.nodes.get(key);
    if (existing) {
      const p = existing.querySelector('p');
      if (p && text.length >= String(p.textContent || '').trim().length) p.textContent = text;
      ui.mirrored.add(key);
      if (id != null && id !== '') ui.nodes.set('id:' + id, existing);
      ui.scrollLog();
      return;
    }
    ui.mirrored.add(key);
    const m = el('div', 'surface-msg ' + role);
    m.innerHTML = `<span>${role === 'user' ? '你' : 'EV'}</span><p>${esc(text)}</p>`;
    ui.nodes.set(key, m);
    if (id != null && id !== '') ui.nodes.set('id:' + id, m);
    const log = ui.logEl();
    if (log) log.appendChild(m);
    ui.scrollLog();
    if (role === 'assistant') ui.pulse();
  };
  ui.lastConversationId = 0;
  ui.liveSeq = 0;
  ui._syncStopped = false;
  ui.addLive = (role, text, turnId = '', final = true) => {
    text = String(text || '').trim();
    if (!text) return;
    const draftKey = turnId ? ('draft:' + turnId) : '';
    if (draftKey && role === 'assistant' && !final) {
      let node = ui.nodes.get(draftKey);
      if (!node) {
        node = el('div', 'surface-msg assistant');
        node.innerHTML = `<span>EV</span><p></p>`;
        ui.nodes.set(draftKey, node);
        const log = ui.logEl();
        if (log) log.appendChild(node);
      }
      const p = node.querySelector('p');
      if (p) p.textContent = text;
      ui.scrollLog();
      ui.pulse();
      return;
    }
    if (draftKey) {
      const draft = ui.nodes.get(draftKey);
      if (draft) {
        try { draft.remove(); } catch (_) {}
        ui.nodes.delete(draftKey);
      }
    }
    ui.add(role, text);
  };
  ui.syncConversation = async () => {
    if (ui._syncStopped) return;
    try {
      // 实时旁路：本机语音增量 + 声波
      const live = await api(`/api/agents/${agent.id}/live?after=${ui.liveSeq || 0}`);
      ui.liveSeq = Math.max(ui.liveSeq || 0, Number(live.seq) || 0);
      updateLocalVoiceChip(live.local_voice);
      for (const ev of live.events || []) {
        if (ev.type === 'utterance') {
          ui.addLive(ev.role, ev.text, ev.turn_id || '', ev.final !== false);
        } else if (ev.type === 'panel' && ev.panel) {
          applyLivePanel(ui, ev.panel);
        } else if (ev.type === 'stage') {
          const frame = ui.frame || getEngineFrame();
          frame?.contentWindow?.postMessage({
            source: 'muse-parent',
            type: 'voice-stage',
            speaking: !!ev.speaking,
            level: Number(ev.level) || 0,
          }, location.origin);
        }
      }
      // 当前电平也推一帧（即使没有新 event）
      if (live.speaking || (live.level || 0) > 0.02) {
        const frame = ui.frame || getEngineFrame();
        frame?.contentWindow?.postMessage({
          source: 'muse-parent',
          type: 'voice-stage',
          speaking: !!live.speaking,
          level: Number(live.level) || 0,
        }, location.origin);
      }
      // DB 兜底同步（带 id 去重）
      const data = await api(`/api/agents/${agent.id}/conversation?after_id=${ui.lastConversationId}&limit=40`);
      for (const message of data.messages || []) {
        ui.lastConversationId = Math.max(ui.lastConversationId, Number(message.id) || 0);
        const role = message.role === 'user' ? 'user' : 'assistant';
        const text = String(message.content || '').trim();
        if (text) ui.add(role, text, message.id);
      }
    } catch (_) {}
    if (!ui._syncStopped) ui._conversationTimer = setTimeout(ui.syncConversation, 280);
  };
  ui.abort.signal.addEventListener('abort', () => {
    ui._syncStopped = true;
    clearTimeout(ui._conversationTimer);
  }, { once: true });
  ui.syncConversation();
  return ui;
}

/** 父页面是对话唯一渲染器：监听 iframe utterance/status，与是否重载引擎无关 */
function bindEngineMessages(ui) {
  const frame = ui.frame || getEngineFrame();
  ui.frame = frame;
  window.addEventListener('message', e => {
    if (e.origin !== location.origin) return;
    const msg = e.data || {};
    if (msg.source !== 'muse-digital-human') return;
    if (msg.type === 'ready') {
      ui.engineReady = true;
      if (frame) frame.dataset.booted = '1';
      hideAgentNotify();
      if (USE_BROWSER_VOICE) {
        injectHostMediaIntoFrame(ui.frame);
        if (ui.pendingStart || ui.active) {
          ui.pendingStart = false;
          doAgentEngineConnect(ui);
        }
      } else {
        ui.pendingStart = false;
        ui.active = false;
        ui.previewOnly = true;
      }
      return;
    }
    if (msg.type === 'status' || msg.type === 'notify') {
      const t = String(msg.text || msg.error || '').trim();
      if (!t) return;
      if (/正在连接|连接中|请稍候|自动连接|已连接|连接成功|开始聊天/i.test(t)) return;
      showAgentNotify(t, msg.level || 'warn');
      return;
    }
    if (msg.type === 'utterance' || msg.type === 'chat') {
      const text = String(msg.text || '').trim();
      const role = msg.role === 'user' ? 'user' : 'assistant';
      if (!text) return;
      ui.add(role, text, msg.id);
      return;
    }
    if (msg.type === 'mcp-tool') {
      handleMuseMcpToolRequest(ui, msg, e.source);
    }
  }, { signal: ui.abort.signal });
}

async function mountAgentEngine(agentId, ui) {
  void api('/api/latency/prewarm', { method: 'POST', body: JSON.stringify({ agent_id: Number(agentId) || 1 }) }).catch(() => {});
  const t = await api('/api/agents/' + agentId + '/terminal');
  ui.terminal = t;
  const frame = getEngineFrame();
  ui.frame = frame;
  frame.onload = () => {
    try {
      const doc = frame.contentDocument;
      if (doc) injectAgentEngineSkin(doc);
      if (USE_BROWSER_VOICE) injectHostMediaIntoFrame(frame);
    } catch (e) {
      ui.add('assistant', '智能体引擎初始化失败：' + e.message);
    }
  };
  const nextSrc = t.terminal_url + '&v=0271&preview=1';
  const abs = new URL(nextSrc, location.origin).href;
  if (frame.src !== abs) {
    frame.src = nextSrc;
  } else {
    ui.engineReady = true;
    frame.dataset.booted = '1';
    if (USE_BROWSER_VOICE) {
      injectHostMediaIntoFrame(frame);
      if (ui.pendingStart || ui.active) {
        ui.pendingStart = false;
        doAgentEngineConnect(ui);
      }
    }
  }
}

function injectAgentEngineSkin(doc) {
  if (doc.getElementById('museAgentSkin')) return;
  const style = doc.createElement('style');
  style.id = 'museAgentSkin';
  style.textContent = `
    .control-bar,.chat-container,.modal,#settingsModal,#mcpToolModal,#mcpPropertyModal{display:none!important}
    .connection-status-top,.model-container,.model-loading{display:none!important}
    .background-container{background:#0a0a0a!important;background-image:none!important}
    .background-overlay{background:transparent!important}
    body{background:#0a0a0a!important}
    #live2d-stage{pointer-events:auto!important}
    #sound-visualizer-stage{pointer-events:none!important;display:block!important}
    #live2d-stage.hidden{display:none!important}
    .camera-container{right:24px!important;top:76px!important;width:240px!important;height:160px!important;border-radius:10px!important}
  `;
  doc.head.appendChild(style);
}

function startAgentEngine(ui) {
  if (!USE_BROWSER_VOICE) {
    ui.active = false;
    ui.pendingStart = false;
    ui.previewOnly = true;
    $('.agent-stage')?.classList.remove('session-live');
    $('.agent-sidechat')?.classList.add('compact');
    return;
  }
  ui.active = true;
  $('.agent-stage')?.classList.add('session-live');
  if (!ui.engineReady) {
    ui.pendingStart = true;
  } else {
    doAgentEngineConnect(ui);
  }
  $('.agent-sidechat')?.classList.add('compact');
}

async function doAgentEngineConnect(ui) {
  if (!USE_BROWSER_VOICE) return;
  if (ui._connecting) return;
  ui._connecting = true;
  try {
    const micTask = ensureHostMicStream().then(s => {
      injectHostMediaIntoFrame(ui.frame);
      return s;
    }).catch(e => {
      console.warn('[EV] mic before connect', e);
      return null;
    });
    await Promise.race([micTask, new Promise(r => setTimeout(r, 1200))]);
    injectHostMediaIntoFrame(ui.frame);
    let called = false;
    try {
      if (typeof ui.frame?.contentWindow?.museStart === 'function') {
        await ui.frame.contentWindow.museStart();
        called = true;
      }
    } catch (e) {
      showAgentNotify('语音连接失败：' + (e.message || e), 'warn');
    }
    if (!called) ui.frame?.contentWindow?.postMessage({ source: 'muse-parent', type: 'start' }, location.origin);
    void micTask.then(() => {
      injectHostMediaIntoFrame(ui.frame);
      unlockAgentAudio();
    });
    unlockAgentAudio();
  } finally {
    ui._connecting = false;
  }
}

async function sendSurfaceText(ui, agent) {
  const input = $('#surfaceInput');
  const q = input.value.trim();
  if (!q) return;
  input.value = '';
  ui.add('user', q);
  // 预览模式：文字走 EV 试聊；本机语音对话会经 conversation API 同步到侧栏
  if (!USE_BROWSER_VOICE || !ui.active || ui.previewOnly) {
    try {
      const history = [];
      const j = await api('/api/agents/' + agent.id + '/chat', {
        method: 'POST',
        body: JSON.stringify({ message: q, history }),
      });
      ui.add('assistant', j.ok ? (j.reply || '') : (j.error || '调用失败'));
      if (j.ok) {
        if (j.panel) applyLivePanel(ui, j.panel);
        if (j.site_panel) applyLivePanel(ui, j.site_panel);
      }
      if (j.ok && j.reply) {
        try {
          await api('/api/agents/' + agent.id + '/conversation', {
            method: 'POST',
            body: JSON.stringify({ role: 'user', content: q, source: 'terminal-text' }),
          });
          await api('/api/agents/' + agent.id + '/conversation', {
            method: 'POST',
            body: JSON.stringify({ role: 'assistant', content: j.reply, source: 'terminal-text' }),
          });
        } catch (_) {}
      }
    } catch (e) {
      ui.add('assistant', e.message);
    }
    return;
  }
  const doc = ui.frame && ui.frame.contentDocument;
  const internalInput = doc && doc.getElementById('messageInput');
  let sent = false;
  try {
    if (typeof ui.frame?.contentWindow?.museSendText === 'function') {
      sent = !!ui.frame.contentWindow.museSendText(q);
    }
  } catch (e) {}
  if (!sent) ui.frame?.contentWindow?.postMessage({ source: 'muse-parent', type: 'sendText', text: q }, location.origin);
  if (!sent && internalInput) {
    internalInput.value = q;
    internalInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  }
}

function handleSurfaceCommand(q, ui, agent, devices, modules) {
  $('#surfaceInput').value = q;
  sendSurfaceText(ui, agent);
}

function toggleAgentWindow(name, open) {
  const ui = window.MUSE_SURFACE_UI;
  if (!ui) return;
  if (open) openSurfaceWindow(ui, { kind: name, title: name === 'status' ? '状态' : name, body: statusWindowHtml(ui.agent, ui.devices, ui.modules), position: 'right' });
  else { const w = ui.windows.get(name); if (w) closeSurfaceWindow(ui, w); }
}

function handleMuseMcpToolRequest(ui, msg, source) {
  const reply = (result, error) => {
    source?.postMessage({
      source: 'muse-parent',
      type: 'mcp-tool-result',
      requestId: msg.requestId,
      result,
      error
    }, location.origin);
  };
  try {
    reply(executeMuseMcpTool(ui, msg.tool, msg.args || {}));
  } catch (e) {
    reply(null, e.message);
  }
}

function executeMuseMcpTool(ui, tool, args) {
  switch (tool) {
    case 'muse.ui.open_panel': return museMcpOpenPanel(ui, args);
    case 'muse.ui.update_panel': return museMcpUpdatePanel(ui, args);
    case 'muse.ui.close_panel': return museMcpClosePanel(ui, args);
    case 'muse.ui.list_panels': return museMcpListPanels(ui);
    default: throw new Error('未知工具: ' + tool);
  }
}

const PANEL_TITLES = {
  devices: '设备', models: '模型', status: '状态', weather: '天气', news: '新闻',
  search: '检索', web: '网页', coding: '工作状态', site: '网站预览', custom: '信息',
};

const PANEL_DEFAULTS = {
  web: { w: 680, h: 540 },
  news: { w: 400, h: 420 },
  search: { w: 440, h: 400 },
  weather: { w: 420, h: 480 },
  coding: { w: 152, h: 224 },
  site: { w: 720, h: 560 },
};

const PLUGIN_LABELS = {
  get_weather: '天气查询',
  web_search: '网页搜索',
  search_from_ragflow: '知识库检索',
  get_time: '时间',
  play_music: '音乐播放',
  change_role: '角色切换'
};

function parsePanelData(args = {}) {
  if (args.data && typeof args.data === 'object') {
    const d = args.data;
    if (
      d.items || d.articles || d.paragraphs || d.full_text ||
      d.forecast || d.city || d.temp || d.current || d.details ||
      d.kind === 'coding' || d.kind === 'site' || d.preview_url || d.status || d.files
    ) {
      return d;
    }
  }
  if (typeof args.data === 'string') {
    try { return JSON.parse(args.data); } catch (_) { /* ignore */ }
  }
  if (typeof args.content === 'string' && args.content.trim().startsWith('{')) {
    try { return JSON.parse(args.content); } catch (_) { /* ignore */ }
  }
  return null;
}

function isSafePreviewUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === 'https:' || u.protocol === 'http:';
  } catch (_) {
    return false;
  }
}

function shortUrl(url) {
  try {
    const u = new URL(url);
    return u.hostname + u.pathname.slice(0, 40) + (u.pathname.length > 40 ? '…' : '');
  } catch (_) {
    return String(url || '').slice(0, 48);
  }
}

function weatherText(v) {
  if (v == null || v === '') return '';
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return String(v);
  if (typeof v === 'object') {
    return weatherText(
      v.text || v.description || v.condition || v.summary || v.weather
        || (v.temperature != null ? `${v.temperature}°C` : '')
        || (v.temp != null ? `${v.temp}°C` : '')
    );
  }
  return '';
}

function weatherIcon(desc) {
  const t = String(desc || '');
  if (/雷/.test(t)) return '⛈️';
  if (/雪|冻/.test(t)) return '❄️';
  if (/雨|阵雨/.test(t)) return '🌧️';
  if (/雾|霾/.test(t)) return '🌫️';
  if (/阴/.test(t)) return '☁️';
  if (/多云|少云|晴间多云/.test(t)) return '⛅';
  if (/晴/.test(t)) return '☀️';
  return '🌡️';
}

function extractTempNum(text) {
  const s = weatherText(text);
  const m = s.match(/(-?\d+(?:\.\d+)?)/);
  return m ? m[1] : '';
}

function weatherConditionFromText(text) {
  const s = weatherText(text).trim();
  if (!s) return '';
  const head = s.split(/[，,]/)[0].trim();
  return head || s;
}

function parseWeatherFromText(text) {
  const src = String(text || '').trim();
  if (!src) return null;
  const out = { forecast: [] };
  const cityMatch = src.match(/^(.{2,12}?)(?:市|省)?(?:未来|近日|最近)?\d*天天气/);
  if (cityMatch) out.city = cityMatch[1].trim();
  const rowRe = /(\d{2}\/\d{2})[：:]\s*([^，,\n]+)[，,]?\s*(?:气温|温度)?\s*(-?\d+)\s*[~～-]\s*(-?\d+)/g;
  let m;
  while ((m = rowRe.exec(src))) {
    out.forecast.push({
      date: m[1],
      weather: m[2].trim(),
      low: m[3],
      high: m[4],
    });
  }
  const curMatch = src.match(/当前[^。\n]*?[：:]\s*([^，,\n]+)[，,]\s*(-?\d+)\s*°?C?/);
  if (curMatch) {
    out.condition = curMatch[1].trim();
    out.temp = `${curMatch[2]}°C`;
    out.current = `${curMatch[1].trim()}，${curMatch[2]}°C`;
  }
  return out.forecast.length || out.temp ? out : null;
}

function mergeWeatherData(base, extra) {
  if (!base) return extra || null;
  if (!extra) return base;
  const out = { ...base };
  if (!out.city && extra.city) out.city = extra.city;
  if (!extractTempNum(out.temp) && extractTempNum(extra.temp)) {
    out.temp = extra.temp;
    out.current = extra.current || out.current;
  }
  if (!out.condition && extra.condition) out.condition = extra.condition;
  const rows = Array.isArray(out.forecast) ? [...out.forecast] : [];
  const extraRows = Array.isArray(extra.forecast) ? extra.forecast : [];
  out.forecast = rows.map((row, i) => {
    const add = extraRows[i] || {};
    return {
      ...row,
      date: row.date || add.date || '',
      weather: row.weather || add.weather || '',
      high: row.high || add.high || '',
      low: row.low || add.low || '',
    };
  });
  if (!out.forecast.length && extraRows.length) out.forecast = extraRows;
  return out;
}

function normalizeWeatherData(data, args = {}, ui = null) {
  if (!data || typeof data !== 'object') data = {};
  let out = { ...data };
  if (!out.city && args.title) {
    const m = String(args.title).match(/^(.+?)(?:天气|气象)/);
    if (m) out.city = m[1].trim();
  }
  const currentText = weatherText(out.current);
  const subtitleText = weatherText(out.subtitle);
  let tempText = weatherText(out.temp);
  if (!tempText && out.details && typeof out.details === 'object') {
    for (const [k, v] of Object.entries(out.details)) {
      if (/温度|temp/i.test(k)) {
        tempText = weatherText(v);
        if (tempText) break;
      }
    }
  }
  if (!tempText) {
    const n = extractTempNum(currentText);
    if (n) tempText = `${n}°C`;
  }
  out.temp = tempText;
  out.current = currentText;
  out.subtitle = subtitleText;
  out.condition = weatherText(out.condition)
    || weatherConditionFromText(currentText)
    || weatherConditionFromText(subtitleText);
  out.forecast = (Array.isArray(out.forecast) ? out.forecast : []).map(f => {
    const row = f && typeof f === 'object' ? f : {};
    return {
      date: weatherText(row.date) || row.date || '',
      weather: weatherText(row.weather) || weatherText(row.condition) || row.weather || '',
      high: weatherText(row.high) || weatherText(row.max) || row.high || '',
      low: weatherText(row.low) || weatherText(row.min) || row.low || '',
    };
  });
  if (out.details && typeof out.details === 'object' && !Array.isArray(out.details)) {
    const details = {};
    Object.entries(out.details).forEach(([k, v]) => {
      const val = weatherText(v);
      if (val && val !== '0') details[k] = val;
    });
    out.details = details;
  }

  const parsed = parseWeatherFromText(args.content || '');
  if (parsed) out = mergeWeatherData(out, parsed);
  if (ui?.lastWeatherData) out = mergeWeatherData(ui.lastWeatherData, out);

  const hasTemp = !!extractTempNum(out.temp) || _forecastHasTemps(out.forecast);
  if (hasTemp && ui) ui.lastWeatherData = out;
  return out;
}

function _forecastHasTemps(forecast) {
  return (forecast || []).some(f => extractTempNum(f.high) || extractTempNum(f.low));
}

function renderWeatherPanel(data, fallback, args = {}, ui = null) {
  data = normalizeWeatherData(data, args, ui);
  if (!data || (!_forecastHasTemps(data.forecast) && !extractTempNum(data.temp))) {
    const text = (typeof fallback === 'string' && fallback.trim()) ? fallback.trim() : '';
    if (text) return `<div class="panel-body-text">${esc(text)}</div>`;
    return `<div class="panel-empty">${esc(fallback || '等待天气数据…')}</div>`;
  }

  const city = data.city || '天气';
  const condition = data.condition || weatherConditionFromText(data.subtitle) || '—';
  const tempNum = extractTempNum(data.temp) || extractTempNum(data.current) || '—';
  const icon = weatherIcon(condition);
  const today = (data.forecast || [])[0] || {};
  const todayRange = today.low || today.high
    ? `${today.low || '—'}° ~ ${today.high || '—'}°`
    : '';

  const chips = Object.entries(data.details || {}).map(([k, v]) =>
    `<span class="wx-chip"><b>${esc(k)}</b>${esc(v)}</span>`
  ).join('');

  const forecastRows = (data.forecast || []).map(f => {
    const desc = f.weather || '—';
    const hi = f.high || '—';
    const lo = f.low || '—';
    return `<div class="wx-day">
      <span class="wx-day-date">${esc(f.date || '')}</span>
      <span class="wx-day-icon" aria-hidden="true">${weatherIcon(desc)}</span>
      <span class="wx-day-desc">${esc(desc)}</span>
      <span class="wx-day-temp"><span class="wx-lo">${esc(lo)}°</span><span class="wx-hi">${esc(hi)}°</span></span>
    </div>`;
  }).join('');

  return `<div class="panel-weather">
    <div class="wx-hero">
      <div class="wx-hero-top">
        <span class="wx-city">${esc(city)}</span>
        ${todayRange ? `<span class="wx-today-range">今日 ${esc(todayRange)}</span>` : ''}
      </div>
      <div class="wx-hero-main">
        <div class="wx-icon" aria-hidden="true">${icon}</div>
        <div class="wx-hero-temp">
          <span class="wx-temp-num">${esc(tempNum)}</span><span class="wx-temp-unit">°C</span>
        </div>
        <div class="wx-hero-meta">
          <div class="wx-condition">${esc(condition)}</div>
          ${data.subtitle && data.subtitle !== condition ? `<div class="wx-sub">${esc(data.subtitle)}</div>` : ''}
        </div>
      </div>
      ${chips ? `<div class="wx-chips">${chips}</div>` : ''}
    </div>
    ${forecastRows ? `<div class="wx-forecast">
      <div class="wx-forecast-title">7 日预报</div>
      <div class="wx-forecast-grid">${forecastRows}</div>
    </div>` : ''}
  </div>`;
}

function normalizeNewsData(data) {
  if (!data || typeof data !== 'object') return null;
  if (Array.isArray(data.items) && data.items.length) return data;
  if (Array.isArray(data.articles) && data.articles.length) {
    return {
      source: data.source || '新闻',
      items: data.articles.map(a => ({
        title: a.title || '无标题',
        url: a.url || '',
        source: data.source || a.source || '',
        snippet: a.content || a.snippet || a.desc || ''
      }))
    };
  }
  return data;
}

function renderNewsPanel(data, fallback) {
  data = normalizeNewsData(data);
  if (!data?.items?.length) {
    const text = (typeof fallback === 'string' && fallback.trim()) ? fallback.trim() : '';
    if (text) return `<div class="panel-body-text">${esc(text)}</div>`;
    return `<div class="panel-empty">${esc(fallback || '等待新闻数据…')}</div>`;
  }
  const rows = data.items.map((it, i) => {
    const url = it.url && isSafePreviewUrl(it.url) ? it.url : '';
    const preview = url ? ` data-preview-url="${esc(url)}" data-preview-title="${esc(it.title || '新闻')}"` : '';
    return `<li class="panel-list-item"${preview}>
      <div class="panel-item-title">${i + 1}. ${esc(it.title || '无标题')}</div>
      <div class="panel-meta">${esc(it.source || data.source || '')}${url ? ' · 点击预览' : ''}</div>
    </li>`;
  }).join('');
  return `<div class="panel-news">
    <div class="panel-h2">${esc(data.source || '热点新闻')}</div>
    <ul class="panel-list">${rows}</ul>
  </div>`;
}

function renderSearchPanel(data, fallback) {
  if (!data?.items?.length) return `<div class="panel-empty">${esc(fallback || '等待检索结果…')}</div>`;
  const pagesPayload = (data.pages || []).map(p => ({
    url: p.url,
    title: p.title,
    site: p.site,
    summary: p.summary,
    images: p.images || [],
    paragraphs: p.paragraphs || [],
    full_text: p.full_text || '',
    ok: p.ok,
  }));
  const rows = data.items.map((it, i) => {
    const url = it.url && isSafePreviewUrl(it.url) ? it.url : '';
    const page = (data.pages || []).find(p => p?.url && it.url && p.url.split('#')[0] === it.url.split('#')[0]);
    const pageIdx = page ? (data.pages || []).indexOf(page) : -1;
    const preview = url
      ? ` data-preview-url="${esc(url)}" data-preview-title="${esc(it.title || '网页')}" data-page-index="${pageIdx}"`
      : '';
    const img = page?.images?.[0]?.url || (data.images || [])[i]?.url;
    const safeImg = img && isSafePreviewUrl(img) ? img : '';
    const host = (() => { try { return url ? new URL(url).hostname : ''; } catch (_) { return ''; } })();
    return `<li class="panel-list-item"${preview}>
      <div class="panel-item-title">${i + 1}. ${esc(it.title || '无标题')}</div>
      ${safeImg ? `<div class="panel-item-thumb"><img src="${esc(safeImg)}" alt="" loading="lazy" referrerpolicy="no-referrer"></div>` : ''}
      ${it.snippet ? `<div class="panel-item-desc">${esc(it.snippet)}</div>` : ''}
      ${page?.summary && page.summary !== it.snippet ? `<div class="panel-item-desc">${esc(String(page.summary).slice(0, 160))}</div>` : ''}
      <div class="panel-meta">${esc(host)}${it.date ? ' · ' + esc(it.date) : ''}${url ? ' · 点击打开' : ''}</div>
      ${url ? `<a class="panel-item-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer" data-external-url="${esc(url)}">${esc(url)}</a>` : ''}
    </li>`;
  }).join('');
  const gallery = (data.images || []).filter(im => im?.url && isSafePreviewUrl(im.url)).slice(0, 6);
  const galleryHtml = gallery.length
    ? `<div class="panel-image-strip">${gallery.map(im =>
        `<a class="panel-image-cell" href="${esc(im.url)}" target="_blank" rel="noopener noreferrer" data-external-url="${esc(im.url)}">
          <img src="${esc(im.url)}" alt="${esc(im.alt || '')}" loading="lazy" referrerpolicy="no-referrer">
        </a>`).join('')}</div>`
    : '';
  return `<div class="panel-search" data-search-pages="${esc(JSON.stringify(pagesPayload))}">
    ${data.query ? `<div class="panel-h2">「${esc(data.query)}」</div>` : ''}
    ${data.summary ? `<div class="panel-summary">${esc(data.summary)}</div>` : ''}
    ${galleryHtml}
    <ul class="panel-list">${rows}</ul>
  </div>`;
}

function normalizePanelArgs(args = {}) {
  const a = { ...args };
  if (typeof a.data === 'string') {
    try { a.data = JSON.parse(a.data); } catch (_) { /* ignore */ }
  }
  return a;
}

function paragraphsFromFullText(text) {
  return String(text || '')
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean)
    .map(line => ({ tag: 'p', text: line }));
}

function readerPayloadFromArgs(args = {}) {
  const a = normalizePanelArgs(args);
  const data = parsePanelData(a) || {};
  const url = (a.url || data.url || '').trim();
  let paragraphs = Array.isArray(data.paragraphs) ? data.paragraphs.filter(p => p?.text) : [];
  if (!paragraphs.length && data.full_text) {
    paragraphs = paragraphsFromFullText(data.full_text);
  }
  if (!paragraphs.length) return null;
  return {
    ...data,
    url: url || data.url || '',
    paragraphs
  };
}

function renderWebPanel(args = {}) {
  const a = normalizePanelArgs(args);
  const reader = readerPayloadFromArgs(a);
  if (reader) {
    return renderReaderHtml(reader);
  }
  const data = parsePanelData(a);
  const url = [a.url, data?.url].find(u => u && isSafePreviewUrl(u)) || '';
  if (!url) return `<div class="panel-empty">未提供可预览的网页地址</div>`;
  const safeUrl = esc(url);
  return `<div class="panel-web-reader" data-web-reader-url="${safeUrl}">
    <div class="panel-web-loading panel-meta">正在读取网页…</div>
    <div class="panel-meta" style="margin-top:8px;word-break:break-all">${safeUrl}</div>
  </div>`;
}

function renderReaderHtml(data) {
  const parts = (data.paragraphs || []).map(p => {
    if (p.tag === 'h2' || p.tag === 'h3' || p.tag === 'h4') {
      return `<h3 class="panel-reader-h">${esc(p.text)}</h3>`;
    }
    return `<p class="panel-reader-p">${esc(p.text)}</p>`;
  }).join('');
  const imgs = (data.images || []).filter(im => {
    const u = typeof im === 'string' ? im : im?.url;
    return u && isSafePreviewUrl(u);
  }).slice(0, 6);
  const imgHtml = imgs.length
    ? `<div class="panel-image-strip">${imgs.map(im => {
        const u = typeof im === 'string' ? im : im.url;
        const alt = typeof im === 'string' ? '' : (im.alt || '');
        return `<a class="panel-image-cell" href="${esc(u)}" target="_blank" rel="noopener noreferrer" data-external-url="${esc(u)}">
          <img src="${esc(u)}" alt="${esc(alt)}" loading="lazy" referrerpolicy="no-referrer">
        </a>`;
      }).join('')}</div>`
    : '';
  return `<div class="panel-reader">
    ${data.site ? `<div class="panel-meta panel-reader-site">${esc(data.site)}</div>` : ''}
    <div class="panel-h1 panel-reader-title">${esc(data.title || '网页预览')}</div>
    ${data.summary ? `<div class="panel-summary">${esc(data.summary)}</div>` : ''}
    ${imgHtml}
    ${data.url ? `<a class="panel-item-link" href="${esc(data.url)}" target="_blank" rel="noopener noreferrer" data-external-url="${esc(data.url)}">${esc(data.url)}</a>` : ''}
    <div class="panel-reader-body">${parts || '<div class="panel-empty">未能提取正文</div>'}</div>
    <div class="panel-reader-foot">
      <button type="button" class="btn link panel-web-ext" data-external-url="${esc(data.url)}">在原站打开</button>
    </div>
  </div>`;
}

async function hydrateWebReaderPanels(body) {
  if (!body) return;
  const tasks = [...body.querySelectorAll('[data-web-reader-url]')].map(async wrap => {
    if (wrap.dataset.readerLoaded === '1') return;
    wrap.dataset.readerLoaded = '1';
    const url = wrap.dataset.webReaderUrl;
    if (!url) return;
    try {
      const r = await fetch('/api/web/reader?url=' + encodeURIComponent(url));
      let data = {};
      try { data = await r.json(); } catch (_) { data = { ok: false, error: '读取响应失败' }; }
      const reader = data.ok ? readerPayloadFromArgs({ url, data }) : null;
      if (reader) {
        wrap.innerHTML = renderReaderHtml(reader);
      } else {
        wrap.innerHTML = `<div class="panel-empty">${esc(data.error || '无法读取正文')}</div>
          <div class="panel-reader-foot">
            <button type="button" class="btn link panel-web-ext" data-external-url="${esc(url)}">在原站打开</button>
          </div>`;
      }
    } catch (e) {
      wrap.innerHTML = `<div class="panel-empty">读取失败，请稍后重试</div>
        <div class="panel-reader-foot">
          <button type="button" class="btn link panel-web-ext" data-external-url="${esc(url)}">在原站打开</button>
        </div>`;
    }
  });
  await Promise.all(tasks);
}

function renderCodingPanel(data = {}, fallback = '') {
  const d = data && typeof data === 'object' ? data : {};
  const files = Array.isArray(d.files) ? d.files : [];
  const log = Array.isArray(d.log) ? d.log.filter(Boolean).slice(-24) : [];
  const planSteps = Array.isArray(d.plan_steps) ? d.plan_steps : [];
  const risks = Array.isArray(d.risks) ? d.risks : [];
  const pct = Math.max(0, Math.min(100, Number(d.percent) || 0));
  const done = !!d.done;
  const ok = d.ok !== false;
  const phase = String(d.phase || (done ? 'done' : 'writing'));
  const status = d.status || (done ? (ok ? '完成' : '失败') : '进行中');
  const detail = d.detail || fallback || '';
  const previewUrl = d.preview_url || '';
  const phaseLabel = ({
    clarifying: '澄清',
    planning: '计划',
    awaiting_confirm: '待确认',
    writing: '编写中',
    idle: '空闲',
    done: '完成',
  })[phase] || phase;
  const planHtml = planSteps.length
    ? `<div class="panel-meta" style="margin:10px 0 4px">计划</div>
      <ol class="coding-plan">${planSteps.map(s => `<li>${esc(typeof s === 'string' ? s : (s.title || s.text || JSON.stringify(s)))}</li>`).join('')}</ol>`
    : '';
  const riskHtml = risks.length
    ? `<div class="panel-meta" style="margin:10px 0 4px">风险</div>
      <ul class="panel-list coding-risks">${risks.map(r => {
        const title = typeof r === 'string' ? r : (r.title || r.detail || '');
        const sev = typeof r === 'object' && r.severity ? `[${r.severity}] ` : '';
        return `<li class="panel-list-item"><div class="panel-item-title">${esc(sev + title)}</div></li>`;
      }).join('')}</ul>`
    : '';
  const logHtml = log.length
    ? `<div class="panel-meta" style="margin:10px 0 4px">活动</div>
      <div class="coding-log">${log.map(line => `<div class="coding-log-line">${esc(String(line).slice(0, 240))}</div>`).join('')}</div>`
    : '';
  const fileRows = files.length
    ? `<ul class="panel-list coding-files">${files.map(f =>
        `<li class="panel-list-item"><div class="panel-item-title">${esc(String(f))}</div></li>`
      ).join('')}</ul>`
    : (phase === 'writing' || done
      ? `<div class="panel-empty">${done ? '未检测到新文件' : '等待文件写入…'}</div>`
      : '');
  const previewHtml = previewUrl && isSafePreviewUrl(previewUrl)
    ? `<div class="panel-meta" style="margin:10px 0 4px">预览${d.preview_locked ? '（锁定）' : ''}</div>
      <iframe class="panel-site-frame coding-preview-frame" src="${esc(previewUrl)}" title="preview" sandbox="allow-scripts allow-same-origin allow-forms allow-popups"></iframe>`
    : '';
  const actions = previewUrl
    ? `<div class="panel-reader-foot coding-actions">
        <button type="button" class="btn" data-open-site="${esc(previewUrl)}" data-site-title="网站预览">放大预览</button>
        <button type="button" class="btn link" data-external-url="${esc(previewUrl)}">新标签打开</button>
      </div>`
    : '';
  return `<div class="panel-coding" data-coding-done="${done ? '1' : '0'}" data-phase="${esc(phase)}">
    <div class="coding-phase">${esc(phaseLabel)}</div>
    <div class="panel-h2">${esc(status)}</div>
    ${detail ? `<div class="panel-summary">${esc(detail)}</div>` : ''}
    ${(phase === 'writing' || done) ? `<div class="coding-bar"><i style="width:${pct}%"></i></div>
    <div class="panel-meta coding-pct">${done ? (ok ? '已完成' : '未完成') : ('进度 ' + pct + '%')}</div>` : ''}
    ${planHtml}
    ${riskHtml}
    ${logHtml}
    ${d.cwd ? `<div class="panel-meta" style="margin-top:6px;word-break:break-all">${esc(d.cwd)}</div>` : ''}
    ${files.length || phase === 'writing' || done ? `<div class="panel-meta" style="margin:10px 0 6px">文件</div>${fileRows}` : ''}
    ${previewHtml}
    ${d.summary && done ? `<div class="panel-body-text" style="margin-top:10px">${esc(String(d.summary).slice(0, 600))}</div>` : ''}
    ${actions}
  </div>`;
}

function renderSitePanel(args = {}) {
  const data = parsePanelData(args) || {};
  const url = args.url || data.url || '';
  if (!url || !isSafePreviewUrl(url)) {
    return `<div class="panel-empty">没有可预览的地址</div>`;
  }
  return `<div class="panel-site">
    <div class="panel-meta" style="margin-bottom:8px;word-break:break-all">${esc(data.path || url)}</div>
    <iframe class="panel-site-frame" src="${esc(url)}" title="site preview" sandbox="allow-scripts allow-same-origin allow-forms allow-popups"></iframe>
    <div class="panel-reader-foot" style="margin-top:8px">
      <button type="button" class="btn link" data-external-url="${esc(url)}">在新标签打开</button>
    </div>
  </div>`;
}

function buildPanelBody(ui, panel, args = {}) {
  const data = parsePanelData(args);
  const fallback = args.content || '';
  const map = {
    devices: () => deviceWindowHtml(ui.devices),
    models: () => modelWindowHtml(ui.modules),
    status: () => statusWindowHtml(ui.agent, ui.devices, ui.modules),
    weather: () => renderWeatherPanel(data, fallback, args, ui),
    news: () => renderNewsPanel(data, fallback),
    search: () => renderSearchPanel(data, fallback),
    coding: () => renderCodingPanel(data || args.data || {}, fallback),
    site: () => renderSitePanel(args),
    web: () => {
      const reader = readerPayloadFromArgs(args);
      if (reader) return renderReaderHtml(reader);
      const c = (args.content || '').trim();
      if (c) return `<div class="panel-body-text">${esc(c)}</div>`;
      return renderWebPanel(args);
    },
    custom: () => {
      const c = args.content || '';
      const reader = readerPayloadFromArgs(args);
      if (reader) return renderReaderHtml({ ...reader, url: args.url || reader.url });
      const pdata = normalizeNewsData(parsePanelData(args));
      if (pdata?.items?.length) return renderNewsPanel(pdata, c);
      if (data?.kind === 'coding' || args.panel === 'coding') return renderCodingPanel(data || {}, c);
      if (!c) return '<div class="panel-empty">无内容</div>';
      return c.includes('<') ? c : `<div class="panel-body-text">${esc(c)}</div>`;
    }
  };
  return (map[panel] || map.custom)();
}

function wirePanelBodyInteractions(ui, win) {
  if (!win) return;
  const body = win.querySelector('.win-body');
  if (!body) return;
  body.querySelectorAll('[data-preview-url]').forEach(el => {
    el.onclick = (e) => {
      if (e.target.closest('[data-external-url], a.panel-item-link')) return;
      e.preventDefault();
      const url = el.dataset.previewUrl;
      const title = el.dataset.previewTitle || '网页预览';
      if (!url) return;
      let pageData = null;
      try {
        const root = el.closest('.panel-search');
        const pages = JSON.parse(root?.dataset?.searchPages || '[]');
        const idx = Number(el.dataset.pageIndex);
        if (Number.isInteger(idx) && idx >= 0 && pages[idx]?.ok) pageData = pages[idx];
        else pageData = pages.find(p => p?.url && p.url.split('#')[0] === url.split('#')[0] && p.ok) || null;
      } catch (_) { pageData = null; }
      if (pageData && (pageData.paragraphs?.length || pageData.full_text)) {
        museMcpOpenPanel(ui, {
          panel: 'web', title: pageData.title || title, url,
          data: pageData, width: 680, height: 540, position: 'right-top',
        });
      } else {
        museMcpOpenPanel(ui, { panel: 'web', title, url, width: 680, height: 540, position: 'right-top' });
      }
    };
  });
  body.querySelectorAll('[data-open-site]').forEach(btn => {
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const url = btn.dataset.openSite;
      if (!url) return;
      museMcpOpenPanel(ui, {
        panel: 'site',
        window_id: 'site-preview',
        title: btn.dataset.siteTitle || '网站预览',
        url,
        width: 720,
        height: 560,
        position: 'right',
        data: { kind: 'site', url },
      });
    };
  });
  body.querySelectorAll('[data-external-url]').forEach(btn => {
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const url = btn.dataset.externalUrl;
      if (url) window.open(url, '_blank', 'noopener,noreferrer');
    };
  });
  hydrateWebReaderPanels(body).then(() => {
    body.querySelectorAll('[data-external-url]').forEach(btn => {
      btn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        const url = btn.dataset.externalUrl;
        if (url) window.open(url, '_blank', 'noopener,noreferrer');
      };
    });
  });
}

function applyLivePanel(ui, panelArgs) {
  if (!ui || !panelArgs || typeof panelArgs !== 'object') return;
  try {
    // 只更新同一工作状态视图；项目结果仍复用 site 预览窗。
    return museMcpOpenPanel(ui, panelArgs);
  } catch (e) {
    console.warn('[EV] applyLivePanel', e);
  }
}

function buildPanelSpec(ui, panel, args = {}) {
  const kind = panel || 'status';
  const size = {};
  const defaults = PANEL_DEFAULTS[kind] || {};
  if (args.width) size.w = clamp(+args.width, 260, 860);
  else if (defaults.w) size.w = defaults.w;
  if (args.height) size.h = clamp(+args.height, 180, 640);
  else if (defaults.h) size.h = defaults.h;
  return {
    kind,
    title: args.title || PANEL_TITLES[kind] || '窗口',
    body: buildPanelBody(ui, kind, args),
    position: args.position || 'auto',
    size: Object.keys(size).length ? size : null
  };
}

function findSurfaceWindow(ui, { window_id, panel } = {}) {
  if (window_id) {
    const win = ui.windows.get(window_id);
    if (win) return win;
    return $$(`.agent-window.open[data-window="${window_id}"]`)[0] || null;
  }
  if (panel) {
    const matches = $$('.agent-window.open').filter(w => w.dataset.panel === panel);
    return matches.sort((a, b) => (+b.style.zIndex || 0) - (+a.style.zIndex || 0))[0] || null;
  }
  return getTopSurfaceWindow();
}

function surfaceWindowInfo(win) {
  if (!win) return null;
  return {
    window_id: win.dataset.window,
    panel: win.dataset.panel || 'custom',
    title: win.querySelector('.win-head span')?.textContent || '',
    position: { left: parseFloat(win.style.left) || 0, top: parseFloat(win.style.top) || 0 },
    size: { width: win.offsetWidth, height: win.offsetHeight }
  };
}

const SURFACE_REVEAL_MS = 520;

function finishSurfaceWindowReveal(win) {
  if (!win) return;
  if (win._revealTimer) {
    clearTimeout(win._revealTimer);
    win._revealTimer = null;
  }
  if (win._closeTimer) {
    clearTimeout(win._closeTimer);
    win._closeTimer = null;
  }
  win.classList.remove('booting', 'revealing', 'closing', 'no-transition');
}

function scheduleSurfaceWindowRevealEnd(win) {
  if (!win) return;
  if (win._revealTimer) clearTimeout(win._revealTimer);
  win._revealTimer = setTimeout(() => finishSurfaceWindowReveal(win), SURFACE_REVEAL_MS + 80);
}

function playSurfaceWindowReveal(win, spec) {
  if (!win) return;
  if (win._revealTimer) clearTimeout(win._revealTimer);
  win.classList.remove('closing', 'revealing');
  win.classList.add('booting', 'no-transition');
  void win.offsetWidth;
  win.classList.remove('no-transition');
  void win.offsetWidth;
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      win.classList.remove('booting');
      if (spec?.position) placeSurfaceWindow(win, spec.position, true);
    });
  });
  scheduleSurfaceWindowRevealEnd(win);
}

function replaySurfaceWindowReveal(win) {
  if (!win) return;
  playSurfaceWindowReveal(win, null);
}

function closeSurfaceWindow(ui, win) {
  if (!win || win.classList.contains('closing')) return;
  const id = win.dataset.window;
  if (win._closeTimer) clearTimeout(win._closeTimer);
  finishSurfaceWindowReveal(win);
  win.classList.add('closing');
  void win.offsetWidth;
  win._closeTimer = setTimeout(() => {
    win._closeTimer = null;
    if (id) ui.windows.delete(id);
    win.remove();
  }, SURFACE_REVEAL_MS);
}

function ensureSurfaceWindowVisible(win) {
  finishSurfaceWindowReveal(win);
}

const STABLE_PANEL_IDS = {
  coding: 'work-hud',
  site: 'site-preview',
};

function museMcpOpenPanel(ui, args) {
  const a = normalizePanelArgs(args);
  const panel = a.panel || 'status';
  // 同项目单例：coding / site 永远复用固定 id
  const windowId = a.window_id || a.windowId || STABLE_PANEL_IDS[panel] || '';
  let existing = findSurfaceWindow(ui, windowId ? { window_id: windowId } : { panel });
  // 旧窗没有稳定 id 时，按 panel 种类吞掉并改挂稳定 id
  if (!existing && STABLE_PANEL_IDS[panel]) {
    existing = findSurfaceWindow(ui, { panel });
    if (existing && existing.dataset.window !== windowId) {
      const oldId = existing.dataset.window;
      ui.windows.delete(oldId);
      existing.dataset.window = windowId;
      ui.windows.set(windowId, existing);
    }
  }
  if (existing) {
    // site：同 URL 只置顶，不整页重建（真正复用预览窗）
    if (panel === 'site' && a.url) {
      const frame = existing.querySelector('.panel-site-frame, .coding-preview-frame');
      const cur = frame?.getAttribute('src') || '';
      if (frame && cur === a.url) {
        if (a.title) {
          const titleEl = existing.querySelector('.win-head span');
          if (titleEl) titleEl.textContent = a.title;
        }
        existing.style.zIndex = ++ui.z;
        ensureSurfaceWindowVisible(existing);
        return { success: true, ...surfaceWindowInfo(existing), message: '预览窗已复用' };
      }
      if (frame && a.url) {
        frame.setAttribute('src', a.url);
        if (a.title) {
          const titleEl = existing.querySelector('.win-head span');
          if (titleEl) titleEl.textContent = a.title;
        }
        existing.style.zIndex = ++ui.z;
        ensureSurfaceWindowVisible(existing);
        return { success: true, ...surfaceWindowInfo(existing), message: '预览已更新' };
      }
    }
    return museMcpUpdatePanel(ui, {
      ...a,
      panel,
      window_id: existing.dataset.window,
      bring_to_front: a.bring_to_front !== false,
    });
  }
  const spec = buildPanelSpec(ui, panel, a);
  if (windowId) spec.window_id = windowId;
  const win = openSurfaceWindow(ui, spec);
  if (!win) return { success: false, error: '无法创建窗口' };
  wirePanelBodyInteractions(ui, win);
  return { success: true, ...surfaceWindowInfo(win), message: `已打开${spec.title}面板` };
}

function museMcpUpdatePanel(ui, args) {
  const a = normalizePanelArgs(args);
  const win = findSurfaceWindow(ui, a);
  if (!win) return { success: false, error: '未找到要调整的窗口，可先调用 list_panels' };
  const spec = {};
  if (a.position) spec.position = a.position;
  if (a.width || a.height) {
    spec.size = {
      w: a.width ? clamp(+a.width, 260, 860) : win.offsetWidth,
      h: a.height ? clamp(+a.height, 180, 640) : win.offsetHeight
    };
  }
  if (spec.position || spec.size) moveSurfaceWindow(win, spec);
  if (a.title) {
    const titleEl = win.querySelector('.win-head span');
    if (titleEl) titleEl.textContent = a.title;
  }
  if (a.content || a.data || a.url) {
    const body = win.querySelector('.win-body');
    const panelKind = a.panel || win.dataset.panel || 'custom';
    if (body) {
      // 预览 iframe 同 URL 时保留，避免每次进度刷新整页闪烁
      const prevFrame = body.querySelector('.coding-preview-frame, .panel-site-frame');
      const prevSrc = prevFrame?.getAttribute('src') || '';
      body.innerHTML = buildPanelBody(ui, panelKind, a);
      const nextFrame = body.querySelector('.coding-preview-frame, .panel-site-frame');
      if (nextFrame && prevSrc && nextFrame.getAttribute('src') === prevSrc) {
        // 已是同一预览，无需额外处理；innerHTML 已重建，但同 src 浏览器常复用缓存
      }
    }
    // 内容更新不再 replay 展开动画（否则像又弹了一窗）
    wirePanelBodyInteractions(ui, win);
  }
  if (a.bring_to_front) win.style.zIndex = ++ui.z;
  return { success: true, ...surfaceWindowInfo(win), message: '窗口已更新' };
}

function museMcpClosePanel(ui, args) {
  if (args.close_all) {
    const wins = [...ui.windows.values()];
    const count = wins.length;
    wins.forEach(win => closeSurfaceWindow(ui, win));
    return { success: true, closed: count, message: count ? `已关闭 ${count} 个窗口` : '没有打开的窗口' };
  }
  const win = findSurfaceWindow(ui, args);
  if (!win) return { success: false, error: '未找到要关闭的窗口' };
  const info = surfaceWindowInfo(win);
  closeSurfaceWindow(ui, win);
  return { success: true, closed: 1, ...info, message: '窗口已关闭' };
}

function museMcpListPanels(ui) {
  const panels = $$('.agent-window.open').map(surfaceWindowInfo).filter(Boolean);
  return { success: true, panels, count: panels.length };
}

function statusWindowHtml(agent, devices, modules) {
  const mine = devices || [];
  return `<p><b>智能体</b>${esc(agent?.name || 'EV')}</p>
    <p><b>设备</b>${mine.length ? mine.map(d => esc(d.name || d.mac)).join(' / ') : '未绑定设备'}</p>
    <p><b>语音链路</b>${['ASR', 'LLM', 'TTS'].map(k => `${k}:${(modules[k] || {}).selected || '未配置'}`).join(' · ')}</p>
    <p><b>MCP</b>设备端工具已启用（窗口面板 muse.ui.open_panel / update_panel / close_panel）</p>`;
}

function deviceWindowHtml(devices) {
  if (!devices || !devices.length) return '<p><b>设备</b>当前智能体还没有绑定设备。</p>';
  return devices.map(d => `<p><b>${esc(d.name || '未命名设备')}</b>${esc(d.mac)} · ${timeAgo(d.last_seen)}</p>`).join('');
}

function modelWindowHtml(modules) {
  return ['ASR', 'LLM', 'TTS', 'VAD', 'Intent', 'Memory', 'VLLM']
    .map(k => `<p><b>${k}</b>${esc((modules[k] || {}).selected || '未配置')}</p>`).join('');
}

function openSurfaceWindow(ui, spec) {
  const layer = $('#agentWindows');
  if (!layer) return null;
  $('.agent-stage')?.classList.add('session-live');
  const kind = spec.kind || 'panel';
  const id = String(spec.window_id || spec.id || (kind + '-' + Date.now().toString(36))).replace(/[^\w.-]/g, '-');
  // 同 id 已存在则更新，绝不叠第二扇
  const existed = ui.windows.get(id) || $$(`.agent-window.open[data-window="${id}"]`)[0];
  if (existed) {
    const body = existed.querySelector('.win-body');
    if (body && spec.body != null) body.innerHTML = spec.body;
    const titleEl = existed.querySelector('.win-head span');
    if (titleEl && spec.title) titleEl.textContent = spec.title;
    existed.style.zIndex = ++ui.z;
    ensureSurfaceWindowVisible(existed);
    wirePanelBodyInteractions(ui, existed);
    return existed;
  }
  const win = el('div', 'agent-window open booting');
  const size = spec.size || {};
  const defaults = PANEL_DEFAULTS[kind] || {};
  win.dataset.window = id;
  win.dataset.panel = kind;
  win.style.width = (size.w || defaults.w || 420) + 'px';
  win.style.height = (size.h || defaults.h || 300) + 'px';
  win.style.zIndex = ++ui.z;
  win.innerHTML = `<div class="win-head"><span>${esc(spec.title || '窗口')}</span><button type="button" title="关闭">×</button></div>
    <div class="win-body">${spec.body || '<p>等待数据...</p>'}</div>`;
  layer.appendChild(win);
  ui.windows.set(id, win);
  win.addEventListener('pointerdown', () => win.style.zIndex = ++ui.z);
  win.querySelector('.win-head button').onclick = () => closeSurfaceWindow(ui, win);
  makeWindowDraggable(win, ui);
  placeSurfaceWindow(win, spec.position || 'center', false);
  playSurfaceWindowReveal(win, spec);
  wirePanelBodyInteractions(ui, win);
  return win;
}

function getTopSurfaceWindow() {
  const wins = $$('.agent-window.open');
  return wins.sort((a, b) => (+b.style.zIndex || 0) - (+a.style.zIndex || 0))[0] || null;
}

function moveSurfaceWindow(win, spec = {}) {
  win.classList.remove('no-transition');
  if (spec.size) {
    win.style.width = spec.size.w + 'px';
    win.style.height = spec.size.h + 'px';
  }
  if (spec.position) {
    void win.offsetWidth;
    placeSurfaceWindow(win, spec.position, true);
  }
}

function placeSurfaceWindow(win, position, animate = false) {
  const layer = $('#agentWindows');
  if (!layer) return;
  if (!animate) win.classList.add('no-transition');
  else win.classList.remove('no-transition');
  const pad = 22;
  const lw = layer.clientWidth || 900, lh = layer.clientHeight || 620;
  const w = Math.min(parseFloat(win.style.width) || 420, lw - pad * 2);
  const h = Math.min(parseFloat(win.style.height) || 300, lh - pad * 2);
  win.style.width = w + 'px';
  win.style.height = h + 'px';
  const anchors = {
    'left-top': [pad, pad], 'right-top': [lw - w - pad, pad],
    'left-bottom': [pad, lh - h - pad], 'right-bottom': [lw - w - pad, lh - h - pad],
    left: [pad, Math.round((lh - h) / 2)], right: [lw - w - pad, Math.round((lh - h) / 2)],
    top: [Math.round((lw - w) / 2), pad], bottom: [Math.round((lw - w) / 2), lh - h - pad],
    center: [Math.round((lw - w) / 2), Math.round((lh - h) / 2)]
  };
  let [x, y] = anchors[position] || findFreeWindowSpot(win, w, h, lw, lh, pad);
  const rect = () => ({ x, y, w, h });
  const others = $$('.agent-window.open').filter(o => o !== win);
  let guard = 0;
  while (others.some(o => rectsOverlap(rect(), windowRect(o))) && guard++ < 40) {
    x += 34; y += 28;
    if (x + w > lw - pad) x = pad;
    if (y + h > lh - pad) y = pad;
  }
  win.style.left = clamp(x, pad, Math.max(pad, lw - w - pad)) + 'px';
  win.style.top = clamp(y, pad, Math.max(pad, lh - h - pad)) + 'px';
  if (!animate) requestAnimationFrame(() => win.classList.remove('no-transition'));
}

function findFreeWindowSpot(win, w, h, lw, lh, pad) {
  const spots = [
    [lw - w - pad, pad], [pad, pad], [lw - w - pad, lh - h - pad], [pad, lh - h - pad],
    [Math.round((lw - w) / 2), pad], [Math.round((lw - w) / 2), Math.round((lh - h) / 2)]
  ];
  const others = $$('.agent-window.open').filter(o => o !== win);
  return spots.find(([x, y]) => !others.some(o => rectsOverlap({ x, y, w, h }, windowRect(o)))) || spots[0];
}

function windowRect(win) {
  return { x: parseFloat(win.style.left) || 0, y: parseFloat(win.style.top) || 0, w: win.offsetWidth, h: win.offsetHeight };
}

function rectsOverlap(a, b) {
  return a.x < b.x + b.w + 12 && a.x + a.w + 12 > b.x && a.y < b.y + b.h + 12 && a.y + a.h + 12 > b.y;
}

function makeWindowDraggable(win, ui) {
  const head = win.querySelector('.win-head');
  head.addEventListener('pointerdown', e => {
    if (e.target.closest('button')) return;
    const layer = $('#agentWindows');
    win.classList.add('dragging');
    const start = { x: e.clientX, y: e.clientY, left: parseFloat(win.style.left) || 0, top: parseFloat(win.style.top) || 0 };
    win.style.zIndex = ++ui.z;
    head.setPointerCapture(e.pointerId);
    const move = ev => {
      const maxX = layer.clientWidth - win.offsetWidth - 12;
      const maxY = layer.clientHeight - win.offsetHeight - 12;
      win.style.left = clamp(start.left + ev.clientX - start.x, 12, Math.max(12, maxX)) + 'px';
      win.style.top = clamp(start.top + ev.clientY - start.y, 12, Math.max(12, maxY)) + 'px';
    };
    const up = () => {
      win.classList.remove('dragging');
      head.removeEventListener('pointermove', move);
      head.removeEventListener('pointerup', up);
      head.removeEventListener('pointercancel', up);
    };
    head.addEventListener('pointermove', move);
    head.addEventListener('pointerup', up);
    head.addEventListener('pointercancel', up);
  });
}

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, n));
}

async function renderTerminal(v, id) {
  setCrumb(['EV', '智能体']);
  const [{ agents }, { devices }, status] = await Promise.all([api('/api/agents'), api('/api/devices'), api('/api/status')]);
  if (!agents.length) {
    v.innerHTML = `<div class="terminal-empty"><h1>EV 没有可运行的智能体</h1><a class="btn" href="#/agents">进入设置模式</a></div>`;
    return;
  }
  const agent = pickAgent(agents, id);
  const modules = agent.modules || {};
  const mine = devices.filter(d => String(d.agent_id) === String(agent.id));
  const avatar = (BOOT.avatars || []).find(x => x.name === agent.avatar) || (BOOT.avatars || [])[0] || {};
  v.innerHTML = `<div class="ops">
    <section class="voice-dock open primary-voice" id="voiceDock">
      <div class="ops-head"><span>Agent Engine</span><button class="btn link" id="refreshDock">刷新智能体</button></div>
      <div id="agentTerminal"></div>
    </section>

    <section class="ops-hero">
      <div class="ops-copy">
        <div class="kicker">Live Agent</div>
        <div class="ops-title">
          <h1>${esc(agent.name)}</h1>
          <span class="led ${status.core_up ? 'ok' : 'err'}">${status.core_up ? 'ACTIVE' : 'CORE OFFLINE'}</span>
        </div>
        <p>${esc((agent.prompt || '该智能体还没有写人设。').slice(0, 130))}${(agent.prompt || '').length > 130 ? '…' : ''}</p>
        <div class="ops-actions">
          <select id="opsAgentPick"></select>
          <a class="btn ghost" href="#/agents/${agent.id}">设置模式</a>
          <a class="btn ghost" href="#/agents/${agent.id}/avatar">形象管理</a>
        </div>
      </div>
      <div class="ops-avatar">
        <canvas id="live2d-stage"></canvas>
        <div class="avatar-scan"></div>
        <div class="avatar-status"><span></span> avatar linked · ${esc(agent.avatar || 'default')}</div>
      </div>
    </section>

    <section class="ops-grid">
      <div class="ops-chat">
        <div class="ops-head"><span>Status Console</span><b id="opsModel">${esc((modules.LLM || {}).selected || 'LLM 未配置')}</b></div>
        <div class="ops-log" id="opsLog"></div>
        <div class="ops-input">
          <input type="text" id="opsIn" placeholder="和 ${esc(agent.name)} 对话，或问：设备状态 / 模型状态 / 现在能做什么">
          <button class="btn" id="opsSend">发送</button>
        </div>
        <div class="quickbar">
          <button data-q="查看设备状态">设备状态</button>
          <button data-q="模型状态">模型状态</button>
          <button data-q="现在能做什么">能力概览</button>
          <button data-q="打开智能体会话">智能体会话</button>
        </div>
      </div>
      <aside class="ops-side">
        <div class="ops-card">
          <div class="ops-head"><span>Intelligence</span><b>live context</b></div>
          <div id="intelRows">${intelligenceRows(agent, devices, status)}</div>
        </div>
        <div class="ops-card">
          <div class="ops-head"><span>Devices</span><b>${mine.length}</b></div>
          <div class="device-stack">
            ${mine.length ? mine.map(d => `<div class="dev-tile"><div><b>${esc(d.name || '未命名设备')}</b><span>${esc(d.mac)}</span></div><em class="${d.last_seen ? 'ok' : ''}">${timeAgo(d.last_seen)}</em></div>`).join('') : '<div class="hint">当前智能体还没有绑定设备。进入设置模式绑定新设备。</div>'}
          </div>
        </div>
        <div class="ops-card">
          <div class="ops-head"><span>Model Stack</span><b>agent scoped</b></div>
          <div class="sys-grid">
            ${['ASR', 'LLM', 'TTS', 'VAD', 'Intent', 'Memory'].map(k => moduleChip(modules, k)).join('')}
          </div>
        </div>
      </aside>
    </section>
  </div>`;

  const pick = $('#opsAgentPick');
  agents.forEach(a => pick.appendChild(new Option(a.name, a.id)));
  pick.value = agent.id;
  pick.onchange = () => location.hash = '#/terminal/' + pick.value;

  const add = (role, text) => {
    const line = el('div', 'ops-msg ' + role);
    line.innerHTML = `<span>${role === 'user' ? 'YOU' : 'EV'}</span><p>${esc(text)}</p>`;
    $('#opsLog').appendChild(line); $('#opsLog').scrollTop = $('#opsLog').scrollHeight;
  };
  add('assistant', `${agent.name} 已就绪。智能体引擎已经接入当前配置，右侧会持续显示设备、模型和上下文状态。`);
  const localReply = q => {
    if (/语音|终端|voice|麦克风|摄像头/i.test(q)) return '智能体已经在主画面中。点击启动后，允许麦克风/摄像头权限即可进入实时会话。';
    if (/设备|device/i.test(q)) return mine.length ? mine.map(d => `${d.name || '未命名设备'}：${d.mac}，最近连接 ${timeAgo(d.last_seen)}`).join('\n') : '当前智能体还没有绑定设备。进入设置模式可以用绑定码添加设备。';
    if (/模型|model|配置/i.test(q)) return ['ASR', 'LLM', 'TTS', 'VAD', 'Intent', 'Memory'].map(k => `${k}: ${(modules[k] || {}).selected || '未配置'}`).join('\n');
    if (/能做|能力|状态/i.test(q)) return `我可以显示 ${agent.name} 的运行状态、设备归属、模型链路、MCP 接入，并把普通对话转给当前 LLM。语音、摄像头和形象会话都在默认智能体页面启动。`;
    return null;
  };
  const send = async text => {
    const q = (text || $('#opsIn').value).trim(); if (!q) return;
    $('#opsIn').value = ''; add('user', q);
    const local = localReply(q);
    if (local) { add('assistant', local); if (/语音|终端|voice|麦克风|摄像头/i.test(q)) $('#voiceDock').scrollIntoView({ behavior: 'smooth', block: 'start' }); return; }
    add('assistant', '正在调用当前智能体的大脑…');
    try {
      const j = await api('/api/agents/' + agent.id + '/chat', { method: 'POST', body: JSON.stringify({ message: q, history: [] }) });
      const last = $$('#opsLog .ops-msg.assistant').pop();
      if (j.ok) {
        last.querySelector('p').textContent = j.reply;
        speakText(modules.TTS, j.reply, last);
      } else last.querySelector('p').textContent = j.error || '调用失败';
    } catch (e) { const last = $$('#opsLog .ops-msg.assistant').pop(); last.querySelector('p').textContent = e.message; }
  };
  $('#opsSend').onclick = () => send();
  $('#opsIn').onkeydown = e => { if (e.key === 'Enter') send(); };
  $$('.quickbar button').forEach(b => b.onclick = () => send(b.dataset.q));
  $('#refreshDock').onclick = () => renderAgentTerminal(agent.id);
  renderAgentTerminal(agent.id);
  try { if (avatar.model) { await ensureLive2D(); await mountLive2D(avatar.model); } } catch (e) { $('.avatar-status').textContent = 'avatar load failed · ' + e.message; }
}

// ---------- agents ----------
async function renderAgents(v) {
  setCrumb(['设置模式', '智能体']);
  const { agents } = await api('/api/agents');
  v.innerHTML = `<div class="page-head">
      <div><div class="kicker">Agents</div><h1 class="t">智能体</h1>
        <div class="d">每个智能体是一套独立的模型组合与人设，并管理绑定到它名下的设备。</div></div>
      <button class="btn" id="newA">＋ 新建智能体</button></div>
    <div id="list"></div>`;
  $('#newA').onclick = async () => { const { id } = await api('/api/agents', { method: 'POST', body: JSON.stringify({ name: '新智能体' }) }); location.hash = '#/terminal/' + id + '/setup'; };
  const list = $('#list');
  if (!agents.length) { list.innerHTML = `<div class="empty"><div class="big">还没有智能体</div><div class="mono">点右上角新建一个</div></div>`; return; }
  const rows = el('div', 'rows');
  agents.forEach((a, i) => {
    const mods = a.modules || {};
    const sum = ['LLM', 'ASR', 'TTS'].map(t => (mods[t] || {}).selected).filter(Boolean);
    const r = el('div', 'r');
    r.innerHTML = `<div class="id">${String(i + 1).padStart(2, '0')}</div>
      <div><div class="nm"><span class="av">◆</span>${esc(a.name)}</div>
        <div class="sub" style="margin-top:6px">${sum.map(x => `<span>${esc(x)}</span>`).join('') || '<span>未配置模型</span>'}</div></div>
      <div class="led ${a.device_count ? 'ok' : ''}">${a.device_count} 台设备</div>
      <div class="go">›</div>`;
    r.onclick = () => location.hash = '#/terminal/' + a.id + '/setup';
    rows.appendChild(r);
  });
  list.appendChild(rows);
}

// ---------- agent editor ----------
function enhanceSelects(root = document) {
  if (!window._museSelectCloseBound) {
    window._museSelectCloseBound = true;
    document.addEventListener('click', () => {
      $$('.muse-select.open').forEach(w => {
        w.classList.remove('open');
        w.querySelector('.muse-select-menu')?.classList.remove('open');
      });
    });
  }
  const scope = root && root.querySelectorAll ? root : document;
  scope.querySelectorAll('select:not(.muse-select-native)').forEach(sel => {
    if (sel.closest('.muse-select')) return;
    const wrap = el('div', 'muse-select');
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    sel.classList.add('muse-select-native');
    const btn = el('button', 'muse-select-btn');
    btn.type = 'button';
    btn.setAttribute('aria-haspopup', 'listbox');
    const menu = el('div', 'muse-select-menu');
    menu.setAttribute('role', 'listbox');
    wrap.appendChild(btn);
    wrap.appendChild(menu);
    const syncBtn = () => {
      const opt = sel.options[sel.selectedIndex];
      btn.textContent = opt ? opt.textContent : '请选择';
      btn.disabled = !!sel.disabled;
    };
    const buildMenu = () => {
      menu.innerHTML = '';
      [...sel.options].forEach((opt, i) => {
        const needsConfig = opt.dataset.configured === 'false';
        const item = el(
          'button',
          'muse-select-item'
            + (i === sel.selectedIndex ? ' active' : '')
            + (needsConfig ? ' needs-config' : ''),
        );
        item.type = 'button';
        item.setAttribute('role', 'option');
        item.textContent = opt.textContent;
        item.title = needsConfig ? '请先填写必需参数，保存后即可切换' : '已配置，可以直接切换';
        item.onclick = e => {
          e.stopPropagation();
          sel.selectedIndex = i;
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          syncBtn();
          menu.classList.remove('open');
          wrap.classList.remove('open');
          buildMenu();
        };
        menu.appendChild(item);
      });
    };
    sel._museRefresh = () => { syncBtn(); buildMenu(); };
    syncBtn();
    buildMenu();
    btn.onclick = e => {
      e.stopPropagation();
      $$('.muse-select.open').forEach(w => {
        if (w === wrap) return;
        w.classList.remove('open');
        w.querySelector('.muse-select-menu')?.classList.remove('open');
      });
      const open = !menu.classList.contains('open');
      menu.classList.toggle('open', open);
      wrap.classList.toggle('open', open);
    };
    sel.addEventListener('change', () => { syncBtn(); buildMenu(); });
  });
}

function wireSetupSections() {
  const nav = $('.setup-nav');
  if (!nav) return;
  const panes = $$('.setup-pane');
  nav.querySelectorAll('[data-section]').forEach(btn => {
    btn.onclick = () => {
      nav.querySelectorAll('[data-section]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const key = btn.dataset.section;
      panes.forEach(p => p.classList.toggle('active', p.id === 'sec-' + key));
      // 离开设备页停掉摄像头预览流
      if (key !== 'devices' && window._camLiveClean) {
        try { window._camLiveClean(); } catch (_) {}
        window._camLiveClean = null;
      }
    };
  });
}


function setupStackHtml(modules) {
  return ['ASR', 'LLM', 'TTS', 'Memory', 'Intent', 'VAD'].map(k => {
    const v = (modules[k] || {}).selected || '未配置';
    return `<div class="setup-stack-item"><span>${k}</span><b>${esc(v)}</b></div>`;
  }).join('');
}

function renderSetupAgentList(agents, currentId) {
  if (!agents.length) return '<p class="setup-dock-empty">还没有智能体</p>';
  return agents.map(a => `<button type="button" class="setup-agent-item${String(a.id) === String(currentId) ? ' active' : ''}" data-agent-id="${a.id}">
    <span class="setup-agent-name">${esc(a.name)}</span>
    <span class="setup-agent-meta">${a.device_count || 0} 台设备</span>
  </button>`).join('');
}

function wireSetupDock(currentId, agents, modules) {
  $('#setupNewAgent')?.addEventListener('click', async () => {
    const { id } = await api('/api/agents', { method: 'POST', body: JSON.stringify({ name: '新智能体' }) });
    location.hash = '#/terminal/' + id + '/setup';
  });
  $$('.setup-agent-item').forEach(btn => {
    btn.onclick = () => {
      const next = btn.dataset.agentId;
      if (String(next) === String(currentId)) return;
      location.hash = '#/terminal/' + next + '/setup';
    };
  });
  const stack = $('#setupStack');
  if (stack) stack.innerHTML = setupStackHtml(modules);
}

function wireSetupMemory(agentId, initialRaw) {
  const list = $('#setupMemoryList');
  const input = $('#setupMemoryInput');
  const st = $('#setupMemoryStatus');
  const dossierView = $('#setupDossierView');
  const dossierSt = $('#setupDossierStatus');
  if (!list) return;
  let items = [];

  const setStatus = (t) => { if (st) st.textContent = t || ''; };
  const setDossierStatus = (t) => { if (dossierSt) dossierSt.textContent = t || ''; };

  let dossierSnapshot = null;

  const paintDossier = (dossier) => {
    if (!dossierView) return;
    dossierSnapshot = dossier || {};
    dossierView.value = JSON.stringify(dossierSnapshot, null, 2);
  };

  const loadDossier = async () => {
    if (!dossierView) return;
    setDossierStatus('加载中…');
    try {
      const j = await api('/api/agents/' + agentId + '/dossier');
      paintDossier(j.dossier || {});
      setDossierStatus('');
    } catch (e) {
      dossierView.value = '';
      setDossierStatus('加载失败');
    }
  };

  const saveDossier = async () => {
    if (!dossierView) return;
    let parsed;
    try {
      parsed = JSON.parse(dossierView.value || '{}');
    } catch (e) {
      setDossierStatus('JSON 无效');
      toast('档案 JSON 格式不对');
      return;
    }
    setDossierStatus('保存中…');
    try {
      const j = await api('/api/agents/' + agentId + '/dossier', {
        method: 'PUT',
        body: JSON.stringify({ dossier: parsed }),
      });
      paintDossier(j.dossier || parsed);
      setDossierStatus('已保存');
      toast('档案已保存');
    } catch (e) {
      setDossierStatus('保存失败');
      toast('保存失败：' + e.message);
    }
  };

  $('#setupDossierRefresh')?.addEventListener('click', loadDossier);
  $('#setupDossierSave')?.addEventListener('click', saveDossier);
  dossierView?.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 's') {
      e.preventDefault();
      saveDossier();
    }
  });
  $('#setupDossierClear')?.addEventListener('click', async () => {
    if (!confirm('清空运行时档案（用户画像/相处状态/事件看板）？人设与事实条不受影响。')) return;
    try {
      await api('/api/agents/' + agentId + '/dossier', {
        method: 'PUT',
        body: JSON.stringify({ dossier: {} }),
      });
      await loadDossier();
      toast('档案已清空');
    } catch (e) {
      toast('清空失败：' + e.message);
    }
  });
  loadDossier();

  const paint = (next) => {
    items = Array.isArray(next) ? next.slice() : [];
    if (!items.length) {
      list.innerHTML = '<p class="setup-memory-empty">还没有记忆。在下方添加一条，或聊完语音后自动生成。</p>';
      return;
    }
    list.innerHTML = items.map(it => `<div class="setup-memory-item" data-id="${esc(it.id)}">
      <input type="text" class="setup-memory-text" value="${esc(it.text)}" maxlength="200" spellcheck="false">
      <button type="button" class="btn link setup-memory-del" title="删除">删除</button>
    </div>`).join('');
    list.querySelectorAll('.setup-memory-item').forEach(row => {
      const id = row.dataset.id;
      const field = row.querySelector('.setup-memory-text');
      field.addEventListener('change', async () => {
        const text = field.value.trim();
        if (!text) { field.value = (items.find(i => i.id === id) || {}).text || ''; return; }
        setStatus('保存中…');
        try {
          const j = await api('/api/agents/' + agentId + '/memory/items/' + encodeURIComponent(id), {
            method: 'PUT', body: JSON.stringify({ text })
          });
          paint(j.items || []);
          setStatus('已保存');
        } catch (e) {
          setStatus('保存失败');
          toast('保存失败：' + e.message);
        }
      });
      row.querySelector('.setup-memory-del').onclick = async () => {
        setStatus('删除中…');
        try {
          const j = await api('/api/agents/' + agentId + '/memory/items/' + encodeURIComponent(id), { method: 'DELETE' });
          paint(j.items || []);
          setStatus('');
          toast('已删除');
        } catch (e) {
          setStatus('删除失败');
          toast('删除失败：' + e.message);
        }
      };
    });
  };

  // 初始：可能是旧版整段文本，先走 API 规范化
  const boot = async () => {
    try {
      const j = await api('/api/agents/' + agentId + '/memory');
      paint(j.items || []);
    } catch (e) {
      // 回退：把 initialRaw 当文本展示为临时条目（仅 UI）
      if (initialRaw) {
        paint([{ id: 'tmp', text: String(initialRaw).slice(0, 200) }]);
      } else {
        paint([]);
      }
    }
  };
  boot();

  const add = async () => {
    const text = (input?.value || '').trim();
    if (!text) return;
    setStatus('添加中…');
    try {
      const j = await api('/api/agents/' + agentId + '/memory/items', {
        method: 'POST', body: JSON.stringify({ text })
      });
      paint(j.items || []);
      if (input) input.value = '';
      setStatus('已添加');
      toast('已添加');
    } catch (e) {
      setStatus('添加失败');
      toast('添加失败：' + e.message);
    }
  };
  $('#setupMemoryAdd')?.addEventListener('click', add);
  input?.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); add(); }
  });
  $('#setupMemoryRefresh')?.addEventListener('click', async () => {
    setStatus('加载中…');
    try {
      const j = await api('/api/agents/' + agentId + '/memory');
      paint(j.items || []);
      setStatus('');
      toast('已刷新');
    } catch (e) {
      setStatus('加载失败');
      toast('刷新失败：' + e.message);
    }
  });
  $('#setupMemoryClear')?.addEventListener('click', async () => {
    if (!confirm('清空该智能体的全部记忆？')) return;
    try {
      await api('/api/agents/' + agentId + '/memory', { method: 'DELETE' });
      paint([]);
      setStatus('');
      toast('记忆已清空');
    } catch (e) {
      toast('清空失败：' + e.message);
    }
  });
}

async function renderAgentEditor(v, id, opts = {}) {
  if (!opts.immersive) {
    location.replace('#/terminal/' + id + '/setup');
    return;
  }
  const [a, dev, { agents }, status] = await Promise.all([
    api('/api/agents/' + id),
    api('/api/devices'),
    api('/api/agents'),
    api('/api/status')
  ]);
  const modules = a.modules || {};
  const mine = (dev.devices || []).filter(d => String(d.agent_id) === String(id));

  v.innerHTML = `<div class="agent-setup">
    <div class="agent-setup-bg" aria-hidden="true"></div>
    <header class="setup-head">
      <a class="setup-back" href="#/terminal/${id}">← 返回</a>
      <input type="text" class="setup-title-input" id="f_name" value="${esc(a.name)}" aria-label="智能体名称" spellcheck="false">
      <div class="setup-head-actions">
        <span class="st" id="saveStatus"></span>
        <button type="button" class="btn setup-save" id="saveA">保存</button>
        <button type="button" class="btn ghost danger setup-del" id="delA">删除</button>
      </div>
    </header>
    <div class="setup-body">
      <aside class="setup-rail">
        <div class="setup-rail-stage" id="setupAvatarStage">
          <canvas id="setupVizCanvas" class="setup-viz-canvas"></canvas>
          <canvas id="setupLive2d" class="setup-live2d hidden"></canvas>
          <div class="setup-avatar-cap" id="setupAvatarCap">声波</div>
        </div>
        <label class="setup-rail-label">展示形象</label>
        <select id="f_avatar" class="setup-rail-avatar" aria-label="展示形象"></select>
        <nav class="setup-nav" aria-label="设置分区">
          <button type="button" data-section="persona" class="active">人设</button>
          <button type="button" data-section="models">模型</button>
          <button type="button" data-section="devices">设备</button>
          <button type="button" data-section="advanced">连接</button>
        </nav>
      </aside>
      <main class="setup-main">
        <section class="setup-pane active" id="sec-persona">
          <div class="setup-pane-head">
            <h2>人设</h2>
            <p>系统提示词决定说话风格、能力边界与回复习惯。</p>
          </div>
          <textarea id="f_prompt" class="setup-prompt" placeholder="例如：你是简洁友好的语音助手，优先用口语回答…">${esc(a.prompt || '')}</textarea>
        </section>
        <section class="setup-pane" id="sec-models">
          <div class="setup-pane-head">
            <h2>模型</h2>
            <p>语音链路各模块的 Provider 与参数，展开后检测或试听。</p>
          </div>
          <div class="setup-sync-note">
            <b>设置同步</b>
            <span>摄像头 ASR 最迟 2 秒切换，LLM/TTS 下一轮对话生效；EV 终端返回主界面后自动重连并读取新配置。</span>
          </div>
          <div id="modules" class="setup-modules"></div>
        </section>
        <section class="setup-pane" id="sec-devices">
          <div class="setup-pane-head">
            <h2>设备</h2>
            <p>本机与已接入硬件按能力归类。ESP32 在底部用绑定码接入。</p>
          </div>
          <div id="agentDevices"></div>
        </section>
        <section class="setup-pane" id="sec-advanced">
          <div class="setup-pane-head">
            <h2>连接与技能</h2>
            <p>第一期技能以「网页搜索」为准；密钥与强弱在 <a href="#/settings">系统设置 → 技能</a> 配置。核心 WS 会话仍可用下方勾选。</p>
          </div>
          <div class="setup-field">
            <label>技能插件</label>
            <div class="chips setup-chips" id="plugins"></div>
            <span class="hint">试聊 / PC 语音走 EV 搜索引擎；此处勾选主要影响核心语音 WebSocket。</span>
          </div>
          <details class="setup-field" style="margin-top:12px">
            <summary style="cursor:pointer;color:var(--muted)">高级 · MCP 接入点（可选）</summary>
            <label for="f_mcp" style="display:block;margin-top:8px">MCP 接入点</label>
            <input type="text" id="f_mcp" value="${esc(a.mcp_endpoint || '')}" placeholder="wss://.../mcp/ 留空则不启用">
            <span class="hint">留空即可。通用 MCP 注册表第二期再做。</span>
          </details>
        </section>
      </main>
      <aside class="setup-dock">
        <section class="setup-dock-block">
          <h3 class="setup-dock-title">服务器核心</h3>
          <div class="setup-core-card">
            <div class="setup-core-row">
              <span class="led ${status.core_up ? 'ok' : 'err'}">${status.core_up ? '在线' : '离线'}</span>
              <div><b>server</b><span>127.0.0.1:8000 · 语音 WebSocket</span></div>
            </div>
            <div class="setup-core-row">
              <span class="led ok">在线</span>
              <div><b>EV</b><span>127.0.0.1:8002 · 配置与设备</span></div>
            </div>
            <p class="setup-dock-hint">核心需配置 <code class="k">manager-api.url</code> 指向 EV 控制服务。Secret 见 <a href="#/settings">系统设置</a>。</p>
          </div>
        </section>
        <section class="setup-dock-block">
          <div class="setup-dock-head">
            <h3 class="setup-dock-title">智能体</h3>
            <button type="button" class="btn sm setup-new-agent" id="setupNewAgent">＋ 新建</button>
          </div>
          <div class="setup-agent-list" id="setupAgentList">${renderSetupAgentList(agents, id)}</div>
        </section>
        <section class="setup-dock-block">
          <h3 class="setup-dock-title">当前链路</h3>
          <div class="setup-stack" id="setupStack">${setupStackHtml(modules)}</div>
          <p class="setup-dock-hint">本智能体 ${mine.length} 台设备</p>
        </section>
      </aside>
    </div>
  </div>`;

  const av = $('#f_avatar');
  (BOOT.avatars || []).forEach(x => av.appendChild(new Option(x.label || x.name, x.name)));
  av.value = a.avatar || 'visualizer';
  av.onchange = () => wireSetupAvatarPreview(av.value);
  // Wire navigation first so a later paint failure never leaves tabs dead.
  wireSetupSections();
  wireSetupDock(id, agents, modules);
  try {
    const pluginList = Array.isArray(a.plugins)
      ? a.plugins
      : ((a.plugins && Array.isArray(a.plugins.enabled)) ? a.plugins.enabled : []);
    const active = new Set(pluginList);
    const pc = $('#plugins');
    // 第一期 UI 只强调网页搜索；其它插件仍保留在保存结果里，不在此摆一排假可用项
    const SHOW_PLUGINS = ['web_search'];
    pc.dataset.hiddenPlugins = JSON.stringify(pluginList.filter(f => !SHOW_PLUGINS.includes(f)));
    const funcs = (BOOT.functions || []).filter(f => SHOW_PLUGINS.includes(f));
    (funcs.length ? funcs : ['web_search']).forEach(f => {
      const l = el('label', 'chk');
      const label = PLUGIN_LABELS[f] || f;
      l.innerHTML = `<input type="checkbox" value="${f}" ${active.has(f) ? 'checked' : ''}><span class="plugin-name">${esc(label)}</span><span class="plugin-code">${esc(f)}</span>`;
      pc.appendChild(l);
    });
    BOOT.module_types.forEach(mt => $('#modules').appendChild(moduleBlock(mt, modules[mt] || {})));
    renderAgentDevices(id, dev.devices || []);
    wireSetupMemory(id, a.summary_memory || '');
    enhanceSelects(v);
  } catch (paintErr) {
    console.error('[EV setup paint]', paintErr);
    toast('部分设置未能加载：' + (paintErr.message || paintErr));
  }

  $('#saveA').onclick = () => saveAgent(id);
  $('#delA').onclick = async () => {
    if (!confirm('删除该智能体？绑定的设备会解绑。')) return;
    await api('/api/agents/' + id, { method: 'DELETE' });
    location.hash = '#/terminal';
  };
  stopSetupAvatarPreview();
  wireSetupAvatarPreview(av.value);
  window.addEventListener('hashchange', stopSetupAvatarPreview, { once: true });
}

const DEVICE_CAP_UI = {
  sensor: [
    { id: 'mic', label: '麦克风', always: true },
    { id: 'ir', label: '红外', always: false },
    { id: 'ultrasonic', label: '超声波', always: false },
  ],
  actuator: [
    { id: 'speaker', label: '扬声器', always: true },
    { id: 'display', label: '显示', always: false },
    { id: 'servo', label: '舵机', always: false },
  ],
};

const CAP_LABEL = {
  mic: '麦克风', ir: '红外', ultrasonic: '超声波', imu: 'IMU', temp: '温湿度',
  speaker: '扬声器', display: '显示', servo: '舵机', led: '灯带',
};

/** muse: / 测试 MAC / web_test 等一律不当真设备 */
function isPlaceholderDevice(d) {
  if (!d) return true;
  if (d.placeholder) return true;
  const mac = String(d.mac || '');
  const cid = String(d.client_id || '');
  const name = String(d.name || '');
  if (mac.startsWith('muse:')) return true;
  if (/^aa:bb:cc/i.test(mac)) return true;
  if (/test|dummy|fake|placeholder/i.test(mac)) return true;
  if (/test|dummy|fake|web_test/i.test(cid)) return true;
  if (/测试|占位|placeholder/i.test(name)) return true;
  return false;
}

/** 仅真·ESP32 瘦客户端才走绑定码（标准 MAC，非 muse/测试） */
function isEsp32Bindable(d) {
  if (!d || isPlaceholderDevice(d)) return false;
  if (String(d.device_type) !== 'thin_client') return false;
  const mac = String(d.mac || '');
  return /^([0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(mac);
}

function deviceCapabilities(d) {
  if (isPlaceholderDevice(d)) return [];
  if (Array.isArray(d.capabilities) && d.capabilities.length) return d.capabilities.slice();
  const t = String(d.device_type || '').toLowerCase();
  const mac = String(d.mac || '');
  if (t === 'camera' || mac.startsWith('camera:')) return ['mic'];
  if (t === 'speaker') return ['speaker'];
  if (t === 'edge' || t === 'edge_agent') return [];
  if (t === 'thin_client' && isEsp32Bindable(d)) return ['mic', 'speaker'];
  return [];
}

function effectiveCapabilities(d) {
  let caps = deviceCapabilities(d);
  if (String(d.device_type) === 'camera' && d.io_status) {
    const io = d.io_status;
    caps = caps.filter(c => (c === 'mic' ? !!io.mic : true));
  }
  return caps;
}

function isCapEnabled(d, cap) {
  const dis = d.disabled_capabilities || (d.metadata && d.metadata.disabled_capabilities) || [];
  return !dis.map(String).includes(String(cap));
}

function capRoleOf(d, cap) {
  const t = String(d.device_type || '').toLowerCase();
  const isCam = t === 'camera' || String(d.mac || '').startsWith('camera:');
  if (cap === 'mic') {
    if (isCam) return { role: '摄像头麦', hint: (d.io_status && d.io_status.detail) || '' };
    if (isEsp32Bindable(d)) return { role: 'ESP32 麦', hint: '' };
    return { role: '麦克风', hint: '' };
  }
  if (cap === 'speaker') {
    if (t === 'speaker') return { role: '网络扬声器', hint: '' };
    if (isEsp32Bindable(d)) return { role: 'ESP32 喇叭', hint: '' };
    return { role: '扬声器', hint: '' };
  }
  return { role: CAP_LABEL[cap] || cap, hint: '' };
}

function renderHwEndpoints(list, cap) {
  if (!list.length) return '';
  return list.map(d => {
    const role = capRoleOf(d, cap);
    const on = isCapEnabled(d, cap);
    const io = d.io_status || null;
    let linked = !!(d.last_seen || d.online);
    let status = '待连接';
    if (String(d.device_type) === 'camera') {
      if (io) {
        if (cap === 'mic') linked = !!io.mic;
        else linked = !!io.online;
        if (io.online && linked) status = '可用';
        else if (io.online) status = '在线';
        else status = io.detail || '离线';
      } else {
        // 未探测时先标检测中，由实时预览再改写状态
        linked = true;
        status = '检测中';
      }
    } else if (String(d.device_type) === 'speaker') {
      linked = !!d.online;
      status = d.online ? (on ? '在线' : '关闭') : '离线';
    } else {
      status = linked ? '在线' : '待连接';
    }
    if (!on) status = '关闭';
    const title = d.name || d.mac || '设备';
    return `<div class="io-row ${on ? '' : 'is-off'}" data-id="${d.id}" data-cam-row="${String(d.device_type) === 'camera' ? d.id : ''}" title="${esc(d.mac || '')}">
      <span class="io-dot ${on && linked ? 'ok' : ''}"></span>
      <div class="io-row-text">
        <span class="io-row-name">${esc(title)}</span>
        <span class="io-row-sub">${esc(role.role)}</span>
      </div>
      <span class="io-row-state" data-cam-state="${String(d.device_type) === 'camera' ? d.id : ''}">${esc(status)}</span>
      <button type="button" class="io-sw ${on ? 'on' : ''}" data-cap-tog="${d.id}" data-cap="${cap}" aria-pressed="${on}" aria-label="开关"></button>
      <div class="io-row-more">
        <button type="button" class="btn link" data-rn="${d.id}">改名</button>
        <button type="button" class="btn link" data-ub="${d.id}">解绑</button>
      </div>
    </div>`;
  }).join('');
}

function renderAgentDevices(agentId, allDevices) {
  const box = $('#agentDevices'); if (!box) return;
  const aid = String(agentId);
  const mine = (allDevices || []).filter(d =>
    String(d.agent_id) === aid &&
    d.device_type !== 'edge' && d.device_type !== 'edge_agent' &&
    !isPlaceholderDevice(d) &&
    (d.device_type !== 'thin_client' || isEsp32Bindable(d))
  );
  const espPending = (allDevices || []).filter(d =>
    (d.agent_id == null || d.agent_id === '') &&
    isEsp32Bindable(d) &&
    d.bind_code
  );

  const byCap = {};
  mine.forEach(d => {
    effectiveCapabilities(d).forEach(cap => {
      (byCap[cap] || (byCap[cap] = [])).push(d);
    });
  });

  const group = (capDef) => {
    const cap = capDef.id;
    const hw = byCap[cap] || [];
    if (!capDef.always && !hw.length) return '';
    const isAudio = cap === 'mic' || cap === 'speaker';
    const tools = isAudio
      ? `<div class="io-tools">
           <button type="button" class="btn ghost" data-host-refresh="${cap}">刷新</button>
         </div>`
      : '';
    const host = isAudio
      ? `<div class="io-rows" id="${cap === 'mic' ? 'hostMicList' : 'hostSpkList'}">
           <div class="io-row is-empty"><span class="io-row-sub">检测中…</span></div>
         </div>
         <p class="setup-audio-hint${cap === 'mic' ? '' : ' hidden'}" id="${cap === 'mic' ? 'evAudioHint' : 'evSpkHint'}">${
           cap === 'mic'
             ? '由本机语音进程枚举麦克风，不弹浏览器权限。开关打开的麦会同时用于本机语音（可多开）。'
             : ''
         }</p>`
      : '';
    const hwHtml = renderHwEndpoints(hw, cap);
    return `<div class="io-group" data-cap="${cap}">
      <div class="io-group-h">
        <span>${esc(capDef.label)}</span>
        <span class="io-group-n" data-cap-count="${cap}"></span>
        ${tools}
      </div>
      ${host}
      ${hwHtml ? `<div class="io-rows">${hwHtml}</div>` : (!isAudio ? '<div class="io-row is-empty"><span class="io-row-sub">暂无设备</span></div>' : '')}
    </div>`;
  };

  box.innerHTML = `
    <div class="io-board">
      <div class="io-cols">
        <section class="io-col">
          <header class="io-col-h"><span>SENSOR</span><b>传感器</b></header>
          ${DEVICE_CAP_UI.sensor.map(group).join('')}
        </section>
        <section class="io-col">
          <header class="io-col-h"><span>ACTUATOR</span><b>执行器</b></header>
          ${DEVICE_CAP_UI.actuator.map(group).join('')}
        </section>
      </div>
      <section class="io-esp">
        <header class="io-col-h"><span>ESP32</span><b>对话终端</b></header>
        <p class="io-esp-hint">仅 xiaozhi / ESP32 使用绑定码</p>
        <div class="setup-bind">
          <input type="text" id="ad_code" placeholder="6 位绑定码" maxlength="6" inputmode="numeric" aria-label="ESP32 绑定码">
          <input type="text" id="ad_name" placeholder="名称（可选）" aria-label="设备名">
          <button type="button" class="btn" id="ad_bind">绑定</button>
        </div>
        <div class="msg setup-msg" id="ad_msg"></div>
        ${espPending.length ? `<div class="io-rows">${espPending.map(d => `
          <div class="io-row">
            <span class="io-dot"></span>
            <div class="io-row-text">
              <span class="io-row-name">${esc(d.name || d.mac)}</span>
              <span class="io-row-sub mono">码 ${esc(d.bind_code)}</span>
            </div>
            <button type="button" class="btn" data-attach="${d.id}">绑定</button>
          </div>`).join('')}</div>` : ''}
      </section>
    </div>
  `;

  const reload = async () => {
    const dev = await api('/api/devices');
    renderAgentDevices(agentId, dev.devices);
  };

  $('#ad_bind').onclick = async () => {
    const m = $('#ad_msg');
    try {
      await api('/api/devices/bind', {
        method: 'POST',
        body: JSON.stringify({
          bind_code: $('#ad_code').value.trim(),
          agent_id: +agentId,
          name: $('#ad_name').value.trim(),
        }),
      });
      toast('ESP32 已绑定');
      await reload();
    } catch (e) {
      m.textContent = '✗ ' + e.message;
      m.style.color = 'var(--err)';
    }
  };
  box.querySelectorAll('[data-attach]').forEach(b => {
    b.onclick = async () => {
      try {
        await api('/api/devices/' + b.dataset.attach + '/attach', {
          method: 'POST',
          body: JSON.stringify({ agent_id: +agentId }),
        });
        toast('ESP32 已绑定');
        await reload();
      } catch (e) { toast('绑定失败：' + e.message); }
    };
  });
  box.querySelectorAll('[data-ub]').forEach(b => {
    b.onclick = async () => {
      await api('/api/devices/' + b.dataset.ub + '/unbind', { method: 'POST' });
      toast('已解绑');
      await reload();
    };
  });
  box.querySelectorAll('[data-rn]').forEach(b => {
    b.onclick = async () => {
      const nm = prompt('新设备名');
      if (nm == null) return;
      await api('/api/devices/' + b.dataset.rn + '/rename', {
        method: 'POST',
        body: JSON.stringify({ name: nm }),
      });
      await reload();
    };
  });
  box.querySelectorAll('[data-cap-tog]').forEach(b => {
    b.onclick = async () => {
      const did = b.getAttribute('data-cap-tog') || b.dataset.capTog;
      const cap = b.getAttribute('data-cap') || b.dataset.cap;
      const next = b.getAttribute('aria-pressed') !== 'true';
      try {
        await api('/api/devices/' + did + '/capability', {
          method: 'POST',
          body: JSON.stringify({ capability: cap, enabled: next }),
        });
        toast(next ? '已开启' : '已关闭');
        await reload();
      } catch (e) {
        const msg = String(e.message || e);
        toast(/404|Not Found/i.test(msg)
          ? '切换失败：后端未更新，请重启 EV(:8002)'
          : ('切换失败：' + msg));
      }
    };
  });

  wireRealHostIo(box, byCap).catch(e => console.warn('host io', e));
}

async function wireRealHostIo(box, byCap) {
  const micCount = () => (byCap.mic || []).length;
  const spkCount = () => (byCap.speaker || []).length;

  const updateCount = (cap, hostN) => {
    const elc = box.querySelector(`[data-cap-count="${cap}"]`);
    if (!elc) return;
    const hw = cap === 'mic' ? micCount() : (cap === 'speaker' ? spkCount() : (byCap[cap] || []).length);
    const n = (hostN || 0) + hw;
    elc.textContent = n ? (n + ' 路') : '空';
  };

  // 先合并在线扬声器到 hardware 列表下方
  let liveSpeakers = [];
  try { liveSpeakers = (await api('/api/speakers')).speakers || []; } catch (_) {}

  const spkHostExtra = liveSpeakers.length
    ? liveSpeakers.map(s => `<div class="io-row">
        <span class="io-dot ${s.enabled ? 'ok' : ''}"></span>
        <div class="io-row-text">
          <span class="io-row-name">${esc(s.name || '网络扬声器')}</span>
          <span class="io-row-sub">网络 · ${esc(s.mac)}</span>
        </div>
        <span class="io-row-state">${s.enabled ? '播放中' : '关闭'}</span>
      </div>`).join('')
    : '';

  const fillHost = async () => {
    const micBox = $('#hostMicList');
    const spkBox = $('#hostSpkList');
    const hint = $('#evAudioHint');

    let usableMics = [];
    let usableSpks = [];
    let listError = '';
    try {
      const data = await api('/api/host-audio/devices');
      usableMics = (data.inputs || []).map(d => ({
        id: d.id,
        label: d.label || d.id,
        ok: d.ok !== false,
        pending: false,
      }));
      usableSpks = (data.outputs || []).map(d => ({
        id: d.id,
        label: d.label || d.id,
        ok: d.ok !== false,
        pending: false,
        note: '本机播放设备',
      }));
      if (!data.ok && data.error) listError = String(data.error);
    } catch (e) {
      listError = e.message || String(e);
    }

    EV_AUDIO.rememberCatalog(usableMics, usableSpks);

    // 把服务器里按「标签」禁用的项迁移到当前 PortAudio id（兼容旧浏览器 deviceId）
    const applyLabelDisable = (devices, labels, setter) => {
      const want = new Set((labels || []).map(x => String(x).trim().toLowerCase()).filter(Boolean));
      if (!want.size) return;
      devices.forEach(d => {
        const lab = String(d.label || '').trim().toLowerCase();
        if (lab && want.has(lab)) setter(d.id, true);
      });
    };
    if (EV_AUDIO._pendingMicLabelDisable) {
      applyLabelDisable(usableMics, EV_AUDIO._pendingMicLabelDisable, (id, off) => EV_AUDIO.setMicDisabled(id, off));
      EV_AUDIO._pendingMicLabelDisable = null;
    }
    if (EV_AUDIO._pendingSpkLabelDisable) {
      applyLabelDisable(usableSpks, EV_AUDIO._pendingSpkLabelDisable, (id, off) => EV_AUDIO.setSpkDisabled(id, off));
      EV_AUDIO._pendingSpkLabelDisable = null;
    }
    if (EV_AUDIO._pendingActiveMicLabels) {
      const wanted = new Set(EV_AUDIO._pendingActiveMicLabels.map(
        x => String(x).trim().toLowerCase()
      ).filter(Boolean));
      const selected = usableMics.find(
        d => wanted.has(String(d.label || '').trim().toLowerCase())
      );
      if (selected) EV_AUDIO.setExclusiveMic(selected.id, selected.label);
      EV_AUDIO._pendingActiveMicLabels = null;
    }

    // 已选设备拔掉则清空
    if (EV_AUDIO.mic && !usableMics.some(d => d.id === EV_AUDIO.mic && d.ok)) EV_AUDIO.setMic('');
    if (EV_AUDIO.spk && !usableSpks.some(d => d.id === EV_AUDIO.spk)) EV_AUDIO.setSpk('');

    const disIn = new Set(EV_AUDIO.disabledMics());
    const disOut = new Set(EV_AUDIO.disabledSpks());
    const activeMicId = EV_AUDIO.activeMicId();
    const micOn = (d) => d.id === activeMicId && !disIn.has(d.id);
    const spkOn = (d) => !disOut.has(d.id);

    if (micBox) {
      if (!usableMics.length) {
        micBox.innerHTML = `<div class="io-row is-empty"><span class="io-row-sub">${
          esc(listError || '未检测到本机麦克风')
        }</span></div>`;
      } else {
        micBox.innerHTML = usableMics.map(d => {
          const on = micOn(d);
          const status = on ? '语音中' : (disIn.has(d.id) ? '已关闭' : '可切换');
          return `<div class="io-row ${on ? '' : 'is-off'}${on && d.ok ? ' is-active' : ''}" data-host-mic="${esc(d.id)}">
            <span class="io-dot ${d.ok && on ? 'ok' : ''}"></span>
            <div class="io-row-text">
              <span class="io-row-name">${esc(d.label)}</span>
              <span class="io-row-sub">本机 · 单选</span>
            </div>
            <span class="io-row-state">${status}</span>
            <button type="button" class="io-sw ${on ? 'on' : ''}" data-host-mic-tog="${esc(d.id)}" aria-pressed="${on}" aria-label="开关"></button>
          </div>`;
        }).join('');
      }
    }

    if (spkBox) {
      const hostRows = usableSpks.length
        ? usableSpks.map(d => {
            const on = spkOn(d);
            const active = EV_AUDIO.spk === d.id
              || (!EV_AUDIO.spk && on && usableSpks[0]?.id === d.id);
            const status = on ? (active ? '使用中' : '可用') : '已关闭';
            return `<div class="io-row ${on ? '' : 'is-off'}${active && on ? ' is-active' : ''}" data-host-spk="${esc(d.id)}" title="${esc(d.note || d.id)}">
              <span class="io-dot ${on ? 'ok' : ''}"></span>
              <div class="io-row-text">
                <span class="io-row-name">${esc(d.label)}</span>
                <span class="io-row-sub">本机</span>
              </div>
              <span class="io-row-state">${status}</span>
              <button type="button" class="io-sw ${on ? 'on' : ''}" data-host-spk-tog="${esc(d.id)}" aria-pressed="${on}" aria-label="开关"></button>
              <button type="button" class="btn link" data-host-spk-use="${esc(d.id)}" ${!on ? 'disabled' : ''}>选用</button>
            </div>`;
          }).join('')
        : `<div class="io-row is-empty"><span class="io-row-sub">${esc(listError || '未枚举到本机扬声器')}</span></div>`;
      spkBox.innerHTML = hostRows + spkHostExtra;
    }

    if (hint) {
      if (listError) {
        hint.textContent = listError;
        hint.classList.remove('hidden');
      } else {
        hint.classList.add('hidden');
      }
    }
    updateCount('mic', usableMics.filter(d => d.ok).length);
    updateCount('speaker', usableSpks.length + liveSpeakers.length);

    const notifyEngine = () => {
      // 本机语音读 DB 开关即可；不再要求浏览器重绑麦
      resumeVoiceSession(window.MUSE_SURFACE_UI);
    };

    box.querySelectorAll('[data-host-mic-tog]').forEach(b => {
      b.onclick = () => {
        const id = b.getAttribute('data-host-mic-tog');
        const next = b.getAttribute('aria-pressed') !== 'true';
        if (next) EV_AUDIO.setExclusiveMic(id, EV_AUDIO.catalog.mic[id] || '');
        else EV_AUDIO.disableAllMics();
        fillHost();
        notifyEngine();
        toast(next ? '已切换本机语音输入' : '已关闭本机语音输入');
      };
    });
    box.querySelectorAll('[data-host-spk-tog]').forEach(b => {
      b.onclick = () => {
        const id = b.getAttribute('data-host-spk-tog');
        const next = b.getAttribute('aria-pressed') !== 'true';
        EV_AUDIO.setSpkDisabled(id, !next);
        if (!next && EV_AUDIO.spk === id) EV_AUDIO.setSpk('');
        fillHost();
        notifyEngine();
        toast(next ? '已开启本机喇叭' : '已关闭本机喇叭');
      };
    });
    box.querySelectorAll('[data-host-spk-use]').forEach(b => {
      b.onclick = () => {
        const id = b.getAttribute('data-host-spk-use');
        EV_AUDIO.setSpk(id, EV_AUDIO.catalog.spk[id] || '');
        fillHost();
        notifyEngine();
        toast('已选用该扬声器');
      };
    });
  };

  box.querySelectorAll('[data-host-refresh]').forEach(b => {
    b.onclick = () => fillHost();
  });

  // 拉取服务器已存的禁用标签，在首次枚举时落到 PortAudio id
  try {
    const prefs = await api('/api/host-audio');
    EV_AUDIO._pendingMicLabelDisable = prefs.disabled_mic_labels || [];
    EV_AUDIO._pendingSpkLabelDisable = prefs.disabled_spk_labels || [];
    EV_AUDIO._pendingActiveMicLabels = prefs.active_mic_labels || [];
  } catch (_) { /* ignore */ }
  await fillHost();
  void EV_AUDIO.syncToServer();
}

async function wireAgentSpeakerLive() { /* merged into wireRealHostIo */ }

async function wireLocalAudioDevicePanel() { /* replaced by wireRealHostIo */ }


const EV_AUDIO = {
  micKey: 'ev_audio_input_id',
  spkKey: 'ev_audio_output_id',
  disMicKey: 'ev_audio_disabled_inputs',
  disSpkKey: 'ev_audio_disabled_outputs',
  micLabelKey: 'ev_audio_input_label',
  spkLabelKey: 'ev_audio_output_label',
  catalog: { mic: {}, spk: {} },
  get mic() { return localStorage.getItem(this.micKey) || ''; },
  get spk() { return localStorage.getItem(this.spkKey) || ''; },
  get micLabel() { return localStorage.getItem(this.micLabelKey) || this.catalog.mic[this.mic] || ''; },
  get spkLabel() { return localStorage.getItem(this.spkLabelKey) || this.catalog.spk[this.spk] || ''; },
  setMic(id, label) {
    if (id) localStorage.setItem(this.micKey, id); else localStorage.removeItem(this.micKey);
    const lab = label || this.catalog.mic[id] || '';
    if (lab) localStorage.setItem(this.micLabelKey, lab); else localStorage.removeItem(this.micLabelKey);
    void this.syncToServer();
  },
  setSpk(id, label) {
    if (id) localStorage.setItem(this.spkKey, id); else localStorage.removeItem(this.spkKey);
    const lab = label || this.catalog.spk[id] || '';
    if (lab) localStorage.setItem(this.spkLabelKey, lab); else localStorage.removeItem(this.spkLabelKey);
    void this.syncToServer();
  },
  _parse(key) {
    try { return JSON.parse(localStorage.getItem(key) || '[]') || []; } catch (_) { return []; }
  },
  disabledMics() { return this._parse(this.disMicKey); },
  disabledSpks() { return this._parse(this.disSpkKey); },
  activeMicId() {
    const known = Object.keys(this.catalog.mic || {});
    const disabled = new Set(this.disabledMics());
    if (this.mic && known.includes(this.mic) && !disabled.has(this.mic)) return this.mic;
    return known.find(id => !disabled.has(id)) || '';
  },
  setExclusiveMic(id, label) {
    // 单选由 active_mic_ids 表达；不要把其它设备永久禁用，否则 agent 无法切过去。
    const disabled = new Set(this.disabledMics());
    disabled.delete(id);
    localStorage.setItem(this.disMicKey, JSON.stringify([...disabled]));
    this.setMic(id, label);
  },
  disableAllMics() {
    localStorage.setItem(this.disMicKey, JSON.stringify(Object.keys(this.catalog.mic || {})));
    this.setMic('');
  },
  setMicDisabled(id, disabled) {
    const s = new Set(this.disabledMics());
    if (disabled) s.add(id); else s.delete(id);
    localStorage.setItem(this.disMicKey, JSON.stringify([...s]));
    void this.syncToServer();
  },
  setSpkDisabled(id, disabled) {
    const s = new Set(this.disabledSpks());
    if (disabled) s.add(id); else s.delete(id);
    localStorage.setItem(this.disSpkKey, JSON.stringify([...s]));
    void this.syncToServer();
  },
  rememberCatalog(mics, spks) {
    this.catalog.mic = {};
    this.catalog.spk = {};
    (mics || []).forEach(d => { if (d && d.id) this.catalog.mic[d.id] = d.label || d.id; });
    (spks || []).forEach(d => { if (d && d.id) this.catalog.spk[d.id] = d.label || d.id; });
  },
  async syncToServer() {
    const micIds = this.disabledMics();
    const spkIds = this.disabledSpks();
    const activeMicId = this.activeMicId();
    const activeMicIds = activeMicId ? [activeMicId] : [];
    const payload = {
      mic_id: activeMicId,
      mic_label: this.catalog.mic[activeMicId] || '',
      disabled_mic_ids: micIds,
      disabled_mic_labels: micIds.map(id => this.catalog.mic[id] || id).filter(Boolean),
      active_mic_ids: activeMicIds,
      active_mic_labels: activeMicIds.map(id => this.catalog.mic[id] || id).filter(Boolean),
      spk_id: this.spk,
      spk_label: this.spkLabel || this.catalog.spk[this.spk] || '',
      disabled_spk_ids: spkIds,
      disabled_spk_labels: spkIds.map(id => this.catalog.spk[id] || id).filter(Boolean),
    };
    try {
      await api('/api/host-audio', { method: 'PUT', body: JSON.stringify(payload) });
    } catch (e) {
      console.warn('[EV] sync host-audio', e);
    }
  },
};


const _chatAudio = new Audio();
const speakText = async (ttsMod, text, bubble) => {
  if (!ttsMod || !ttsMod.selected) return;
  if (bubble) bubble.classList.add('speaking');
  try {
    const disabled = new Set(EV_AUDIO.disabledSpks());
    const spk = EV_AUDIO.spk;
    if ((spk && disabled.has(spk)) || (!spk && disabled.has('default') && disabled.size)) {
      throw new Error('本机扬声器已在设备页关闭');
    }
    _chatAudio.pause();
    const r = await fetch('/api/tts/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: ttsMod.selected, overrides: ttsMod.overrides || {}, text }) });
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || r.status); }
    const b = await r.blob(); _chatAudio.src = URL.createObjectURL(b);
    if (spk && typeof _chatAudio.setSinkId === 'function') {
      try { await _chatAudio.setSinkId(spk); } catch (_) { /* ignore */ }
    }
    await _chatAudio.play();
  } catch (e) { toast('朗读失败：' + e.message); }
  finally { if (bubble) bubble.classList.remove('speaking'); }
};

function wireChat(id, agent) {
  const log = $('#chatLog'), input = $('#chatIn'); if (!log) return;
  const hist = [];
  const ttsMod = (agent.modules || {}).TTS || {};
  const speak = (text, bubble) => speakText(ttsMod, text, bubble);
  const add = (role, text) => {
    const b = el('div', 'bubble ' + role); b.textContent = text; log.appendChild(b); log.scrollTop = log.scrollHeight;
    if (role === 'assistant') { b.title = '点击用本智能体音色朗读'; b.style.cursor = 'pointer'; b.onclick = () => { if (b._t) speak(b._t, b); }; }
    return b;
  };
  const send = async () => {
    const m = input.value.trim(); if (!m) return; input.value = '';
    add('user', m);
    const pend = add('assistant', '思考中…');
    try {
      const j = await api('/api/agents/' + id + '/chat', { method: 'POST', body: JSON.stringify({ message: m, history: hist.slice() }) });
      if (j.ok) {
        pend.textContent = j.reply; pend._t = j.reply;
        hist.push({ role: 'user', content: m }, { role: 'assistant', content: j.reply });
        if ($('#chatSpeak').checked) speak(j.reply, pend);
      } else { pend.className = 'bubble err'; pend.textContent = '✗ ' + j.error; }
    } catch (e) { pend.className = 'bubble err'; pend.textContent = '✗ ' + e.message; }
  };
  $('#chatSend').onclick = send;
  $('#chatClear').onclick = () => { log.innerHTML = ''; hist.length = 0; };
  input.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
}

async function renderAgentTerminal(agentId) {
  const box = $('#agentTerminal'); if (!box) return;
  box.innerHTML = `<div class="panel"><div class="panel-b"><div class="hint">正在准备当前智能体的会话终端…</div></div></div>`;
  try {
    const t = await api('/api/agents/' + agentId + '/terminal');
    const d = t.device || {};
    box.innerHTML = `<div class="terminal-layout">
      <div class="panel terminal-side"><div class="panel-h"><span class="ttl">终端设备</span></div><div class="panel-b">
        <div class="kv"><span>Agent</span><code>${esc(t.agent_name || ('#' + agentId))}</code></div>
        <div class="kv"><span>Device MAC</span><code>${esc(d.mac || '')}</code></div>
        <div class="kv"><span>Client ID</span><code>${esc(d.client_id || '')}</code></div>
        <div class="kv"><span>OTA</span><code>${esc(t.ota_url || '')}</code></div>
        <div class="kv"><span>绑定状态</span><code>${d.agent_id ? '已绑定到当前智能体' : ('待绑定 ' + (d.bind_code || ''))}</code></div>
        <div class="hint">这里是真实 digital-human 引擎。启动后会通过 OTA 获取核心 WebSocket，再按当前智能体的 ASR、LLM、TTS、Intent、MCP 配置运行。</div>
      </div></div>
      <div class="panel terminal-frame"><iframe id="dhFrame" src="${esc(t.terminal_url)}" allow="microphone; camera; autoplay; clipboard-write"></iframe></div>
    </div>`;
    const frame = $('#dhFrame');
    frame.onload = () => {
      if (frame.dataset.seeded) return;
      try {
        const ls = frame.contentWindow.localStorage;
        ls.setItem('xz_tester_deviceMac', d.mac || '');
        ls.setItem('xz_tester_clientId', d.client_id || '');
        ls.setItem('xz_tester_deviceName', d.name || 'EV Terminal');
        ls.setItem('xz_tester_otaUrl', t.ota_url || '');
        ls.setItem('xz_tester_emojiEnabled', 'true');
        frame.dataset.seeded = '1';
        frame.contentWindow.location.replace(t.terminal_url);
      } catch (e) {
        toast('终端配置写入失败：' + e.message);
      }
    };
  } catch (e) {
    box.innerHTML = `<div class="errbox">终端初始化失败：${esc(e.message)}</div>`;
  }
}

// one module block
function providerStatus(mt, provider) {
  return (((BOOT.provider_status || {})[mt] || {})[provider]) || { configured: true, missing: [] };
}

function providerConfig(mt, provider) {
  return { ...((((BOOT.provider_configs || {})[mt] || {})[provider]) || {}) };
}

function providerOptionLabel(mt, provider) {
  return `${provider} · ${providerStatus(mt, provider).configured ? '已配置' : '需配置'}`;
}

function updateProviderStatus(wrap, provider, state) {
  const mt = wrap.dataset.type;
  BOOT.provider_status = BOOT.provider_status || {};
  BOOT.provider_status[mt] = BOOT.provider_status[mt] || {};
  BOOT.provider_status[mt][provider] = state;
  const select = wrap.querySelector('[data-prov]');
  const option = [...select.options].find(item => item.value === provider);
  if (option) {
    option.textContent = providerOptionLabel(mt, provider);
    option.dataset.configured = state.configured ? 'true' : 'false';
  }
  select._museRefresh?.();
}

function moduleBlock(mt, m) {
  const providers = Object.keys(BOOT.catalog[mt] || {});
  const selected = m.selected || providers[0] || '';
  const wrap = el('div', 'module' + (mt === 'TTS' || mt === 'LLM' || mt === 'Memory' ? '' : ' collapsed'));
  wrap.dataset.type = mt;
  wrap.innerHTML = `<div class="module-head">
      <span class="module-type">${mt}</span>
      <span class="module-title">${labelOf(mt)}</span>
      <span class="module-sel" data-sel></span>
      <span class="module-caret">▾</span>
    </div>
    <div class="module-body">
      <div class="row"><div class="field" style="flex:2"><label>Provider</label><select data-prov></select></div>
        <div class="field" style="flex:none;min-width:130px"><label>可用性</label><span class="led avail" data-avail>—</span></div></div>
      <div data-backups></div>
      <div data-voice></div>
      <div data-params></div>
      ${mt === 'Memory' ? `<div class="setup-memory" id="setupMemory">
        <div class="setup-memory-label">运行时档案</div>
        <p class="setup-memory-hint">可直接改 JSON 后点保存。对话也会自动更新；别写「技术咨询需求」这类分析腔，模型容易拿来炫耀。人设仍在「人设」页。</p>
        <textarea class="setup-dossier-view" id="setupDossierView" spellcheck="false" rows="14"></textarea>
        <div class="setup-memory-actions" style="margin-bottom:14px">
          <button type="button" class="btn sm" id="setupDossierSave">保存档案</button>
          <button type="button" class="btn ghost sm" id="setupDossierRefresh">刷新</button>
          <button type="button" class="btn ghost danger sm" id="setupDossierClear">清空档案</button>
          <span class="setup-memory-st" id="setupDossierStatus"></span>
        </div>
        <div class="setup-memory-label">事实条（兼容旧记忆）</div>
        <p class="setup-memory-hint">一条一条的补充事实。可手动增删改。</p>
        <div class="setup-memory-list" id="setupMemoryList"></div>
        <div class="setup-memory-add">
          <input type="text" id="setupMemoryInput" placeholder="例如：用户叫小明，喜欢喝美式" maxlength="200" autocomplete="off">
          <button type="button" class="btn sm" id="setupMemoryAdd">添加</button>
        </div>
        <div class="setup-memory-actions">
          <button type="button" class="btn ghost sm" id="setupMemoryRefresh">刷新</button>
          <button type="button" class="btn ghost danger sm" id="setupMemoryClear">全部清空</button>
          <span class="setup-memory-st" id="setupMemoryStatus"></span>
        </div>
      </div>` : ''}
    </div>`;
  const prov = wrap.querySelector('[data-prov]');
  providers.forEach(p => {
    const option = new Option(providerOptionLabel(mt, p), p);
    option.dataset.configured = providerStatus(mt, p).configured ? 'true' : 'false';
    prov.appendChild(option);
  });
  prov.value = selected;
  wrap._providerOverrides = {};
  providers.forEach(provider => { wrap._providerOverrides[provider] = providerConfig(mt, provider); });
  wrap._providerOverrides[selected] = { ...(m.overrides || wrap._providerOverrides[selected] || {}) };
  wrap._overrides = { ...(wrap._providerOverrides[selected] || {}) };
  wrap._backups = Array.isArray(m.backups) ? m.backups.filter(x => typeof x === 'string') : [];
  wrap.dataset.currentProvider = selected;
  wrap.querySelector('.module-head').onclick = e => { if (e.target.closest('[data-prov]')) return; wrap.classList.toggle('collapsed'); };
  prov.onchange = () => {
    const previous = wrap.dataset.currentProvider;
    if (previous) wrap._providerOverrides[previous] = moduleOverrides(wrap);
    const current = prov.value;
    wrap.dataset.currentProvider = current;
    wrap._overrides = { ...(wrap._providerOverrides[current] || providerConfig(mt, current)) };
    renderModuleBody(wrap);
    const state = providerStatus(mt, current);
    if (!state.configured) {
      const suffix = state.missing?.length ? `：${state.missing.join('、')}` : '';
      toast(`${current} 尚未配置${suffix}，补全后保存即可切换`);
      wrap.classList.remove('collapsed');
    }
    if (mt === 'Memory') syncMemoryPanelVisibility(wrap);
  };
  renderModuleBody(wrap);
  if (mt === 'Memory') syncMemoryPanelVisibility(wrap);
  return wrap;
}

function syncMemoryPanelVisibility(wrap) {
  const panel = wrap.querySelector('#setupMemory');
  if (!panel) return;
  const prov = wrap.querySelector('[data-prov]')?.value || '';
  const off = prov === 'nomem';
  panel.hidden = off;
  panel.classList.toggle('is-off', off);
}
function labelOf(mt) { return { VAD: '语音活动', ASR: '语音识别', LLM: '大脑', VLLM: '视觉', TTS: '音色', Memory: '记忆', Intent: '意图' }[mt] || mt; }

function renderModuleBackups(wrap) {
  const mt = wrap.dataset.type;
  const box = wrap.querySelector('[data-backups]');
  if (!box) return;
  if (mt !== 'LLM') { box.innerHTML = ''; return; }
  const cur = wrap.querySelector('[data-prov]').value;
  const providers = Object.keys(BOOT.catalog[mt] || {})
    .filter(p => p !== cur && providerStatus(mt, p).configured);
  const selected = wrap._backups || [];
  if (!providers.length) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="setup-field" style="margin-bottom:10px">
    <label>备用模型（降级/竞争）</label>
    <div class="chips setup-chips">${providers.map(p => {
      const on = selected.includes(p);
      return `<label class="chk" style="border-color:${on ? 'rgba(214,210,202,.28)' : 'var(--term-line)'};background:${on ? 'rgba(214,210,202,.08)' : 'rgba(255,255,255,.03)'};color:${on ? 'var(--term-ink)' : 'rgba(200,196,188,.75)'}"><input type="checkbox" data-backup="${esc(p)}" ${on ? 'checked' : ''}><span>${esc(providerOptionLabel(mt, p))}</span></label>`;
    }).join('')}</div>
    <span class="hint">语音回复与主模型并行竞争，谁先出首包用谁；主模型排队/失败时自动顶上。勾选需要多个模型都已配置密钥。</span>
  </div>`;
  box.querySelectorAll('[data-backup]').forEach(cb => {
    cb.addEventListener('change', () => {
      const set = new Set(wrap._backups || []);
      if (cb.checked) set.add(cb.value); else set.delete(cb.value);
      wrap._backups = [...set];
      cb.closest('.chip')?.classList.toggle('on', cb.checked);
    });
  });
}

function renderModuleBody(wrap) {
  const mt = wrap.dataset.type, prov = wrap.querySelector('[data-prov]').value;
  const def = BOOT.catalog[mt][prov] || {};
  const initialState = providerStatus(mt, prov);
  wrap.querySelector('[data-sel]').textContent = `${prov} · ${initialState.configured ? '已配置' : '需配置'}`;
  renderModuleBackups(wrap);
  const ph = wrap.querySelector('[data-params]'); ph.innerHTML = '';
  const grid = el('div', 'grid2');
  const VOICE_KEYS = ['voice', 'speaker', 'voice_id', 'private_voice', 'ref_audio_path', 'prompt_text'];
  Object.keys(def).forEach(k => {
    if (k === 'type' || k === 'output_dir') return;
    if (mt === 'TTS' && VOICE_KEYS.includes(k)) return;
    const val = wrap._overrides[k] != null ? wrap._overrides[k] : def[k];
    if (val && typeof val === 'object') return;
    const secret = /(^|_)(api_key|access_token|token|secret_key|secret_id|api_secret|access_key_id|access_key_secret|personal_access_token)$/i.test(k);
    const f = el('div', 'field'); f.innerHTML = `<label>${esc(k)}</label><input type="${secret ? 'password' : 'text'}" data-k="${esc(k)}" value="${esc(val)}" autocomplete="off">`;
    grid.appendChild(f);
  });
  if (grid.children.length) { const det = el('details'); det.innerHTML = `<summary style="margin-bottom:10px">参数 (${grid.children.length})</summary>`; det.appendChild(grid); if (mt === 'LLM' || mt === 'ASR') det.open = true; ph.appendChild(det); }
  const vh = wrap.querySelector('[data-voice]'); vh.innerHTML = '';
  if (mt === 'TTS') buildTTSVoice(wrap, vh, prov);
  wrap.querySelectorAll('[data-k], [data-voice-val], [data-ref], [data-prompt]').forEach(input => {
    input.addEventListener('input', () => {
      clearTimeout(wrap._checkTimer);
      wrap._checkTimer = setTimeout(() => checkModule(wrap), 250);
    });
  });
  checkModule(wrap);
  enhanceSelects(wrap);
}

function moduleOverrides(wrap) {
  const o = { ...wrap._overrides };
  wrap.querySelectorAll('[data-k]').forEach(i => o[i.dataset.k] = i.value);
  const vk = wrap._voiceKey; if (vk) { const vs = wrap.querySelector('[data-voice-val]'); if (vs) o[vk] = vs.value; }
  const rp = wrap.querySelector('[data-ref]'); if (rp) o['ref_audio_path'] = rp.value;
  const pt = wrap.querySelector('[data-prompt]'); if (pt) o['prompt_text'] = pt.value;
  return o;
}

async function checkModule(wrap) {
  const mt = wrap.dataset.type, av = wrap.querySelector('[data-avail]');
  const set = (cls, txt) => { av.className = 'led avail ' + cls; av.textContent = txt; };
  const provider = wrap.querySelector('[data-prov]').value;
  try {
    const state = await api('/api/providers/check', {
      method: 'POST',
      body: JSON.stringify({ module_type: mt, provider, overrides: moduleOverrides(wrap) }),
    });
    if (wrap.querySelector('[data-prov]').value !== provider) return;
    updateProviderStatus(wrap, provider, state);
    set(state.configured ? 'ok' : '', state.configured ? '已配置' : '需配置');
    av.title = state.detail || '';
    wrap.querySelector('[data-sel]').textContent = `${provider} · ${state.configured ? '已配置' : '需配置'}`;
  } catch (e) {
    set('err', '检测失败');
    av.title = e.message;
  }
}

async function buildTTSVoice(wrap, vh, prov) {
  let d; try { d = await api('/api/tts/voices?provider=' + encodeURIComponent(prov)); } catch (e) { return; }
  wrap._voiceKey = d.voiceKey || null;
  const box = el('div', 'field');
  if (d.mode === 'list') {
    box.innerHTML = `<label>音色</label><select data-voice-val></select>`;
    const sel = box.querySelector('select'); (d.voices || []).forEach(x => sel.appendChild(new Option(x.label, x.value)));
    const cur = wrap._overrides[d.voiceKey] || d.current; if (![...sel.options].some(o => o.value === cur)) sel.appendChild(new Option(cur + ' (自定义)', cur)); sel.value = cur;
    sel.onchange = () => checkModule(wrap);
  } else if (d.mode === 'refaudio') {
    box.innerHTML = `<label>参考音频路径</label><input type="text" data-ref value="${esc(wrap._overrides.ref_audio_path || d.ref_audio_path || '')}">
      <label style="margin-top:12px">参考台词</label><input type="text" data-prompt value="${esc(wrap._overrides.prompt_text || d.prompt_text || '')}">`;
  } else { box.innerHTML = `<label>音色 ${esc(d.voiceKey)}</label><input type="text" data-voice-val value="${esc(wrap._overrides[d.voiceKey] || d.current || '')}">`; }
  vh.appendChild(box);
  const bar = el('div', 'row'); bar.style.alignItems = 'flex-end';
  bar.innerHTML = `<div class="field" style="flex:2"><label>试听文本</label><input type="text" data-ptext value="你好，我是 EV，这是当前音色的效果。"></div>
    <button class="btn ghost sm" data-preview style="margin-bottom:15px">▶ 试听</button>`;
  vh.appendChild(bar);
  const audio = el('audio'); audio.controls = true; audio.style.display = 'none'; vh.appendChild(audio);
  const msg = el('div', 'msg'); vh.appendChild(msg);
  bar.querySelector('[data-preview]').onclick = async () => {
    msg.textContent = '合成中…'; msg.style.color = 'var(--muted)';
    try { const r = await fetch('/api/tts/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: prov, overrides: moduleOverrides(wrap), text: vh.querySelector('[data-ptext]').value }) });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || r.status); }
      const b = await r.blob(); audio.src = URL.createObjectURL(b); audio.style.display = 'block'; audio.play().catch(() => {}); msg.textContent = '✓ 已合成'; msg.style.color = 'var(--ok)';
    } catch (e) { msg.textContent = '✗ ' + e.message; msg.style.color = 'var(--err)'; }
  };
  if (d.clone) {
    const c = el('div'); c.style.marginTop = '14px'; c.style.borderTop = '1px dashed var(--line-2)'; c.style.paddingTop = '14px';
    c.innerHTML = `<div class="hint" style="margin-bottom:8px">🎤 克隆音色 — 10~300 秒清晰单人音频，首次合成扣约 ¥10</div>
      <div class="row"><div class="field" style="flex:2"><label>音频路径</label><input type="text" data-cpath placeholder="D:\\AI\\myvoice.wav"></div>
        <div class="field"><label>音色名 (≥8位字母数字)</label><input type="text" data-cname placeholder="muse2026"></div>
        <button class="btn ghost sm" data-clone style="margin-bottom:15px">🧬 克隆</button></div><div class="msg" data-cmsg></div>`;
    vh.appendChild(c);
    c.querySelector('[data-clone]').onclick = async () => {
      const path = c.querySelector('[data-cpath]').value.trim(), name = c.querySelector('[data-cname]').value.trim(), cm = c.querySelector('[data-cmsg]');
      if (!confirm('克隆将在首次合成时扣约 ¥10，确认？')) return;
      cm.textContent = '上传+复刻中…'; cm.style.color = 'var(--muted)';
      try { await api('/api/tts/minimax/clone', { method: 'POST', body: JSON.stringify({ audio_path: path, voice_id: name, overrides: moduleOverrides(wrap) }) });
        cm.textContent = '✓ 复刻成功，已选中'; cm.style.color = 'var(--ok)'; const sel = vh.querySelector('[data-voice-val]'); if (sel) { sel.appendChild(new Option(name + ' (克隆)', name)); sel.value = name; }
      } catch (e) { cm.textContent = '✗ ' + e.message; cm.style.color = 'var(--err)'; }
    };
  }
}

async function saveAgent(id) {
  const modules = {};
  $$('.module').forEach(w => {
    const node = { selected: w.querySelector('[data-prov]').value, overrides: moduleOverrides(w) };
    if (w.dataset.type === 'LLM' && Array.isArray(w._backups) && w._backups.length) node.backups = w._backups;
    modules[w.dataset.type] = node;
  });
  let hiddenPlugins = [];
  try { hiddenPlugins = JSON.parse($('#plugins')?.dataset?.hiddenPlugins || '[]'); } catch (_) {}
  const plugins = [...new Set([
    ...hiddenPlugins,
    ...$$('#plugins input:checked').map(c => c.value),
  ])];
  const data = { name: $('#f_name').value, prompt: $('#f_prompt').value, avatar: $('#f_avatar').value, mcp_endpoint: ($('#f_mcp')?.value || '').trim(), modules, plugins };
  const st = $('#saveStatus'); st.textContent = '保存中…';
  try {
    const result = await api('/api/agents/' + id, { method: 'PUT', body: JSON.stringify(data) });
    const fresh = result.agent || await api('/api/agents/' + id);
    const mismatched = Object.entries(modules)
      .filter(([type, node]) => ((fresh.modules || {})[type] || {}).selected !== node.selected)
      .map(([type]) => type);
    if (mismatched.length) throw new Error('保存后校验不一致：' + mismatched.join(' / '));
    BOOT = await api('/api/bootstrap');
    const stack = $('#setupStack');
    if (stack) stack.innerHTML = setupStackHtml(fresh.modules || {});
    st.textContent = '已同步';
    toast('已保存并校验 · 摄像头 2 秒内生效');
  }
  catch (e) { st.textContent = '保存失败：' + e.message; }
}

// ---------- device workshop · ESP-Claw ----------
async function renderWorkshop(v) {
  setCrumb(['设备工坊', 'ESP-Claw']);
  const SIM = 'https://skills-lab.esp-claw.com/';
  let info = { config: {}, snapshot: {} }, manifest = {};
  try { [info, manifest] = await Promise.all([api('/api/esp-claw/config'), api('/api/esp-claw/firmware')]); } catch (e) {}
  const boardCount = Object.values(manifest || {}).reduce((total, chips) => total + Object.values(chips || {}).reduce((chipTotal, brands) => chipTotal + Object.values(brands || {}).reduce((brandTotal, boards) => brandTotal + Object.keys(boards || {}).length, 0), 0), 0);
  const cfg = info.config || {}, snap = info.snapshot || {};
  v.innerHTML = `<div class="page-head"><div><div class="kicker">Device Workshop · ESP-Claw</div><h1 class="t">设备工坊</h1>
      <div class="d">EV 同时管理两种范式：现有对话设备仍由服务端智能体做大脑；ESP-Claw 设备自己运行 Agent Loop，EV 只负责刷写、配置衔接、预览与登记。</div></div></div>
    <div class="workshop-phases">
      <span class="active"><b>1</b> 浏览器刷写</span><span><b>2</b> 配网衔接</span><span><b>3</b> 本地模拟器</span><span><b>4</b> MCP 互通</span><span><b>5</b> 聊天编程</span>
    </div>
    <div class="workshop-grid">
      <section class="workshop-card primary">
        <div class="workshop-card-top"><span class="tag accent">Phase 1 · 已接入</span><span class="mono">${boardCount || '—'} 块板卡 · ${esc(snap.master_ref || 'snapshot')}</span></div>
        <h2>USB 刷写 ESP-Claw</h2>
        <p>官方 WebSerial 刷写器已打包到 EV；板卡清单和刷写代码本地加载，固件二进制按配置从 ESP-Claw 源下载。</p>
        <div class="sim-note mono">仅支持桌面 Chrome / Edge；必须从 <b>localhost 或 HTTPS</b> 打开。iOS / 手机浏览器不能刷写。</div>
        <div class="workshop-actions"><a class="btn" href="/flash">打开本地刷写器 →</a><a class="btn ghost" href="https://esp-claw.com/zh-cn/flash/" target="_blank" rel="noopener">官方页 ↗</a></div>
      </section>
      <section class="workshop-card">
        <div class="workshop-card-top"><span class="tag">Firmware source</span><span class="led ok">已配置</span></div>
        <h2>固件源 / 镜像</h2>
        <p>网络不稳时可换成自己的版本索引和固件镜像；保存后刷新刷写页生效。</p>
        <label class="field"><span>版本索引根地址</span><input id="clawVersionsUrl" value="${esc(cfg.versions_url || 'https://esp-claw.com/versions')}"></label>
        <label class="field"><span>master 固件站点</span><input id="clawFirmwareOrigin" value="${esc(cfg.firmware_origin || 'https://esp-claw.com')}"></label>
        <div class="workshop-actions"><button class="btn ghost" id="clawSaveSource">保存固件源</button><span class="msg" id="clawSourceMsg"></span></div>
      </section>
      <section class="workshop-card">
        <div class="workshop-card-top"><span class="tag">Edge registry</span><span class="mono">独立 Agent</span></div>
        <h2>手动登记边缘设备</h2>
        <p>刷写页也有登记入口；这里可补登已经刷好的设备。登记不会把它绑定到 EV 智能体。</p>
        <div class="grid2"><label class="field"><span>设备名</span><input id="clawDeviceName" placeholder="书桌 ESP-Claw"></label><label class="field"><span>设备 IP（可选）</span><input id="clawDeviceIp" placeholder="192.168.1.88"></label></div>
        <label class="field"><span>板卡 / 备注（可选）</span><input id="clawDeviceBoard" placeholder="ESP32-S3 DevKitC-1"></label>
        <div class="workshop-actions"><button class="btn ghost" id="clawRegister">登记边缘设备</button><span class="msg" id="clawRegisterMsg"></span></div>
      </section>
      <section class="workshop-card future">
        <div class="workshop-card-top"><span class="tag">Phase 3 · 在线预览</span><span class="mono">待本地 vendor</span></div>
        <h2>Lua / LVGL Skills Lab</h2>
        <p>当前先保留官方在线模拟器；后续把 <code>pages/simulator/</code> 打包进 EV 后才算离线集成。</p>
        <div class="workshop-actions"><button class="btn ghost" id="simToggle">在此加载</button><a class="btn ghost" href="${SIM}" target="_blank" rel="noopener">新窗口 ↗</a></div>
      </section>
    </div>
    <div class="sim-frame" id="simFrame" hidden>
      <div class="sim-load mono" id="simLoad">正在加载 ESP-Claw Skills Lab…</div>
      <iframe id="simIframe" title="ESP-Claw Skills Lab" referrerpolicy="no-referrer" allow="clipboard-write" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads"></iframe>
    </div>`;

  $('#clawSaveSource').onclick = async () => {
    const msg = $('#clawSourceMsg'); msg.textContent = '保存中…';
    try { await api('/api/esp-claw/config', { method: 'PUT', body: JSON.stringify({ versions_url: $('#clawVersionsUrl').value.trim(), firmware_origin: $('#clawFirmwareOrigin').value.trim() }) }); msg.textContent = '✓ 已保存'; }
    catch (e) { msg.textContent = '✗ ' + e.message; }
  };
  $('#clawRegister').onclick = async () => {
    const msg = $('#clawRegisterMsg'); msg.textContent = '登记中…';
    try { const j = await api('/api/devices/edge', { method: 'POST', body: JSON.stringify({ name: $('#clawDeviceName').value.trim(), ip_address: $('#clawDeviceIp').value.trim(), board: $('#clawDeviceBoard').value.trim() }) }); msg.innerHTML = `✓ 已登记 <code class="k">${esc(j.device.mac)}</code>`; }
    catch (e) { msg.textContent = '✗ ' + e.message; }
  };
  $('#simToggle').onclick = () => {
    const box = $('#simFrame'), fr = $('#simIframe'); box.hidden = false; fr.src = SIM; $('#simToggle').disabled = true; box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  const fr = $('#simIframe'), ld = $('#simLoad');
  if (fr) {
    fr.addEventListener('load', () => { if (ld) ld.style.display = 'none'; });
    setTimeout(() => { if (ld && ld.style.display !== 'none') ld.innerHTML = '若长时间空白：Skills Lab 可能因网络或跨域策略未加载，可点右上「新窗口打开 ↗」。'; }, 8000);
  }
}

// ---------- settings ----------
async function renderSettings(v) {
  setCrumb(['系统', '设置']);
  const cat = BOOT.catalog || {};
  const cnt = t => (cat[t] ? Object.keys(cat[t]).length : 0);
  let skill = {
    enabled: true, provider: 'agentsearch', max_results: 6, fetch_pages: 3,
    tavily_api_key_masked: '', metaso_api_key_masked: '',
    tavily_api_key_set: false, metaso_api_key_set: false, ready: [],
  };
  let ccSkill = {
    enabled: true, provider: 'auto', configured_provider: 'auto',
    available: false, providers: {}, timeout_s: 900,
  };
  try {
    skill = await api('/api/skills/web-search');
  } catch (e) {
    console.warn('[settings] web-search skill', e);
  }
  try {
    ccSkill = await api('/api/agent-runtime');
  } catch (e) {
    console.warn('[settings] agent runtime', e);
  }
  const readyTxt = (skill.ready && skill.ready.length)
    ? ('已就绪：' + skill.ready.join(' + '))
    : '未配置可用密钥';
  const ccReady = ccSkill.enabled && ccSkill.available
    ? ('已就绪 · ' + (ccSkill.provider || '自动'))
    : (ccSkill.enabled ? '当前执行器不可用' : '已关闭');
  v.innerHTML = `<div class="page-head"><div><div class="kicker">System</div><h1 class="t">设置</h1></div></div>
    <div class="panel"><div class="panel-h"><span class="ttl">工作 Agent</span>
      <span class="chip ${ccSkill.enabled && ccSkill.available ? 'ok' : ''}">${esc(ccReady)}</span></div>
    <div class="panel-b">
      <p class="hint" style="margin-top:0">EV 通过统一运行时把确认后的工程任务交给执行器。代码、PCB、CAD 后续都复用同一对象协议，不会增加对话工具数量。</p>
      <div class="row">
        <div class="field" style="min-width:160px">
          <label><input type="checkbox" id="skCcEnabled" ${ccSkill.enabled ? 'checked' : ''}> 启用工作 Agent</label>
        </div>
        <div class="field" style="min-width:160px">
          <label>执行器</label>
          <select id="skCcProvider">
            <option value="auto" ${ccSkill.configured_provider === 'auto' ? 'selected' : ''}>自动选择</option>
            <option value="codex" ${ccSkill.configured_provider === 'codex' ? 'selected' : ''}>Codex App Server</option>
            <option value="claude" ${ccSkill.configured_provider === 'claude' ? 'selected' : ''}>Claude 兼容层</option>
          </select>
        </div>
        <div class="field" style="min-width:100px">
          <label>超时秒</label>
          <input type="number" id="skCcTimeout" min="30" max="7200" value="${esc(String(ccSkill.timeout_s || 900))}">
        </div>
      </div>
      <div class="row" style="align-items:flex-end;gap:10px;margin-top:8px">
        <button type="button" class="btn" id="skCcSave">保存</button>
        <div class="field" style="flex:1;min-width:200px;margin:0">
          <label>测试任务</label>
          <input type="text" id="skCcTask" placeholder="例如：用一句话总结当前目录有哪些文件">
        </div>
        <button type="button" class="btn primary" id="skCcRun">启动测试</button>
      </div>
      <div id="skCcResult" class="hint" style="margin-top:12px;white-space:pre-wrap"></div>
    </div></div>
    <div class="panel"><div class="panel-h"><span class="ttl">技能 · 网页搜索</span>
      <span class="chip ${skill.enabled && skill.ready?.length ? 'ok' : ''}">${esc(readyTxt)}</span></div>
    <div class="panel-b">
      <p class="hint" style="margin-top:0">AgentSearch（默认）：本机自托管、开源、免 API key —— SearXNG 多引擎聚合 + 去重打分 + 正文多级抽取（含浏览器渲染兜底），配图取自 SearXNG 图片源。Tavily/秘塔为可选外部源。</p>
      <div class="row">
        <div class="field" style="min-width:140px">
          <label><input type="checkbox" id="skWsEnabled" ${skill.enabled ? 'checked' : ''}> 启用网页搜索</label>
        </div>
        <div class="field" style="min-width:140px">
          <label><input type="checkbox" id="skWsExtract" ${skill.use_extract !== false ? 'checked' : ''}> Tavily 读原文</label>
        </div>
        <div class="field" style="min-width:120px">
          <label><input type="checkbox" id="skWsImages" ${skill.include_images !== false ? 'checked' : ''}> 抽取配图</label>
        </div>
        <div class="field" style="min-width:160px">
          <label>搜索源</label>
          <select id="skWsProvider">
            <option value="agentsearch" ${skill.provider === 'agentsearch' ? 'selected' : ''}>AgentSearch（本地自托管·开源）</option>
            <option value="tavily" ${skill.provider === 'tavily' ? 'selected' : ''}>Tavily</option>
            <option value="metaso" ${skill.provider === 'metaso' ? 'selected' : ''}>秘塔 Metaso</option>
            <option value="both" ${skill.provider === 'both' ? 'selected' : ''}>双源合并</option>
          </select>
        </div>
        <div class="field" style="min-width:100px">
          <label>条数</label>
          <input type="number" id="skWsMax" min="1" max="12" value="${esc(String(skill.max_results || 6))}">
        </div>
        <div class="field" style="min-width:100px">
          <label>读原文</label>
          <input type="number" id="skWsFetch" min="0" max="8" value="${esc(String(skill.fetch_pages || 3))}">
        </div>
      </div>
      <div class="row">
        <div class="field" style="flex:1;min-width:220px">
          <label>Tavily API Key ${skill.tavily_api_key_set ? '· 已保存 ' + esc(skill.tavily_api_key_masked || '') : ''}</label>
          <input type="password" id="skWsTavily" placeholder="${skill.tavily_api_key_set ? '留空则保持不变' : 'tvly-…'}" autocomplete="off">
        </div>
        <div class="field" style="flex:1;min-width:220px">
          <label>Metaso API Key ${skill.metaso_api_key_set ? '· 已保存 ' + esc(skill.metaso_api_key_masked || '') : ''}</label>
          <input type="password" id="skWsMetaso" placeholder="${skill.metaso_api_key_set ? '留空则保持不变' : 'mk-…'}" autocomplete="off">
        </div>
      </div>
      <div class="row" style="align-items:flex-end;gap:10px;margin-top:8px">
        <button type="button" class="btn" id="skWsSave">保存</button>
        <div class="field" style="flex:1;min-width:200px;margin:0">
          <label>测试查询</label>
          <input type="text" id="skWsQuery" placeholder="例如：今天 A 股有什么重要新闻">
        </div>
        <button type="button" class="btn primary" id="skWsRun">测试搜索</button>
      </div>
      <div id="skWsResult" class="hint" style="margin-top:12px;white-space:pre-wrap"></div>
    </div></div>
    <div class="panel"><div class="panel-h"><span class="ttl">核心接入</span></div><div class="panel-b">
      <div class="field"><label>Server Secret</label><input type="text" value="${esc(BOOT.secret)}" readonly></div>
      <div class="hint">核心 server 的 <code class="k">data/.config.yaml</code> 里
        <code class="k">manager-api.url = http://127.0.0.1:8002/xiaozhi</code>、
        <code class="k">secret</code> 填上面这串，即从 EV 拉取每台设备所属智能体的配置。</div></div></div>
    <div class="panel"><div class="panel-h"><span class="ttl">模型库</span></div><div class="panel-b">
      <div class="row">
        ${['LLM', 'ASR', 'TTS', 'VLLM', 'VAD', 'Intent', 'Memory'].map(t => `<div class="field" style="min-width:120px"><label>${t}</label><div class="mono" style="font-size:16px;color:var(--ink)">${cnt(t)}</div></div>`).join('')}
      </div><div class="hint">provider 目录读自核心的 <code class="k">config.yaml</code>，随核心版本自动更新。</div></div></div>`;

  const resultBox = $('#skWsResult');
  const ccResult = $('#skCcResult');
  const ccSave = $('#skCcSave');
  if (ccSave) {
    ccSave.onclick = async () => {
      const body = {
        enabled: $('#skCcEnabled')?.checked !== false,
        provider: $('#skCcProvider')?.value || 'auto',
        timeout_s: +($('#skCcTimeout')?.value || 900),
      };
      try {
        await api('/api/agent-runtime', { method: 'PUT', body: JSON.stringify(body) });
        toast('工作 Agent 设置已保存');
        await renderSettings(v);
      } catch (e) {
        toast('保存失败：' + (e.message || e));
      }
    };
    $('#skCcRun') && ($('#skCcRun').onclick = async () => {
      const task = ($('#skCcTask')?.value || '').trim();
      if (!task) { toast('先写测试任务'); return; }
      ccResult.textContent = '运行中…';
      try {
        const j = await api('/api/agent-runtime/run', {
          method: 'POST',
          body: JSON.stringify({ task, mode: 'external' }),
        });
        const lines = [(j.ok ? '✓ 已启动' : '✗ 未启动'), 'run=' + (j.run_id || '—') + ' · provider=' + (j.provider || '—')];
        if (j.error) lines.push('错误：' + j.error);
        if (j.ok) lines.push('实时过程会显示在状态窗旁边的工作窗口。');
        ccResult.textContent = lines.join('\n');
      } catch (e) {
        ccResult.textContent = '测试失败：' + (e.message || e);
      }
    });
  }
  $('#skWsSave').onclick = async () => {
    const body = {
      enabled: $('#skWsEnabled').checked,
      use_extract: $('#skWsExtract')?.checked !== false,
      include_images: $('#skWsImages')?.checked !== false,
      provider: $('#skWsProvider').value,
      max_results: +$('#skWsMax').value || 6,
      fetch_pages: +$('#skWsFetch').value || 0,
    };
    const tavily = ($('#skWsTavily').value || '').trim();
    const metaso = ($('#skWsMetaso').value || '').trim();
    if (tavily) body.tavily_api_key = tavily;
    if (metaso) body.metaso_api_key = metaso;
    try {
      const j = await api('/api/skills/web-search', { method: 'PUT', body: JSON.stringify(body) });
      toast(j.ready?.length ? '已保存 · ' + j.ready.join('+') : '已保存（尚未配置可用密钥）');
      await renderSettings(v);
    } catch (e) {
      toast('保存失败：' + (e.message || e));
    }
  };
  $('#skWsRun').onclick = async () => {
    const query = ($('#skWsQuery').value || '').trim();
    if (!query) { toast('先写一句测试查询'); return; }
    resultBox.textContent = '搜索中…';
    try {
      const j = await api('/api/skills/web-search/run', {
        method: 'POST',
        body: JSON.stringify({ query }),
      });
      const lines = [];
      lines.push((j.ok ? '✓' : '✗') + ' ' + (j.sources || []).join('+') + ' · ' + (j.elapsed_ms || '?') + 'ms');
      if (j.error) lines.push('错误：' + j.error);
      if (j.summary) { lines.push(''); lines.push('摘要：' + String(j.summary).slice(0, 280)); }
      (j.items || []).slice(0, 5).forEach((it, i) => {
        lines.push((i + 1) + '. ' + (it.title || '无标题'));
        if (it.url) lines.push('   ' + it.url);
        if (it.snippet) lines.push('   ' + String(it.snippet).slice(0, 120));
      });
      const pages = (j.pages || []).filter(p => p.ok);
      const imgN = (j.images || []).length + pages.reduce((n, p) => n + (p.images?.length || 0), 0);
      if (pages.length || imgN) {
        lines.push('');
        lines.push('原文 ' + pages.length + ' 页 · 配图 ' + imgN);
        pages.slice(0, 2).forEach(p => {
          lines.push('· ' + (p.title || p.url) + (p.extractor ? ' [' + p.extractor + ']' : ''));
          if (p.summary) lines.push('  ' + String(p.summary).slice(0, 160));
        });
      }
      resultBox.textContent = lines.join('\n');
    } catch (e) {
      resultBox.textContent = '测试失败：' + (e.message || e);
    }
  };
}

// ---------- 虚拟形象预览（Live2D / 声波可视化） ----------
let _l2dLoaded = null, _pixiApp = null, _l2dRO = null;
let _setupVizRaf = null, _setupPixiApp = null, _setupL2dRO = null;

function loadScript(src) { return new Promise((res, rej) => { const s = document.createElement('script'); s.src = src; s.onload = res; s.onerror = () => rej(new Error('脚本加载失败 ' + src)); document.head.appendChild(s); }); }
async function ensureLive2D() {
  if (_l2dLoaded) return _l2dLoaded;
  _l2dLoaded = (async () => { await loadScript('/avatar-lib/pixi.js'); await loadScript('/avatar-lib/live2dcubismcore.min.js'); await loadScript('/avatar-lib/cubism4.min.js'); })();
  return _l2dLoaded;
}
function disposePixi() { if (_l2dRO) { try { _l2dRO.disconnect(); } catch (e) {} _l2dRO = null; } if (_pixiApp) { try { _pixiApp.destroy(true, { children: true }); } catch (e) {} _pixiApp = null; } }
function disposeSetupPixi() {
  if (_setupL2dRO) { try { _setupL2dRO.disconnect(); } catch (e) {} _setupL2dRO = null; }
  if (_setupPixiApp) { try { _setupPixiApp.destroy(true, { children: true }); } catch (e) {} _setupPixiApp = null; }
}
function stopSetupVisualizerLoop() {
  if (_setupVizRaf) { cancelAnimationFrame(_setupVizRaf); _setupVizRaf = null; }
}
function stopSetupAvatarPreview() {
  stopSetupVisualizerLoop();
  disposeSetupPixi();
}
function drawSetupRoundBar(ctx, x, y, w, h, r) {
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
  ctx.fill();
}
function startSetupVisualizer(canvas) {
  stopSetupVisualizerLoop();
  const ctx = canvas.getContext('2d');
  const barCount = 11;
  const heights = new Array(barCount).fill(0);
  let phase = 0;
  const resize = () => {
    const wrap = canvas.parentElement;
    const w = wrap.clientWidth, h = wrap.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    canvas._vw = w;
    canvas._vh = h;
  };
  resize();
  const draw = () => {
    _setupVizRaf = requestAnimationFrame(draw);
    phase += 0.016;
    const w = canvas._vw, h = canvas._vh;
    if (!w || !h) return;
    ctx.clearRect(0, 0, w, h);
    const cx = w * 0.5, cy = h * 0.5;
    const barW = 4, gap = 10, minH = 22, maxH = Math.min(w, h) * 0.32;
    const startX = cx - (barCount * barW + (barCount - 1) * gap) / 2;
    for (let i = 0; i < barCount; i++) {
      const wave = Math.sin(phase * 1.1 + i * 0.55) * 0.5 + 0.5;
      const target = minH + wave * (maxH - minH) * 0.35;
      heights[i] += (target - heights[i]) * 0.08;
      const barH = heights[i];
      const x = startX + i * (barW + gap);
      const y = cy - barH * 0.5;
      const dist = Math.abs(i - (barCount - 1) / 2) / ((barCount - 1) / 2);
      const alpha = 0.16 + (1 - dist * 0.35) * 0.14;
      ctx.fillStyle = `rgba(214, 210, 202, ${alpha})`;
      drawSetupRoundBar(ctx, x, y, barW, barH, barW);
    }
  };
  draw();
}
async function mountSetupLive2D(modelUrl) {
  if (!modelUrl) return;
  const canvas = $('#setupLive2d');
  const wrap = $('#setupAvatarStage');
  if (!canvas || !wrap) return;
  disposeSetupPixi();
  await ensureLive2D();
  _setupPixiApp = new PIXI.Application({ view: canvas, resizeTo: wrap, backgroundAlpha: 0, antialias: true, autoDensity: true, resolution: window.devicePixelRatio || 1 });
  const model = await PIXI.live2d.Live2DModel.from(modelUrl);
  _setupPixiApp.stage.addChild(model);
  model.interactive = true;
  model.on('pointertap', () => { try { model.motion('Tap'); } catch (e) {} });
  const refit = () => fitModel(model, _setupPixiApp.screen.width, _setupPixiApp.screen.height);
  refit();
  _setupL2dRO = new ResizeObserver(refit);
  _setupL2dRO.observe(wrap);
}
async function wireSetupAvatarPreview(avatarName) {
  const stage = $('#setupAvatarStage');
  if (!stage) return;
  const viz = $('#setupVizCanvas');
  const l2d = $('#setupLive2d');
  const cap = $('#setupAvatarCap');
  const list = BOOT.avatars || [];
  const av = list.find(x => x.name === avatarName) || { name: avatarName, type: 'visualizer' };
  if (cap) cap.textContent = av.label || av.name || avatarName;
  if (avatarName === 'visualizer' || av.type === 'visualizer') {
    disposeSetupPixi();
    if (l2d) l2d.classList.add('hidden');
    if (viz) { viz.classList.remove('hidden'); startSetupVisualizer(viz); }
    return;
  }
  stopSetupVisualizerLoop();
  if (viz) viz.classList.add('hidden');
  if (l2d) {
    l2d.classList.remove('hidden');
    try { await mountSetupLive2D(av.model); } catch (e) { if (cap) cap.textContent = '形象加载失败'; }
  }
}
function fitModel(model, W, H) { if (!W || !H) return; const s = Math.min((H * 0.94) / (model.internalModel.height || model.height), (W * 0.92) / (model.internalModel.width || model.width)); model.scale.set(s); model.x = (W - model.width) / 2; model.y = H - model.height + H * 0.03; }
async function mountLive2D(modelUrl) {
  const canvas = $('#live2d-stage'); if (!canvas || !modelUrl) return;
  disposePixi(); const wrap = canvas.parentElement;
  _pixiApp = new PIXI.Application({ view: canvas, resizeTo: wrap, backgroundAlpha: 0, antialias: true, autoDensity: true, resolution: window.devicePixelRatio || 1 });
  const model = await PIXI.live2d.Live2DModel.from(modelUrl);
  _pixiApp.stage.addChild(model); model.interactive = true;
  model.on('pointertap', () => { try { model.motion('Tap'); } catch (e) {} });
  const refit = () => fitModel(model, _pixiApp.screen.width, _pixiApp.screen.height); refit();
  _l2dRO = new ResizeObserver(refit); _l2dRO.observe(wrap);
}

// ---------- boot ----------
(async function () {
  document.documentElement.setAttribute('data-theme', localStorage.getItem('muse-theme') || 'dark');
  syncRouteClasses();
  mountShell();
  try {
    BOOT = await api('/api/bootstrap');
    window.addEventListener('hashchange', route);
    await route();
  } finally {
    document.documentElement.classList.remove('muse-booting', 'pre-terminal');
    document.documentElement.classList.add('muse-ready');
  }
})();
