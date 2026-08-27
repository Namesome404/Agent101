# -*- coding: utf-8 -*-
"""Compact voice-facing desktop surface capability.

The low-level Scene protocol remains available internally.  Voice turns use
this high-level tool so ordinary pages require a small typed payload instead
of a large HTML/CSS function call.  Custom code is still supported when the
request genuinely needs an interactive page.
"""
from __future__ import annotations

import html
import json
import re
import uuid
from urllib.parse import urlparse

from devices.coding import surface_tools
from devices.coding.scene_store import scene_store
from tools import surface_apps


DEFAULT_SURFACE_ID = "conversation-canvas"


def tool_definition(*, slim=False):
    definition = {
        "type": "function",
        "function": {
            "name": "surface_control",
            "description": (
                "页面中控。app=内置计时器/记事本；create 新建独立页；update 修改现有页；"
                "close 隐藏；delete/status/append/record_*；目标不清 clarify。"
                "页面用 sections 或 html/css/js；相对几何先 status；状态栏 status-timeline。"
                "网站给 url（同站复用窗口）；close 也带 url 定位，别猜 id。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "app", "create", "update", "append", "close", "delete", "status",
                            "record_start", "record_stop",
                        ],
                    },
                    "surface_id": {
                        "type": "string",
                        "description": "页面id；状态栏=status-timeline。",
                    },
                    "app_id": {
                        "type": "string",
                        "enum": ["timer", "notes"],
                        "description": "timer计时；notes记事。",
                    },
                    "command": {
                        "type": "string",
                        "enum": ["open", "start", "pause", "resume", "add", "reset", "status", "append", "replace", "clear"],
                    },
                    "duration_seconds": {
                        "type": "integer",
                        "description": "秒；十分钟=600。",
                    },
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string"},
                                "body": {"type": "string"},
                                "items": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "metric": {"type": "string"},
                            },
                        },
                    },
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "text": {"type": "string", "description": "append 的单条文字。"},
                    "count": {
                        "type": "integer",
                    },
                    "url": {
                        "type": "string",
                        "description": "网址；close 时带上=按此站定位关闭。",
                    },
                    "html": {"type": "string"},
                    "css": {"type": "string"},
                    "js": {"type": "string"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "position": {
                        "type": "string",
                        "enum": ["center", "top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right"],
                    },
                    "theme": {
                        "type": "object",
                        "description": (
                            "状态栏配色（surface_id=status-timeline、action=update）。"
                            "mode=auto/dark/light；dark/light 可分别给 surface、surface_2、surface_3、"
                            "text、secondary、tertiary、accent、accent_soft、line、line_strong、danger。"
                            "颜色直接写在 theme 顶层则同时覆盖明暗模式。"
                        ),
                    },
                    "speak_while": {
                        "type": "boolean",
                        "description": "边执行边播开始语。",
                    },
                    "continue_after": {
                        "type": "boolean",
                        "description": "必须填；有后续步骤才true。",
                    },
                    "reply": {
                        "type": "string",
                        "description": "成功后说的那句：你自己的话，口语，十来个字，别套固定句式。",
                    },
                },
                "required": ["action", "continue_after", "reply"],
            },
        },
    }
    if slim:
        # 语音版瘦身：这些参数在 430 次真实调用里一次都没被模型用过，
        # 却占着 schema 预算（position 125 字符、items 57 字符）。
        # 能力本身没删——几何用 x/y/width/height、追加用 text，执行器照常接受。
        for unused in ("position", "items"):
            definition["function"]["parameters"]["properties"].pop(unused, None)
        definition["function"]["parameters"]["properties"]["theme"] = {
            "type": "object",
            "description": "状态栏配色：mode；dark/light 或直接给 surface、text、accent 等颜色。",
        }
    return definition


_ACCENTS = {
    "mint": ("#8fefbd", "#19372a"),
    "blue": ("#82b7ff", "#182b46"),
    "violet": ("#c59cff", "#302144"),
    "orange": ("#ffb36b", "#402b18"),
    "rose": ("#ff91ad", "#40202a"),
}


