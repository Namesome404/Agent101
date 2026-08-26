const tauri = window.__TAURI__
const invoke = tauri.core.invoke
const listen = tauri.event.listen

// 手动窗口拖拽：macOS overlay 标题栏下 data-tauri-drag-region 属性不可靠
// （tauri issue #7304），统一在标题栏 mousedown 时调 startDragging。
// 关闭按钮单独响应 click，不进入拖拽。
function bindWindowDrag() {
  if (ownedSurfaceId === 'work-hud') return
  const bar = document.getElementById('surface-bar')
  if (!bar) return
  bar.addEventListener('mousedown', (event) => {
    if (event.button !== 0) return
    if (event.target.closest('button')) return
    event.preventDefault()
    window.getSelection()?.removeAllRanges()
    document.body.classList.add('is-dragging')
    tauri.window.getCurrentWindow().startDragging().catch(() => {})
  })
  window.addEventListener('mouseup', () => document.body.classList.remove('is-dragging'))
  window.addEventListener('blur', () => document.body.classList.remove('is-dragging'))
  bar.addEventListener('selectstart', (event) => event.preventDefault())
}

function decodeSurfaceLabel(label) {
  const prefix = 'surface-'
  if (!label.startsWith(prefix)) return ''
  const hex = label.slice(prefix.length)
  if (!hex || hex.length % 2 !== 0) return ''
  const bytes = new Uint8Array(hex.length / 2)
  for (let index = 0; index < hex.length; index += 2) {
    const value = Number.parseInt(hex.slice(index, index + 2), 16)
    if (!Number.isFinite(value)) return ''
    bytes[index / 2] = value
  }
  return new TextDecoder().decode(bytes)
}

const ownedSurfaceLabel = new URLSearchParams(window.location.search).get('surface') || ''
const ownedSurfaceId = decodeSurfaceLabel(ownedSurfaceLabel)
const ownedEventTarget = {target: {kind: 'Webview', label: ownedSurfaceLabel}}
document.body.dataset.windowKind = ownedSurfaceId === 'status-timeline'
  ? 'overlay'
  : (ownedSurfaceId === 'work-hud' ? 'hud' : 'native')

const contentNode = document.getElementById('surface-content')
const titleNode = document.getElementById('surface-title')
const statusNode = document.getElementById('surface-status')
const closeNode = document.getElementById('surface-close')

let currentSurface = null
let lastContentFingerprint = ''
let timerHandle = null
let hideHandle = null

function safeText(value) {
  return String(value ?? '')
}

function clearTimer() {
  if (timerHandle !== null) window.clearInterval(timerHandle)
  timerHandle = null
}

function emitSurface(name, data = {}) {
  return invoke('surface_event', {surfaceId: ownedSurfaceId, name, data}).catch(() => {})
}

function animateIn() {
  window.clearTimeout(hideHandle)
  document.body.classList.remove('is-closing')
  document.body.classList.add('is-entering')
  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.body.classList.remove('is-entering')
  }))
}

function animateOut(reason = 'scene') {
  window.clearTimeout(hideHandle)
  document.body.classList.remove('is-entering')
  document.body.classList.add('is-closing')
  hideHandle = window.setTimeout(() => {
    const command = reason === 'user' ? 'surface_close' : 'surface_hidden'
    invoke(command, {surfaceId: ownedSurfaceId}).catch(() => {})
  }, 220)
}

function httpUrl(value) {
  try {
    const url = new URL(safeText(value))
    return ['http:', 'https:'].includes(url.protocol) ? url : null
  } catch {
    return null
  }
}

function renderTimer(content) {
  const state = content.state && typeof content.state === 'object' ? content.state : content
  const durationSeconds = Math.max(1, Number(state.duration_seconds || 600))
  const explicitRemaining = Math.max(0, Number(state.remaining_seconds ?? durationSeconds))
  const endsAtRaw = Number(state.ends_at || state.deadline || 0)
  const endsAtMs = endsAtRaw > 0 ? (endsAtRaw < 10_000_000_000 ? endsAtRaw * 1000 : endsAtRaw) : 0
  const status = safeText(state.status || 'running')

  const root = document.createElement('div')
  root.className = 'surface-app timer-app'
  const center = document.createElement('div')
  const value = document.createElement('div')
  value.className = 'timer-value'
  const detail = document.createElement('div')
  detail.className = 'timer-detail'
  const progress = document.createElement('div')
  progress.className = 'timer-progress'
  const progressValue = document.createElement('span')
  progress.appendChild(progressValue)
  center.append(value, detail, progress)
  const actions = document.createElement('div')
  actions.className = 'app-actions'
  const pause = document.createElement('button')
  pause.type = 'button'
  pause.textContent = status === 'paused' ? '继续' : '暂停'
  pause.addEventListener('click', () => emitSurface('app.command', {
    app_id: 'timer', command: status === 'paused' ? 'resume' : 'pause',
  }))
  const add = document.createElement('button')
  add.type = 'button'
  add.textContent = '＋5 分钟'
  add.addEventListener('click', () => emitSurface('app.command', {
    app_id: 'timer', command: 'add', seconds: 300,
  }))
  actions.append(pause, add)
  root.append(center, actions)
  contentNode.appendChild(root)

  const update = () => {
    const remaining = status === 'running' && endsAtMs
      ? Math.max(0, Math.ceil((endsAtMs - Date.now()) / 1000))
      : explicitRemaining
    const minutes = Math.floor(remaining / 60)
    const seconds = remaining % 60
    value.textContent = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    const ratio = Math.min(1, Math.max(0, 1 - remaining / durationSeconds))
    progressValue.style.width = `${ratio * 100}%`
    detail.textContent = status === 'paused'
      ? '已暂停'
      : (remaining === 0 ? '计时结束' : `${new Date(Date.now() + remaining * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'})} 结束`)
    statusNode.style.opacity = status === 'paused' ? '.35' : '1'
    if (remaining === 0) clearTimer()
  }
  update()
  if (status === 'running') timerHandle = window.setInterval(update, 250)
}