def _coerce_page_content(args):
    """接住模型的自然写法，别把内容丢在门口。

    真实事故：模型 create 一个窗口时传的是 content="春节：正月初一\n元宵：…"，
    执行器只认 summary/sections，于是渲染出一个只有标题的空壳——用户看到窗口
    「什么都没写」。surface.new 的描述符里 properties 是空的，模型本来也无从
    知道该用哪个字段名，这里负责把常见写法收敛过去。
    """
    if not isinstance(args, dict):
        return {}
    if args.get("summary") or args.get("sections"):
        return args
    raw = args.get("content")
    text = ""
    items = []
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, dict):
        text = str(raw.get("text") or raw.get("summary") or "")
        items = [str(item) for item in (raw.get("items") or []) if str(item).strip()]
    elif isinstance(raw, list):
        items = [str(item) for item in raw if str(item).strip()]
    if not text and not items:
        text = str(args.get("body") or args.get("text") or "")
        items = [str(item) for item in (args.get("items") or []) if str(item).strip()]
    if not text and not items:
        return args
    if text and not items:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 1:
            items, text = lines, ""
    out = dict(args)
    if items:
        out["sections"] = [{"items": items[:20]}]
        if text:
            out["summary"] = text[:4000]
    else:
        out["summary"] = text[:4000]
    return out


def _structured_page(args):
    args = _coerce_page_content(args)
    title = html.escape(str(args.get("title") or "EV 对话页面")[:160])
    kicker = html.escape(str(args.get("kicker") or "EV · LIVE CANVAS")[:120])
    summary = html.escape(str(args.get("summary") or "")[:4000])
    layout = str(args.get("layout") or "cards")
    accent, accent_dim = _ACCENTS.get(str(args.get("accent") or "mint"), _ACCENTS["mint"])
    section_markup = []
    for raw in list(args.get("sections") or [])[:16]:
        if not isinstance(raw, dict):
            continue
        heading = html.escape(str(raw.get("heading") or "")[:240])
        body = html.escape(str(raw.get("body") or "")[:3000])
        metric = html.escape(str(raw.get("metric") or "")[:160])
        items = [
            html.escape(str(item)[:700])
            for item in list(raw.get("items") or [])[:20]
        ]
        inner = []
        if metric:
            inner.append('<div class="metric">%s</div>' % metric)
        if heading:
            inner.append("<h2>%s</h2>" % heading)
        if body:
            inner.append("<p>%s</p>" % body)
        if items:
            inner.append("<ul>%s</ul>" % "".join("<li>%s</li>" % item for item in items))
        section_markup.append('<section class="card">%s</section>' % "".join(inner))
    if not section_markup and summary:
        section_markup.append('<section class="card"><p>%s</p></section>' % summary)
    markup = (
        '<main class="canvas layout-%s">'
        '<header><span class="kicker">%s</span><h1>%s</h1>%s</header>'
        '<div class="grid">%s</div></main>'
    ) % (
        html.escape(layout),
        kicker,
        title,
        ("<p class=\"summary\">%s</p>" % summary) if summary else "",
        "".join(section_markup),
    )
    css = """
html,body{margin:0;min-height:100%;background:#0d1117;color:#eef2f6;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}
body{background:radial-gradient(circle at 92% 0,__ACCENT_DIM__ 0,transparent 42%),#0d1117}
.canvas{padding:34px;max-width:1160px;margin:auto}.kicker{display:inline-block;color:__ACCENT__;font:700 11px ui-monospace;letter-spacing:.16em;margin-bottom:14px}
h1{font-size:clamp(30px,5vw,60px);line-height:1.02;letter-spacing:-.04em;margin:0;max-width:880px}.summary{max-width:760px;color:#aeb8c5;font-size:16px;line-height:1.7;margin:18px 0 0}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:30px}.card{border:1px solid #28313d;background:linear-gradient(145deg,#171d25e8,#11161de8);border-radius:18px;padding:20px;box-shadow:0 18px 55px #0005}
.card h2{font-size:16px;margin:0 0 10px}.card p,.card li{color:#b9c2ce;line-height:1.65}.card p{margin:0}.card ul{padding-left:20px;margin:8px 0 0}.metric{color:__ACCENT__;font-size:30px;font-weight:750;margin-bottom:10px}
.layout-list .grid{grid-template-columns:1fr}.layout-focus .grid{grid-template-columns:1fr}.layout-focus .card:first-child{padding:28px;border-color:__ACCENT__}
@media(max-width:680px){.canvas{padding:24px}.grid{grid-template-columns:1fr}}
""".replace("__ACCENT_DIM__", accent_dim).replace("__ACCENT__", accent)
    return markup, css


def _focused_surface_id():
    """只接受唯一聚焦窗口；不从标题或用户原话做关键词推断。"""
    focused = list(scene_store.inspect(scope="focused").get("surfaces") or [])
    if len(focused) != 1:
        return ""
    return str(focused[0].get("id") or "").strip()


def _host_of(url):
    """取 URL 主机名，去掉 www.。非法/空则返回空串。"""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _norm_host(host):
    """主机名归一化到可比较的字母数字串（bilibili.com → bilibilicom）。"""
    return re.sub(r"[^a-z0-9]", "", str(host or "").lower())


def _url_surface_id(url):
    """从 URL 生成稳定窗口 id：同一网站永远落到同一个 id，避免每次打开都新建窗口。"""
    host = _host_of(url)
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    return ("web-" + slug) if slug else ""


def _default_page_title(url):
    """打开网站没给标题时，用网站名兜底，别让所有页面都叫「EV 对话页面」。

    取主机的二级域名并首字母大写（bilibili.com→Bilibili，youtube.com→YouTube 近似）。
    非网页页面维持通用标题。
    """
    host = _host_of(url)
    if not host:
        return "EV 对话页面"
    parts = [p for p in host.split(".") if p]
    label = parts[-2] if len(parts) >= 2 else parts[0] if parts else ""
    return label[:1].upper() + label[1:] if label else host


def _surface_url(item):
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    return str(content.get("url") or "")


def _close_targets(args, requested_surface_id):
    """关闭前先 inspect 真实场景，解析出真正要关的窗口，绝不照搬模型猜的 id。

    匹配优先级：显式 url 的主机 → 稳定 id(web-<host>) 反推的主机 → 命中的具体
    surface_id → 兜底为当前唯一聚焦/唯一可见的非常驻窗口。命中同一网站的多个
    窗口时全部返回（支持「都关上」）。

    返回 (visible_ids, matched_any)：visible_ids 是其中当前可见、需要真正下发
    关闭的窗口；matched_any 表示是否匹配到任何目标（含已隐藏的），用于区分
    「已经是关闭状态」与「根本没有这个窗口」。
    """
    # 常驻窗（信息推送/状态栏）也要能被「明确点名」关闭；只是在没点名、
    # 需要猜目标时不把它们算进去，避免误关。以前一律过滤 → 点名关它时匹配为空
    # → 上层回「没有开着的页面可关」并判成功，窗口却还开着（假回执）。
    surfaces = list(scene_store.inspect(scope="all").get("surfaces") or [])
    by_id = {str(s.get("id")): s for s in surfaces}

    target_host = _norm_host(_host_of(args.get("url"))) if args.get("url") else ""
    if not target_host and requested_surface_id.startswith("web-"):
        target_host = _norm_host(requested_surface_id[4:])

    matched = []
    if target_host:
        matched = [
            str(s.get("id")) for s in surfaces
            if _norm_host(_host_of(_surface_url(s))) == target_host
        ]
    elif requested_surface_id and requested_surface_id in by_id:
        matched = [requested_surface_id]

    if not matched:
        # 兜底猜目标时排除常驻窗：用户没点名就不该动它们
        visible = [
            s for s in surfaces
            if s.get("visible")
            and not surface_tools.is_pinned_surface(str(s.get("id") or ""))
        ]
        focused = [s for s in visible if s.get("focused")]
        if len(focused) == 1:
            matched = [str(focused[0].get("id"))]
        elif len(visible) == 1:
            matched = [str(visible[0].get("id"))]

    visible_ids = [sid for sid in matched if by_id.get(sid, {}).get("visible")]
    return visible_ids, bool(matched)


def _target_required(action):
    meta = {
        "ok": False,
        "action": action,
        "reason": "surface_target_required",
        "detail": "没有唯一的目标窗口；请指定 surface_id，或先让目标窗口获得焦点。",
    }
    return json.dumps(meta, ensure_ascii=False), meta