function renderNotes(content) {
  const state = content.state && typeof content.state === 'object' ? content.state : content
  const items = Array.isArray(state.items) ? state.items.map(safeText) : []
  const text = safeText(state.text || state.draft || items.join('\n'))
  const root = document.createElement('div')
  root.className = 'surface-app note-app'
  const center = document.createElement('div')
  const copy = document.createElement('div')
  copy.className = 'note-copy'
  copy.textContent = text || '开始说话，我会把内容记在这里。'
  if (state.preview === true) {
    const cursor = document.createElement('span')
    cursor.className = 'note-cursor'
    copy.appendChild(cursor)
  }
  const detail = document.createElement('div')
  detail.className = 'note-detail'
  detail.textContent = state.preview === true ? '正在识别' : '已保存到本机'
  center.append(copy, detail)
  root.appendChild(center)
  contentNode.appendChild(root)
}

function renderWorkAgent(app) {
  const state = app?.state && typeof app.state === 'object' ? app.state : app
  const events = Array.isArray(state.events) ? state.events : []
  const latest = events.at(-1) || {}
  const phase = safeText(state.phase || latest.phase || 'working')
  const done = state.done === true
  const failed = done && state.ok === false

  const root = document.createElement('div')
  root.className = 'work-agent'
  root.dataset.phase = failed ? 'failed' : (done ? 'completed' : phase)

  const visual = document.createElement('div')
  visual.className = 'work-visual'
  visual.setAttribute('aria-hidden', 'true')
  const core = document.createElement('span')
  core.className = 'work-core'
  for (let index = 0; index < 3; index += 1) {
    const orbit = document.createElement('i')
    orbit.className = `work-orbit orbit-${index + 1}`
    visual.appendChild(orbit)
  }
  visual.appendChild(core)

  const phaseNode = document.createElement('div')
  phaseNode.className = 'work-phase'
  phaseNode.textContent = failed ? '未完成' : (done ? '已完成' : safeText(latest.label || state.status || '处理中'))

  const detail = document.createElement('div')
  detail.className = 'work-detail'
  detail.textContent = safeText(latest.detail || state.detail || '准备工作区')

  const meta = document.createElement('div')
  meta.className = 'work-meta'
  const fileCount = Array.isArray(state.files) ? state.files.length : 0
  const checkCount = Math.max(0, Number(state.checks || 0))
  const facts = []
  if (fileCount) facts.push(`${fileCount} 文件`)
  if (checkCount) facts.push(`${checkCount} 检查`)
  meta.textContent = facts.join(' · ') || (done ? '结果已交给 EV' : '实时同步')

  root.append(visual, phaseNode, detail, meta)
  contentNode.appendChild(root)
}

function renderBlocks(data) {
  const root = document.createElement('div')
  root.className = 'surface-blocks'
  if (data.title) {
    const heading = document.createElement('h1')
    heading.textContent = safeText(data.title)
    root.appendChild(heading)
  }
  for (const block of Array.isArray(data.blocks) ? data.blocks : []) {
    const type = safeText(block?.type || 'text')
    if (type === 'heading') {
      const heading = document.createElement('h1')
      heading.textContent = safeText(block.text)
      root.appendChild(heading)
    } else if (type === 'list') {
      const list = document.createElement('ul')
      for (const item of Array.isArray(block.items) ? block.items : []) {
        const row = document.createElement('li')
        row.textContent = safeText(item)
        list.appendChild(row)
      }
      root.appendChild(list)
    } else {
      const paragraph = document.createElement('p')
      paragraph.textContent = safeText(block.text)
      root.appendChild(paragraph)
    }
  }
  contentNode.appendChild(root)
}