def _success_speech(action, surface_id):
    """为终态动作补齐确定性播报；只看已执行的结构化动作与成功回执。"""
    if action == "close":
        return (
            "状态栏已隐藏"
            if surface_id == surface_tools.STATUS_TIMELINE_SURFACE
            else "窗口已关闭"
        )
    if action == "delete":
        return "窗口已删除"
    if action == "append":
        return "内容已追加"
    if action == "record_start":
        return "开始记录了"
    if action == "record_stop":
        return "已停止记录"
    return ""


def _info_panel_action(action, args, natural_reply):
    """信息推送已不是独立窗口，而是状态栏的展开区：
    close/delete → 收起；create/update/append → 展开（有内容才展得开）。

    空操作（本来就是这个状态）绝不采用模型写的那句话：模型选错目标时也会走到
    这里，它写的是「放大了」「窗口重新打开了」——描述的动作根本没发生。
    实测过一次：用户要放大 GitHub 榜单窗口，模型却把目标写成 status-timeline，
    面板本就展开着，却照样回了成功，于是播出「放大了」这句假回执。
    """
    from control_plane import info_panel

    expand = action not in ("close", "delete")
    was_expanded = bool(info_panel.snapshot().get("expanded"))

    if expand and not info_panel.has_content():
        meta = {
            "ok": True,
            "action": action,
            "surface_id": surface_tools.PINNED_INFO_SURFACE,
            "expanded": False,
            "changed": False,
            "speech": "现在没有推送内容",
        }
        return json.dumps(meta, ensure_ascii=False), meta

    changed = was_expanded != expand
    if changed:
        info_panel.set_expanded(expand)
        surface_tools.set_status_timeline_expanded(expand)
    meta = {
        "ok": True,
        "action": action,
        "surface_id": surface_tools.PINNED_INFO_SURFACE,
        "expanded": expand,
        "changed": changed,
        "speech": (
            ("信息推送已展开" if expand else "信息推送已收起")
            if changed
            else ("信息推送本来就是展开的" if expand else "信息推送本来就是收起的")
        ),
    }
    if not changed:
        # 提示模型：这可能是选错目标了，要操作别的窗口得给那个窗口的 id
        meta["detail"] = (
            "状态没有变化。若你其实想操作别的窗口，请先 status 查到它的 "
            "surface_id 再操作，不要用 status-timeline。"
        )
    if changed and natural_reply:
        meta["direct_reply"] = natural_reply
    return json.dumps(meta, ensure_ascii=False), meta


def execute(arguments=None, *, aid=None):
    args = dict(arguments or {})
    action = str(args.get("action") or "")
    if action == "app":
        return surface_apps.execute(args, aid=aid)
    # Retrieved 3D assets belong to the versioned research canvas.  A model
    # occasionally tried to fake that capability by opening a one-off Three.js
    # page which depended on remote scripts and then claimed it was loaded.
    # Fail closed at the executor boundary; genuine 3D web development still
    # goes through the coding workflow where it can be built and verified.
    custom_code = "\n".join(
        str(args.get(key) or "") for key in ("title", "html", "css", "js")
    )
    if action in {"create", "update", "show"} and re.search(
        r"GLTFLoader|model-viewer|three(?:\.module)?\.js|OrbitControls|"
        r"https?://[^\s\"']+\.(?:glb|gltf)",
        custom_code,
        re.I,
    ):
        meta = {
            "ok": False,
            "action": action,
            "reason": "use_research_canvas",
            "detail": (
                "不能用临时 HTML/远程脚本冒充 3D 预览。搜索真实 GLB/glTF 后，"
                "由研究画布的 model 节点显示；需要开发 3D 网页则走写码流程。"
            ),
        }
        return json.dumps(meta, ensure_ascii=False), meta
    requested_surface_id = str(args.get("surface_id") or "").strip()
    # 稳定窗口复用：打开网站时若没点名 surface_id，就用 URL 主机生成稳定 id
    # （web-<host>）。这样「再打开一次 bilibili」永远命中同一个窗口，不再每次
    # 都新建 surface-<uuid>、把场景堆成一堆重复窗口。
    url_arg = str(args.get("url") or "").strip()
    if (
        url_arg
        and action in ("create", "update", "show")
        and not surface_tools.is_local_page(url_arg)
        and not surface_tools.web_windows_enabled()
    ):
        # 网站归浏览器。这里不再造壳窗口，并且把该走哪条路说清楚——
        # 只说「不支持」，模型会换个参数接着试。
        meta = {
            "ok": False,
            "action": action,
            "reason": "web_window_retired",
            "error": "网站不在桌面窗口里开了。用浏览器对象（object_control invoke，"
                     "命令 new_page，参数 url）真正打开它。",
        }
        return json.dumps(meta, ensure_ascii=False), meta
    if url_arg and not requested_surface_id and action in ("create", "update", "show"):
        stable_id = _url_surface_id(url_arg)
        if stable_id:
            requested_surface_id = stable_id
    legacy_show = action == "show"
    if action == "create":
        surface_id = requested_surface_id or ("surface-" + uuid.uuid4().hex[:10])
    elif legacy_show:
        # 仅供旧内部调用兼容；voice schema 不再暴露 show。
        surface_id = requested_surface_id or DEFAULT_SURFACE_ID
    else:
        surface_id = requested_surface_id or _focused_surface_id()
    natural_reply = str(args.get("reply") or "").strip()[:240]
    # 信息推送区的展开/收起：模型可能点名 info-board（旧习惯），也可能点名
    # status-timeline（面板确实长在状态栏上）。两种都接住，否则会退化成
    # 「隐藏整个状态栏」或「把状态栏拉成 800 高」这类离谱几何操作。
    if action in ("close", "delete", "create", "update", "append"):
        from control_plane import info_panel
        if requested_surface_id == surface_tools.PINNED_INFO_SURFACE:
            return _info_panel_action(action, args, natural_reply)
        if requested_surface_id == surface_tools.STATUS_TIMELINE_SURFACE:
            if action == "update" and isinstance(args.get("theme"), dict):
                meta = surface_tools.set_status_timeline_theme(args["theme"])
                meta = dict(meta or {})
                meta["action"] = "update"
                if meta.get("ok") and meta.get("changed"):
                    meta["speech"] = "状态栏配色已更新"
                    if natural_reply:
                        meta["direct_reply"] = natural_reply
                elif meta.get("ok"):
                    meta["speech"] = "状态栏已经是这套配色"
                return json.dumps(meta, ensure_ascii=False), meta
            panel_open = info_panel.snapshot()["expanded"]
            # 收起：面板开着就先收面板，而不是把状态栏整个藏掉
            if action in ("close", "delete") and panel_open:
                return _info_panel_action("close", args, natural_reply)
            # 状态栏没有别的合法写操作：它的几何由面板状态托管、内容是固定页面。
            # 所以 create/update/append 一律理解为「展开信息推送」。
            # 以前要求「带几何参数或面板已关」，而模型发来的是不带任何参数的
            # update，条件不成立就落到普通窗口更新，面板纹丝不动却回「展开了」。
            if action in ("create", "update", "append"):
                return _info_panel_action("update", args, natural_reply)

    def attach_reply(result):
        _text, meta = result
        meta = dict(meta or {})
        if meta.get("ok"):
            speech = _success_speech(action, surface_id)
            if speech and not str(meta.get("speech") or "").strip():
                meta["speech"] = speech
            if natural_reply:
                meta["direct_reply"] = natural_reply
        return json.dumps(meta, ensure_ascii=False), meta

    if action == "status":
        target_id = requested_surface_id or "current"
        _text, inspection = surface_tools.surface_inspect_execute({
            "scope": "id",
            "surface_id": target_id,
        })
        surfaces = list((inspection or {}).get("surfaces") or [])
        if not surfaces:
            meta = {
                "ok": False,
                "action": "status",
                "surface_id": target_id,
                "reason": "surface_not_found",
            }
            return json.dumps(meta, ensure_ascii=False), meta
        item = surfaces[0]
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        content = data.get("content") if isinstance(data.get("content"), dict) else {}
        meta = {
            "ok": True,
            "action": "status",
            "surface_id": str(item.get("id") or target_id),
            "title": str(data.get("title") or ""),
            "visible": bool(item.get("visible")),
            "focused": bool(item.get("focused")),
            "bounds": item.get("bounds"),
            "content_type": str(content.get("type") or ""),
            "rendered": bool(item.get("renderer_ready")),
            "content_status": str(item.get("content_status") or "unknown"),
        }
        if str(item.get("id") or target_id) == surface_tools.STATUS_TIMELINE_SURFACE:
            meta["theme"] = surface_tools.status_timeline_theme()
        return json.dumps(meta, ensure_ascii=False), meta
    if action == "close":
        # 状态栏是常驻窗，隐藏走原路径（_close_targets 会把常驻窗过滤掉）。
        if requested_surface_id == surface_tools.STATUS_TIMELINE_SURFACE:
            return attach_reply(surface_tools.surface_manage_execute({
                "action": "close",
                "surface_id": requested_surface_id,
            }))
        # 关闭前先 inspect 真实场景解析目标，不照搬模型猜的 id；命中同一网站的
        # 多个窗口时全部关闭（支持「都关上」）。
        visible_ids, matched_any = _close_targets(args, requested_surface_id)
        if not visible_ids and not matched_any:
            # 没点名、也没有普通窗口可关，而信息推送正展开着：
            # 用户说的「关掉/收起来」几乎只可能是指它。不接这一手就会
            # 回「没有开着的页面可关」并判成功，面板却还开着（假回执）。
            from control_plane import info_panel
            if info_panel.snapshot()["expanded"]:
                return _info_panel_action("close", args, natural_reply)
        if not visible_ids:
            # 目标本来就没开着（或压根没这个窗口）：这是无操作，算成功，
            # 绝不再谎报「没有收到成功回执」。
            meta = {
                "ok": True,
                "action": "close",
                "surface_id": requested_surface_id,
                "closed": [],
                "count": 0,
                "already_closed": bool(matched_any),
                "speech": "已经关了" if matched_any else "没有开着的页面可关",
            }
            if natural_reply:
                meta["direct_reply"] = natural_reply
            return json.dumps(meta, ensure_ascii=False), meta
        closed = []
        for sid in visible_ids:
            _t, m = surface_tools.surface_manage_execute({
                "action": "close",
                "surface_id": sid,
            })
            if m.get("ok"):
                closed.append(sid)
        meta = {
            "ok": len(closed) == len(visible_ids),
            "action": "close",
            "closed": closed,
            "count": len(closed),
            "surface_id": closed[0] if len(closed) == 1 else "",
        }
        if closed:
            meta["speech"] = (
                "窗口已关闭" if len(closed) == 1 else "关闭了%d个窗口" % len(closed)
            )
        else:
            meta["reason"] = "close_failed"
        if natural_reply:
            meta["direct_reply"] = natural_reply
        return json.dumps(meta, ensure_ascii=False), meta
    if not surface_id and action != "record_stop":
        return _target_required(action)
    if action in ("record_start", "record_stop"):
        return attach_reply(surface_tools.surface_expect_input_execute({
            "action": "start" if action == "record_start" else "stop",
            "surface_id": surface_id,
            "count": args.get("count") or 10,
            "path": "/content/items",
        }, aid))
    if action == "delete":
        return attach_reply(surface_tools.surface_manage_execute({
            "action": action,
            "surface_id": surface_id,
        }))
    if action == "append":
        values = [str(item) for item in list(args.get("items") or [])]
        if args.get("text") is not None:
            values.append(str(args.get("text")))
        if not values:
            meta = {"ok": False, "action": action, "error": "没有要追加的内容"}
            return json.dumps(meta, ensure_ascii=False), meta
        current = scene_store.get(surface_id) or {}
        current_data = current.get("data") if isinstance(current.get("data"), dict) else {}
        current_content = current_data.get("content") if isinstance(current_data.get("content"), dict) else {}
        if str(current_content.get("type") or "") in {"html", "app", "chart"}:
            old_items = list(current_content.get("items") or [])
            addition = "".join(
                '<section class="card captured"><p>%s</p></section>'
                % html.escape(value[:4000])
                for value in values
            )
            return attach_reply(surface_tools.surface_manage_execute({
                "action": "set",
                "surface_id": surface_id,
                "content": {
                    "html": str(current_content.get("html") or "") + addition,
                    "items": old_items + values,
                },
            }))
        return attach_reply(surface_tools.surface_manage_execute({
            "action": "append",
            "surface_id": surface_id,
            "path": "/content/items",
            "items": values,
        }))
    if action not in {"create", "update", "show"}:
        meta = {"ok": False, "action": action, "error": "未知的 surface_control action"}
        return json.dumps(meta, ensure_ascii=False), meta

    current = scene_store.get(surface_id)
    if legacy_show and surface_id == surface_tools.STATUS_TIMELINE_SURFACE and not current:
        surface_tools.ensure_status_timeline_surface()
        current = scene_store.get(surface_id)
    operation = (
        ("update" if current else "create")
        if legacy_show
        else action
    )
    if operation == "create" and current:
        # 稳定 id 复用：同一网站再次打开 = 复用已有窗口并置前，不当作重复创建报错。
        if url_arg:
            operation = "update"
        else:
            meta = {
                "ok": False,
                "action": "create",
                "surface_id": surface_id,
                "reason": "surface_id_exists",
                "detail": "create 不能覆盖已有窗口；请省略 surface_id 生成新窗口，或改用 update。",
            }
            return json.dumps(meta, ensure_ascii=False), meta
    if operation == "update" and not current:
        meta = {
            "ok": False,
            "action": "update",
            "surface_id": surface_id,
            "reason": "surface_not_found",
            "detail": "update 只能修改已有窗口；需要新窗口请改用 create。",
        }
        return json.dumps(meta, ensure_ascii=False), meta
    current_data = (
        current.get("data")
        if isinstance(current, dict) and isinstance(current.get("data"), dict)
        else {}
    )
    current_content = (
        current_data.get("content")
        if isinstance(current_data.get("content"), dict)
        else {}
    )
    has_page_content = any(
        args.get(key) is not None
        for key in ("url", "html", "css", "js", "kicker", "summary", "sections", "accent", "layout")
    )
    content = None
    if args.get("url"):
        content = {"url": str(args.get("url") or "")}
    elif any(args.get(key) is not None for key in ("html", "css", "js")):
        # 修改代码页时只覆盖模型明确提交的字段，避免“只调 CSS”把原 HTML 清空。
        # URL 页不能注入局部 CSS/JS；只有显式给 html 才表示切换为自定义页。
        if str(current_content.get("type") or "") == "url" and args.get("html") is None:
            meta = {
                "ok": False,
                "action": "show",
                "surface_id": surface_id,
                "reason": "url_page_requires_html",
                "detail": "外部网站不能只注入 CSS/JS；请提供完整 html 或改用新的 url。",
            }
            return json.dumps(meta, ensure_ascii=False), meta
        content = {
            key: str(args.get(key) or "")
            for key in ("html", "css", "js")
            if args.get(key) is not None
        }
        if str(current_content.get("type") or "") in {"html", "app"}:
            content = {
                key: str(current_content.get(key) or "")
                for key in ("html", "css", "js")
                if key in current_content
            } | content
    elif has_page_content or operation == "create":
        markup, css = _structured_page(args)
        content = {"html": markup, "css": css}
    window = {
        key: args[key]
        for key in ("width", "height", "x", "y", "position")
        if args.get(key) is not None
    }
    payload = {
        "action": "open",
        "surface_id": surface_id,
        "focus": (
            False
            if surface_id == surface_tools.STATUS_TIMELINE_SURFACE
            else args.get("focus") is not False
        ),
    }
    if args.get("title") is not None or operation == "create":
        payload["title"] = str(args.get("title") or _default_page_title(url_arg))
    if content is not None:
        payload["content"] = content
    if window:
        payload["window"] = window
    text, meta = surface_tools.surface_manage_execute(payload)
    meta = dict(meta or {})
    meta["action"] = "show" if legacy_show else operation
    meta["operation"] = operation
    meta["presentation"] = "url" if args.get("url") else (
        "custom_code" if any(args.get(key) is not None for key in ("html", "css", "js"))
        else ("structured" if content is not None else "preserved")
    )
    if meta.get("ok") and not legacy_show:
        title = str(meta.get("title") or args.get("title") or surface_id)
        meta["speech"] = (
            "%s已创建" % title
            if operation == "create"
            else "%s已更新" % title
        )
    if meta.get("ok") and natural_reply:
        meta["direct_reply"] = natural_reply
    return text, meta


def register(registry, *, wrapper=None):
    def fn(args, ctx):
        ctx = ctx if isinstance(ctx, dict) else {}
        return execute(args, aid=ctx.get("aid"))

    final_fn = wrapper(fn, "surface_control") if wrapper else fn
    registry.register("surface_control", final_fn, conflicts="surface_id")