function renderFrame(content, isUrl = false) {
  const frame = document.createElement('iframe')
  frame.className = 'surface-frame'
  frame.referrerPolicy = 'no-referrer'
  if (isUrl) {
    frame.src = safeText(content.url)
  } else {
    frame.setAttribute('sandbox', 'allow-scripts allow-forms allow-modals allow-popups')
    const css = safeText(content.css)
    const html = safeText(content.html)
    const js = safeText(content.js)
      .replaceAll('</script', '<\\/script')
      .replaceAll('</SCRIPT', '<\\/SCRIPT')
    frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><style>html,body{width:100%;min-height:100%;margin:0;box-sizing:border-box;background:transparent}${css}</style></head><body>${html}${js ? `<script>${js}<\/script>` : ''}</body></html>`
  }
  emitSurface('content.loading', {url: safeText(content.url)})
  frame.addEventListener('load', () => emitSurface('content.ready', {url: safeText(content.url)}), {once: true})
  contentNode.appendChild(frame)
}

function renderNativePage(content) {
  const url = httpUrl(content.url)
  const root = document.createElement('div')
  root.className = 'native-page-placeholder'
  const host = document.createElement('div')
  host.className = 'native-page-host'
  host.textContent = url?.hostname || '正在打开网页'
  const detail = document.createElement('div')
  detail.className = 'native-page-detail'
  detail.textContent = '载入中…'
  root.append(host, detail)
  contentNode.appendChild(root)
}

function contentFingerprint(data) {
  return JSON.stringify({
    app: data.app || null,
    content: data.content || null,
    document: data.document || null,
    blocks: data.blocks || null,
  })
}

function renderSurface(surface) {
  currentSurface = surface
  const data = surface?.data || {}
  const content = data.content && typeof data.content === 'object' ? data.content : {}
  const app = data.app && typeof data.app === 'object' ? data.app : null
  const surfaceId = safeText(surface?.id)
  document.body.dataset.surfaceId = surfaceId
  titleNode.textContent = safeText(data.title || app?.title || surfaceId)
  const fingerprint = contentFingerprint(data)
  if (fingerprint === lastContentFingerprint) return
  lastContentFingerprint = fingerprint
  clearTimer()
  contentNode.replaceChildren()
  document.body.classList.remove('has-native-page')

  const appId = safeText(app?.id || content.app_id)
  if (appId === 'timer') {
    renderTimer(app || content)
    emitSurface('content.ready', {kind: 'app', app_id: appId})
  } else if (appId === 'notes') {
    renderNotes(app || content)
    emitSurface('content.ready', {kind: 'app', app_id: appId})
  } else if (appId === 'agent-work') {
    renderWorkAgent(app || content)
    emitSurface('content.ready', {kind: 'app', app_id: appId})
  } else if (content.type === 'url' && httpUrl(content.url)) {
    if (surfaceId === 'status-timeline') {
      renderFrame(content, true)
    } else {
      document.body.classList.add('has-native-page')
      renderNativePage(content)
    }
  } else if (['html', 'app', 'chart'].includes(safeText(content.type)) && safeText(content.html || content.js)) {
    renderFrame(content)
  } else if (Array.isArray(data.blocks) && data.blocks.length) {
    renderBlocks(data)
    emitSurface('content.ready', {kind: 'blocks'})
  } else if (content.text || (Array.isArray(content.items) && content.items.length)) {
    const text = document.createElement('pre')
    text.className = 'surface-text'
    text.textContent = [safeText(content.text), ...(content.items || []).map(safeText)].filter(Boolean).join('\n')
    contentNode.appendChild(text)
    emitSurface('content.ready', {kind: 'text'})
  } else {
    const empty = document.createElement('div')
    empty.className = 'surface-empty'
    empty.textContent = 'EV'
    contentNode.appendChild(empty)
    emitSurface('content.ready', {kind: 'empty'})
  }
}

closeNode.addEventListener('click', () => animateOut('user'))
bindWindowDrag()

await listen('surface:update', event => {
  if (safeText(event.payload?.id) === ownedSurfaceId) renderSurface(event.payload)
}, ownedEventTarget)
await listen('surface:show', () => animateIn(), ownedEventTarget)
await listen('surface:hide', event => {
  animateOut(event.payload?.reason === 'user' ? 'user' : 'scene')
}, ownedEventTarget)

const initial = await invoke('surface_get', {surfaceId: ownedSurfaceId})
if (initial) renderSurface(initial)
if (initial?.visible === true) {
  await invoke('surface_present', {surfaceId: ownedSurfaceId})
  animateIn()
}

const sizeObserver = new ResizeObserver(entries => {
  const entry = entries[0]
  if (!entry || !currentSurface) return
  emitSurface('content.size', {
    width: Math.ceil(entry.contentRect.width),
    height: Math.ceil(entry.contentRect.height),
  })
})
sizeObserver.observe(contentNode)
