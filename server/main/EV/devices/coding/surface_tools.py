# -*- coding: utf-8 -*-
"""窗口能力工具：surface_manage / surface_inspect / surface_expect_input 定义、执行、注册。

从 surfaces.py 拆出的工具层。拥有 _PENDING_INPUT（surface_expect_input 的
记录模式状态），供 surface_hints 读取。

「回执即真相」：每个动作返回真实状态回执（ok/visible/bounds/content_status），
模型只能照回执汇报，禁止编造窗口状态。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import uuid

from devices.coding.scene_store import scene_store
from devices.coding.surface_layout import (
    _append_apply_patches,
    attach_fit_beacon,
    auto_place_window,
    deep_merge_dict,
    json_patch_apply,
    json_pointer_parts,
    normalize_web_surface_definition,
    reject_relative_geometry,
    safe_surface_color,
    surface_resolve_id,
)

# surface_expect_input 的有限待捕获输入（aid → {surface_id, remaining, path}）
_PENDING_INPUT: dict = {}
_PENDING_LOCK = threading.RLock()

# 常驻信息窗口：永远存在、不可被 close/delete 删除的固定窗口。
# 搜索结果/推送类内容自动进这里，固定 id 保证后续更新覆盖同一窗口；
# 用户要特殊展示时，agent 再从这些信息专门新建窗口。
PINNED_INFO_SURFACE = "info-board"
PINNED_INFO_TITLE = "信息推送"

# 实时 Agent 状态窗口：展示边说边出的识别文字、运行阶段和语音控制。
# 常驻且不被对话工具误关；用户可在窗口内主动暂停或停止 Agent。
STATUS_TIMELINE_SURFACE = "status-timeline"
STATUS_TIMELINE_TITLE = "EV 实时状态"
STATUS_UI_VERSION = "40"
WORK_HUD_SURFACE = "work-hud"

# 状态栏两档高度：紧凑条 / 展开信息推送区。信息推送不再是独立窗口，
# 而是状态栏往下展开的一块，风格与状态栏同源。
STATUS_COLLAPSED_WIDTH = 380
STATUS_COLLAPSED_HEIGHT = 132
STATUS_CANVAS_WIDTH = STATUS_COLLAPSED_WIDTH
# Normal expanded windows start compact, then the page reports its exact
# rendered height. The maximum keeps media/table views usable without letting
# a long result take over the screen.
STATUS_EXPANDED_MIN_HEIGHT = 184
STATUS_EXPANDED_FALLBACK_HEIGHT = 252
# 展开上限：320 → 460 → 720。清单改为常显编号条目后，前两个数都只够露出
# 头一两条，用户看到的是「说到一半没了」——面板高度本该跟着内容走，
# 上限只是不让它吃掉整块屏幕。实际高度由页面实测上报（/info_panel/measure），
# 再由窗口层按工作区裁剪；超出部分仍可滚动，沉浸模式另有 760。
STATUS_EXPANDED_HEIGHT = 720
STATUS_IMMERSIVE_WIDTH = 1120
STATUS_IMMERSIVE_HEIGHT = 760

_PINNED_SURFACES = frozenset({STATUS_TIMELINE_SURFACE, WORK_HUD_SURFACE})

_STATUS_THEME_COLOR_KEYS = frozenset({
    "workspace", "surface", "surface_2", "surface_3", "line", "line_strong",
    "text", "secondary", "tertiary", "accent", "accent_soft", "danger",
})


def is_pinned_surface(surface_id: str) -> bool:
    """窗口是否为系统预置常驻 surface（不可删除，状态栏允许临时隐藏）。"""
    return str(surface_id or "") in _PINNED_SURFACES


def status_timeline_theme() -> dict:
    """Return the persisted UI palette for the status surface."""
    current = scene_store.get(STATUS_TIMELINE_SURFACE) or {}
    data = current.get("data") if isinstance(current.get("data"), dict) else {}
    theme = data.get("theme") if isinstance(data.get("theme"), dict) else {}
    status_ui = theme.get("status_ui") if isinstance(theme.get("status_ui"), dict) else {}
    return copy.deepcopy(status_ui)


def set_status_timeline_theme(changes: dict) -> dict:
    """Persist a safe, token-based palette without replacing the status page."""
    if not isinstance(changes, dict):
        return {"ok": False, "reason": "invalid_theme", "detail": "theme 必须是对象"}

    current_surface = scene_store.get(STATUS_TIMELINE_SURFACE)
    if not current_surface:
        ensure_status_timeline_surface()
        current_surface = scene_store.get(STATUS_TIMELINE_SURFACE)
    current_data = (
        current_surface.get("data")
        if isinstance(current_surface, dict) and isinstance(current_surface.get("data"), dict)
        else {}
    )
    current_theme = current_data.get("theme") if isinstance(current_data.get("theme"), dict) else {}
    previous = current_theme.get("status_ui") if isinstance(current_theme.get("status_ui"), dict) else {}
    next_theme = copy.deepcopy(previous)

    mode = str(changes.get("mode") or "").strip().lower()
    if mode:
        if mode not in {"auto", "dark", "light"}:
            return {"ok": False, "reason": "invalid_theme_mode", "detail": "mode 只能是 auto、dark 或 light"}
        next_theme["mode"] = mode

    invalid = []

    def merge_palette(target: dict, source: dict) -> None:
        for raw_key, raw_value in source.items():
            key = str(raw_key or "").strip().lower().replace("-", "_")
            if key == "background":
                key = "surface"
            if key not in _STATUS_THEME_COLOR_KEYS:
                continue
            color = safe_surface_color(raw_value, "")
            if not color:
                invalid.append(str(raw_key))
                continue
            target[key] = color

    merge_palette(next_theme, changes)
    for palette_name in ("dark", "light"):
        raw_palette = changes.get(palette_name)
        if not isinstance(raw_palette, dict):
            continue
        palette = next_theme.get(palette_name) if isinstance(next_theme.get(palette_name), dict) else {}
        palette = copy.deepcopy(palette)
        merge_palette(palette, raw_palette)
        next_theme[palette_name] = palette

    if invalid:
        return {
            "ok": False,
            "reason": "invalid_theme_color",
            "detail": "这些颜色值无效：%s" % "、".join(invalid),
        }
    if next_theme == previous:
        return {
            "ok": True,
            "changed": False,
            "surface_id": STATUS_TIMELINE_SURFACE,
            "theme": next_theme,
        }

    next_data = copy.deepcopy(current_data)
    next_data["theme"] = {**current_theme, "status_ui": next_theme}
    result = scene_store.upsert(
        STATUS_TIMELINE_SURFACE,
        kind=current_surface.get("kind") or "web-surface",
        data=next_data,
        intent=current_surface.get("intent") or "inform",
        focus=False,
        visible=current_surface.get("visible") is not False,
    )
    return {
        "ok": True,
        "changed": bool(result.get("changed")),
        "surface_id": STATUS_TIMELINE_SURFACE,
        "theme": next_theme,
        "rev": result.get("rev"),
    }


def ensure_pinned_surfaces():
    """信息推送已并入状态栏的展开区，不再是独立窗口。

    这里负责一次性迁移：把历史遗留的 info-board 窗口销毁掉，避免它继续以
    独立窗口的形式浮在桌面上（内容改由 control_plane.info_panel 承载）。
    """
    if scene_store.get(PINNED_INFO_SURFACE):
        scene_store.remove(PINNED_INFO_SURFACE)
    if scene_store.get("coding-studio"):
        scene_store.remove("coding-studio")
    work_hud = scene_store.get(WORK_HUD_SURFACE)
    if work_hud and work_hud.get("visible") is not False:
        data = work_hud.get("data") if isinstance(work_hud.get("data"), dict) else {}
        scene_store.upsert(
            WORK_HUD_SURFACE,
            kind=work_hud.get("kind") or "web-surface",
            data=data,
            intent=work_hud.get("intent") or "inform",
            focus=False,
            visible=False,
        )


def ensure_status_timeline_surface():
    """确保实时 Agent 状态窗口存在（幂等）。"""
    from devices.coding.surface_layout import normalize_web_surface_definition
    port = os.environ.get("MUSE_PORT", "8002")
    target_url = "http://127.0.0.1:%s/static/status_timeline.html?v=%s" % (
        port, STATUS_UI_VERSION,
    )
    definition = {
        "title": "EV",
        "content": {"url": target_url},
        "window": {
            "width": STATUS_COLLAPSED_WIDTH,
            "height": STATUS_COLLAPSED_HEIGHT,
            "compact": True,
            "always_on_top": False,
        },
        "theme": {
            "background": "rgba(0, 0, 0, 0)",
            "foreground": "#f0eee9",
            "accent": "#c37a6d",
            "radius": 17,
        },
    }
    existing = scene_store.get(STATUS_TIMELINE_SURFACE)
    if existing:
        # 保留用户拖动后的 x/y，但将旧的大时间线迁移为新的紧凑浮层。
        current_data = (
            existing.get("data") if isinstance(existing, dict) else None
        )
        if isinstance(current_data, dict):
            current_theme = current_data.get("theme")
            if isinstance(current_theme, dict):
                # Version migrations must not wipe a palette chosen by the user.
                definition["theme"] = deep_merge_dict(definition["theme"], current_theme)
            content = current_data.get("content") or {}
            window = current_data.get("window") or {}
            current_height = int(window.get("height") or 0)
            normal_dynamic_height = (
                STATUS_EXPANDED_MIN_HEIGHT
                <= current_height
                <= STATUS_EXPANDED_HEIGHT
            )
            needs_migration = (
                str(content.get("url") or "") != target_url
                or int(window.get("width") or 0) not in (
                    STATUS_COLLAPSED_WIDTH, STATUS_CANVAS_WIDTH, STATUS_IMMERSIVE_WIDTH,
                )
                or not (
                    current_height in (STATUS_COLLAPSED_HEIGHT, STATUS_IMMERSIVE_HEIGHT)
                    or normal_dynamic_height
                )
                or window.get("always_on_top") is not False
            )
            if needs_migration:
                data = normalize_web_surface_definition(
                    definition,
                    current=current_data,
                )
                scene_store.upsert(
                    STATUS_TIMELINE_SURFACE,
                    kind="web-surface",
                    data=data,
                    intent="inform",
                    focus=False,
                    visible=True,
                )
        return
    data = normalize_web_surface_definition(definition, current={})
    scene_store.upsert(
        STATUS_TIMELINE_SURFACE,
        kind="web-surface",
        data=data,
        intent="inform",
        focus=False,
        visible=True,
    )


def reconcile_status_timeline_height() -> None:
    """启动时对齐状态栏高度与面板内容。

    面板内容存在内存（进程重启即空），窗口高度存在磁盘（重启后仍是 560）。
    不对齐的话，重启后状态栏会顶着一截空白展开区；用户说「收起」时面板本就
    是空的，逻辑会误判成「没得可收」而去隐藏整个状态栏。
    """
    try:
        from control_plane import info_panel
    except Exception:
        return
    if not info_panel.has_content():
        set_status_timeline_expanded(False)


def _status_timeline_target_height(
    expanded: bool,
    *,
    immersive: bool = False,
    measured_height: int | None = None,
    current_height: int = 0,
) -> int:
    if not expanded:
        return STATUS_COLLAPSED_HEIGHT
    if immersive:
        return STATUS_IMMERSIVE_HEIGHT
    if measured_height is not None:
        return max(
            STATUS_EXPANDED_MIN_HEIGHT,
            min(STATUS_EXPANDED_HEIGHT, int(measured_height or 0)),
        )
    if STATUS_EXPANDED_MIN_HEIGHT <= current_height <= STATUS_EXPANDED_HEIGHT:
        return current_height
    return STATUS_EXPANDED_FALLBACK_HEIGHT


def set_status_timeline_expanded(
    expanded: bool,
    *,
    immersive: bool = False,
    measured_height: int | None = None,
) -> bool:
    """展开/收起状态栏的信息推送区：只改窗口高度，保留用户拖过的位置。

    返回是否真的发生了变化（高度本就正确时不重复下发，避免无谓重推）。
    """
    ensure_status_timeline_surface()
    current = scene_store.get(STATUS_TIMELINE_SURFACE)
    if not current:
        return False
    data = current.get("data") if isinstance(current.get("data"), dict) else {}
    window = dict(data.get("window") or {})
    target_height = _status_timeline_target_height(
        expanded,
        immersive=immersive,
        measured_height=measured_height,
        current_height=int(window.get("height") or 0),
    )
    target_width = (
        STATUS_IMMERSIVE_WIDTH if expanded and immersive
        else STATUS_CANVAS_WIDTH if expanded
        else STATUS_COLLAPSED_WIDTH
    )
    if (
        int(window.get("height") or 0) == target_height
        and int(window.get("width") or 0) == target_width
    ):
        return False
    window["height"] = target_height
    window["width"] = target_width
    next_data = copy.deepcopy(data)
    next_data["window"] = window
    scene_store.upsert(
        STATUS_TIMELINE_SURFACE,
        kind=current.get("kind") or "web-surface",
        data=next_data,
        intent=current.get("intent") or "inform",
        focus=False,
        visible=True,
    )
    return True


def set_status_timeline_measured_height(height: int) -> bool:
    """Apply the page's real rendered height only while its canvas is open."""
    try:
        from control_plane import info_panel
        state = info_panel.snapshot()
    except Exception:
        return False
    document = state.get("document") if isinstance(state.get("document"), dict) else {}
    view = document.get("view") if isinstance(document.get("view"), dict) else {}
    if not state.get("expanded") or not document or view.get("fullscreen"):
        return False
    return set_status_timeline_expanded(True, measured_height=int(height or 0))


def sync_status_timeline_to_canvas() -> bool:
    """Make native window geometry follow the canvas' verified view state."""
    try:
        from control_plane import info_panel
        state = info_panel.snapshot()
    except Exception:
        return False
    document = state.get("document") if isinstance(state.get("document"), dict) else {}
    view = document.get("view") if isinstance(document.get("view"), dict) else {}
    return set_status_timeline_expanded(
        bool(state.get("expanded") and document),
        immersive=bool(view.get("fullscreen")),
    )


def _clear_pending_for_surface(surface_id: str) -> None:
    """窗口被 close/delete 后，清除所有指向它的待捕获输入。

    否则 expect_input 一开，用户关掉窗口后每句话仍被吞进已关闭的窗口，
    甚至「别记了」这句停止指令也会被记录。
    """
    with _PENDING_LOCK:
        stale = [
            aid for aid, pending in _PENDING_INPUT.items()
            if pending.get("surface_id") == surface_id
        ]
        for aid in stale:
            _PENDING_INPUT.pop(aid, None)


# ==================== 工具定义 ====================
def tool_definitions():
    return [
        surface_manage_tool_definition(),
        surface_inspect_tool_definition(),
        surface_expect_input_tool_definition(),
    ]


def surface_manage_tool_definition():
    return {
        "type": "function",
        "function": {
            "name": "surface_manage",
            "description": (
                "通用原生窗口工具。action=open 显示窗口（不存在时自动创建，必须同时给内容）；"
                "create 只定义不显示；close 隐藏保留；delete 销毁；set 整体更新；"
                "patch 就地打补丁（RFC-6902）。同一 surface_id 始终是同一窗口。"
                "窗口=代码：content 直接给 HTML/CSS/JS（沙箱渲染，计时器/交互组件都这么做）"
                "或 url（外部页面），不需要声明内容类型；"
                "窗口=几何：window 是真实窗口参数（宽高/坐标/位置/置顶）。"
                "窗口会自动适配内容排版：内容多则窗口自动放大、内容少则收缩，"
                "所以 width/height 给一个合理的初始值即可（如 620×480），"
                "不必精确预估内容尺寸，除非用户明确要求某个尺寸。"
                "打开一个不存在的新窗口必须同时给内容：用户说「打开某网站/某链接」"
                "就给 definition.content.url=完整网址（http(s)://开头），说「显示某内容」"
                "就给 definition.content.html，否则 open 会失败（open_requires_definition）。"
                "相对描述（更宽/更大/宽一点）不接受：先 surface_inspect 查当前 bounds，"
                "算出目标数值再用 set/patch，不要编造当前值。"
                "更新现有窗口时，definition 里没提到的字段保持窗口现状不变，"
                "不要凭空重建或还原成默认值——只动用户明确要求改的部分。"
                "「重置/归零/重新开始」指窗内运行状态（计时清零、输入清空、播放复位），"
                "不是把整个窗口打回默认模板：比如番茄钟已设 45 分钟，用户说重置，"
                "就把计时归零但时长仍保持 45，除非用户明确要求改时长。"
                "拿不准当前设定时先 surface_inspect 再动手，不要凭印象重建。"
                "指令真的语义模糊、无法合理推断时，用一句话简短问用户确认，"
                "不要自作主张假设数值或范围。"
                "set/patch/append 提交的内容若与窗口当前内容完全一致会被判为无变化"
                "（回执 changed=false、ok=false）；真要改，必须提交与当前不同的内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "open", "close", "delete", "set", "patch"],
                        "description": "open 显示窗口（不存在则自动创建）；create 只定义不显示；close 隐藏保留（用户说「关上/关掉/关闭/收起来」就是这个动作，即使刚改过该窗口内容也要重新调一次）；delete 销毁；set 整体更新；patch 就地打补丁。",
                    },
                    "surface_id": {
                        "type": "string",
                        "description": "稳定 id；current 指向当前聚焦/最近的窗口；create 可不传由系统分配。",
                    },
                    "definition": {
                        "type": "object",
                        "description": "期望的窗口定义 {title, window, theme, content}。",
                    },
                    "title": {"type": "string"},
                    "window": {
                        "type": "object",
                        "description": (
                            "真实浏览器窗口几何参数。宽高/坐标必须是确定的整数，"
                            "不允许『更宽/大一点』等相对词——那会被拒绝，须先 surface_inspect。"
                        ),
                        "properties": {
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "position": {
                                "type": "string",
                                "enum": ["center", "top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right"],
                            },
                            "always_on_top": {"type": "boolean"},
                        },
                    },
                    "theme": {
                        "type": "object",
                        "description": "配色 {background, foreground, accent}，十六进制色值。",
                    },
                    "content": {
                        "type": "object",
                        "description": (
                            "窗口内容即代码：html/css/js 渲染任意 UI（含计时器等交互组件），"
                            "或 url 渲染外部页面。给出代码即可，无需声明内容类型。"
                        ),
                        "properties": {
                            "html": {"type": "string"},
                            "css": {"type": "string"},
                            "js": {"type": "string"},
                            "url": {"type": "string"},
                            "text": {"type": "string"},
                        },
                    },
                    "patches": {
                        "type": "array",
                        "description": (
                            "RFC-6902 补丁，作用于真实定义。只能指向 /title、/window、/theme、"
                            "/content 根；窗内样式/行为改动更新 /content/css、/content/html、/content/js。"
                            "几何字段 value 必须是数值，相对词会被拒绝。"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {"type": "string", "enum": ["add", "replace", "remove"]},
                                "path": {"type": "string"},
                                "value": {},
                            },
                            "required": ["op", "path"],
                        },
                    },
                    "focus": {"type": "boolean", "description": "打开时是否聚焦置前，默认 true。"},
                },
                "required": ["action"],
            },
        },
    }


def surface_inspect_tool_definition():
    return {
        "type": "function",
        "function": {
            "name": "surface_inspect",
            "description": (
                "只读查询真实桌面窗口清单/可见性/聚焦/几何，永不从对话推测。"
                "仅在用户问『有哪些窗口/多大/在哪』，或相对调整（更宽/更大）需要先查"
                "当前几何时使用；查完要继续调用 surface_manage 完成动作，不要停在查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["all", "visible", "focused", "id"]},
                    "surface_id": {"type": "string"},
                },
                "required": ["scope"],
            },
        },
    }


def surface_expect_input_tool_definition(*, slim=False):
    """「记录模式」工具定义。slim=True 返回精简版（低频使用，描述一行）。"""
    if slim:
        return {
            "type": "function",
            "function": {
                "name": "surface_expect_input",
                "description": (
                    "开启/关闭「记录模式」：把用户接下来要说的话记到某窗口。"
                    "『记下来/我说话你记到窗口』→ start（先 surface_manage open 记录窗口再 start）；"
                    "『别记了/停止记录』→ stop。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["start", "stop"], "description": "start 开启；stop 停止"},
                        "surface_id": {"type": "string", "description": "要记录到的窗口 id"},
                        "count": {"type": "integer", "minimum": 1, "maximum": 100, "description": "预计条数，默认1"},
                        "path": {"type": "string", "description": "记录路径，默认 /content/items"},
                    },
                    "required": ["action", "surface_id"],
                },
            },
        }
    return {
        "type": "function",
        "function": {
            "name": "surface_expect_input",
            "description": (
                "开启/关闭「记录模式」：告诉系统用户接下来要说的话要记到某个窗口。"
                "用户说『记下来/帮我记录接下来要说的话/我说话你记到窗口上』这类"
                "『持续记录』要求时用 start。注意 start 必须配着开窗一起做："
                "先 surface_manage open 一个记录窗口，再立刻调本工具 start 指向它；"
                "漏掉 start 记录模式不会开启。开启后每句话由你判断是否要记录："
                "内容用 surface_manage append 写入，操作/停止/提问不写。"
                "用户说『别记了/停止记录』或关闭目标窗口后用 stop 结束。"
                "注意：单次要立即写入某句话用 surface_manage append，不需要本工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["start", "stop"], "description": "start 启动持续记录；stop 停止。"},
                    "surface_id": {"type": "string", "description": "要记录到的窗口 id。"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 100, "description": "预计要记录的发言条数，默认 1。"},
                    "path": {"type": "string", "description": "记录路径，默认 /content/items。"},
                },
                "required": ["action", "surface_id"],
            },
        },
    }


# ==================== 注册表接入 ====================
def register(registry, *, wrapper=None):
    """把三个窗口动作注册进 action_registry。

    wrapper(fn, name) 可选：自定义执行包装（如注入 trace 上下文），返回替换后的 fn。
    """
    aliases = {
        "surface_manage": (
            "surface_update", "surface_set", "surface_patch", "surface_open",
            "surface_create", "surface_close", "surface_delete",
            "window_manage", "update_surface",
        ),
    }
    for name, conflicts in (
        ("surface_manage", "surface_id"),
        ("surface_inspect", None),
        ("surface_expect_input", "surface_id"),
    ):

        def fn(args, ctx, _name=name):
            ctx = ctx if isinstance(ctx, dict) else {}
            return execute(_name, args, aid=ctx.get("aid"))

        final_fn = wrapper(fn, name) if wrapper else fn
        registry.register(
            name, final_fn, conflicts=conflicts, aliases=aliases.get(name) or (),
        )


def execute(name, arguments, *, aid=None):
    if isinstance(arguments, dict) and arguments.get("__parse_error__"):
        detail = str(arguments.get("__parse_error__"))[:1000]
        return "（工具参数解析失败）%s" % detail, {
            "ok": False, "action": "", "reason": "parse_error", "detail": detail,
        }
    if name == "surface_manage":
        return surface_manage_execute(arguments)
    if name == "surface_inspect":
        return surface_inspect_execute(arguments)
    if name == "surface_expect_input":
        return surface_expect_input_execute(arguments, aid)
    return "surface_skill: unknown action %s." % name, {"ok": False, "action": name}


# ==================== 执行器 ====================
def surface_manage_execute(arguments):
    args = arguments if isinstance(arguments, dict) else {}
    print("[muse] surface_manage arguments: %s" % json.dumps(args, ensure_ascii=False)[:12000], flush=True)
    action = str(args.get("action") or "")
    explicit_id = str(args.get("surface_id") or "").strip()
    # open/create 是「造新窗」语义：用户没显式点名某个已有窗口时，绝不落到
    # current/聚焦窗口，否则「打开番茄钟」会被解析成「改当前 youtube 窗口」。
    if action in ("open", "create") and not explicit_id:
        requested_surface_id = ""
    else:
        requested_surface_id = str(explicit_id or "current").strip()
    geometry_error = reject_relative_geometry(args)
    if geometry_error:
        meta = {
            "ok": False,
            "action": action,
            "surface_id": requested_surface_id,
            "reason": "relative_geometry_requires_inspect",
            "detail": geometry_error,
        }
        return json.dumps(meta, ensure_ascii=False), meta
    surface_id = surface_resolve_id(requested_surface_id)
    ensured_during_open = False
    # Natural "open a window" describes the desired visible outcome. Requiring
    # the speaker to know the internal create/open split is an API leak.
    # open 永远兜底到可见窗口：无 current 或显式 id 不存在时都自动创建。
    if action == "open" and not surface_id:
        surface_id = "blank-board"
        definition = args.get("definition") if isinstance(args.get("definition"), dict) else {}
        for key_name in ("title", "window", "theme", "content"):
            if key_name in args:
                definition[key_name] = copy.deepcopy(args[key_name])
        data = normalize_web_surface_definition(definition, current={})
        # 服务端自动定位：不遮挡已打开的窗口，零额外 LLM 往返
        data = auto_place_window(data, surface_id=surface_id)
        data = attach_fit_beacon(data, surface_id=surface_id)
        scene_store.upsert(
            surface_id, kind="web-surface", data=data, intent="inform",
            focus=True, visible=True,
        )
        ensured_during_open = True
    if action == "create" and not surface_id:
        surface_id = "surface-" + uuid.uuid4().hex[:8]
    if not surface_id:
        return "surface_manage failed: no current or specified surface.", {"ok": False, "action": action}
    current_surface = scene_store.get(surface_id)

    current_data = current_surface.get("data") if isinstance(current_surface, dict) and isinstance(current_surface.get("data"), dict) else {}
    current_content = current_data.get("content") if isinstance(current_data.get("content"), dict) else {}
    current_source = current_content.get("source") if isinstance(current_content.get("source"), dict) else {}
    protected_owner = str(current_source.get("type") or "") in {"claude-code", "project-plan", "project-result"}
    if protected_owner:
        definition_arg = args.get("definition") if isinstance(args.get("definition"), dict) else {}
        changes_owned_content = (
            action in ("append", "create")
            or (action == "set" and ("content" in args or "content" in definition_arg))
            or (action == "patch" and any(
                str(item.get("path") or "").startswith(("/content", "/source_state"))
                for item in (args.get("patches") or []) if isinstance(item, dict)
            ))
        )
        if changes_owned_content:
            meta = {
                "ok": False,
                "action": action,
                "surface_id": surface_id,
                "reason": "surface_content_owned",
                "owner": current_source.get("type"),
            }
            return json.dumps(meta, ensure_ascii=False), meta

    if action == "create":
        definition = args.get("definition") if isinstance(args.get("definition"), dict) else {}
        for key_name in ("title", "window", "theme", "content"):
            if key_name in args:
                definition[key_name] = copy.deepcopy(args[key_name])
        data = normalize_web_surface_definition(definition, current={})
        # 服务端自动定位：即使 create 未显示，先占一个不遮挡的位置
        data = auto_place_window(data, surface_id=surface_id)
        data = attach_fit_beacon(data, surface_id=surface_id)
        result = scene_store.upsert(
            surface_id, kind="web-surface", data=data, intent="inform", focus=False, visible=False,
        )
    elif action == "open":
        if not current_surface:
            if ensured_during_open:
                result = {"rev": scene_store.rev, "surface": scene_store.get(surface_id), "changed": True}
            else:
                # 用户意图打开这个 id 但它不存在：自动创建并显示
                definition = args.get("definition") if isinstance(args.get("definition"), dict) else {}
                for key_name in ("title", "window", "theme", "content"):
                    if key_name in args:
                        definition[key_name] = copy.deepcopy(args[key_name])
                has_definition = (
                    any(key in definition for key in ("title", "window", "theme", "content"))
                    or "definition" in args
                )
                if not has_definition:
                    meta = {
                        "ok": False,
                        "action": action,
                        "surface_id": surface_id,
                        "reason": "open_requires_definition",
                        "detail": (
                            "要打开一个不存在的窗口，必须同时提供它的内容："
                            "打开网站用 definition.content.url（如 https://www.deepseek.com），"
                            "显示文字/UI 用 definition.content.html。仅凭窗口名（surface_id）"
                            "系统不知道要显示什么。"
                        ),
                    }
                    return json.dumps(meta, ensure_ascii=False), meta
                data = normalize_web_surface_definition(definition, current={})
                # 服务端自动定位：不遮挡已打开的窗口
                data = auto_place_window(data, surface_id=surface_id)
                data = attach_fit_beacon(data, surface_id=surface_id)
                scene_store.upsert(
                    surface_id, kind="web-surface", data=data, intent="inform",
                    focus=args.get("focus") is not False, visible=True,
                )
                ensured_during_open = True
                result = {"rev": scene_store.rev, "surface": scene_store.get(surface_id), "changed": True}
        else:
            # 已存在：open 如果带了新 definition（改内容/标题/几何），先应用再显示
            open_definition = (
                args.get("definition") if isinstance(args.get("definition"), dict) else {}
            )
            for key_name in ("title", "window", "theme", "content"):
                if key_name in args:
                    open_definition[key_name] = copy.deepcopy(args[key_name])
            has_open_definition = bool(open_definition)
            if has_open_definition:
                current = current_surface.get("data") if isinstance(current_surface.get("data"), dict) else {}
                data = normalize_web_surface_definition(open_definition, current=current)
                scene_store.upsert(
                    surface_id, kind=current_surface.get("kind") or "web-surface",
                    data=data, intent=current_surface.get("intent") or "inform",
                    focus=args.get("focus") is not False, visible=True,
                )
                result = {"rev": scene_store.rev, "surface": scene_store.get(surface_id), "changed": True}
            else:
                result = scene_store.request_show(
                    surface_id, focus=args.get("focus") is not False,
                )
    elif action == "close":
        # 常驻窗允许「关闭=隐藏」（与状态栏一致），只是不允许 delete 销毁。
        # 用户明确说「把信息推送关掉」时必须真的关掉：以前这里一律拒绝，
        # 上层又把拒绝当良性无操作，结果窗口没关却回「关了」——假回执。
        # 下次有新的搜索推送时它会自动重新显示，符合「推送窗」的语义。
        if not current_surface:
            return "surface_manage failed: surface does not exist.", {"ok": False, "action": action, "surface_id": surface_id}
        result = scene_store.set_visible(surface_id, False)
        # 窗口关了，指向它的持续记录一并停止，否则后续发言仍被吞进已关闭窗口
        _clear_pending_for_surface(surface_id)
    elif action == "delete":
        if is_pinned_surface(surface_id):
            meta = {
                "ok": False,
                "action": action,
                "surface_id": surface_id,
                "reason": "pinned_surface",
                "detail": (
                    "%s 是常驻窗口，不能被删除。它用于持续展示推送信息，内容可被"
                    "搜索/查询结果覆盖更新；要专项展示可新建窗口，不要删除常驻窗口。" % PINNED_INFO_TITLE
                ),
            }
            return json.dumps(meta, ensure_ascii=False), meta
        if not current_surface:
            meta = {
                "ok": False,
                "action": action,
                "surface_id": surface_id,
                "deleted": False,
                "reason": "surface_not_found",
                "detail": "目标页面不存在，没有执行删除。",
            }
            return json.dumps(meta, ensure_ascii=False), meta
        result = scene_store.remove(surface_id)
        _clear_pending_for_surface(surface_id)
        meta = {
            "ok": bool(result.get("changed")),
            "action": action,
            "surface_id": surface_id,
            "deleted": bool(result.get("changed")),
            "rev": result.get("rev"),
        }
        return json.dumps(meta, ensure_ascii=False), meta
    elif action in ("set", "patch", "append"):
        if not current_surface:
            return "surface_manage failed: create the surface first.", {"ok": False, "action": action, "surface_id": surface_id}
        current = current_surface.get("data") if isinstance(current_surface.get("data"), dict) else {}
        if action == "set":
            definition = args.get("definition") if isinstance(args.get("definition"), dict) else {}
            for key_name in ("title", "window", "theme", "content"):
                if key_name in args:
                    definition[key_name] = copy.deepcopy(args[key_name])
            data = normalize_web_surface_definition(definition, current=current)
        elif action == "patch":
            data = json_patch_apply(current, args.get("patches") or [])
            if isinstance(args.get("definition"), dict):
                data = deep_merge_dict(data, args["definition"])
            # JSON Patch already produced the full next document. Re-merging
            # the old document would resurrect fields that a replace/remove
            # operation intentionally removed.
            data = normalize_web_surface_definition(data, current={})
        else:
            data = copy.deepcopy(current)
            path = str(args.get("path") or "/content/items")
            has_patches = args.get("patches") is not None
            has_text = args.get("text") is not None
            has_items = bool(args.get("items"))
            if not (has_patches or has_text or has_items):
                return (
                    "surface_manage append failed: no content to append. "
                    "请提供 text=、items=[...] 或 patches=[{op:'add',path:'/content/items/-',value:...}]。",
                    {"ok": False, "action": "append", "surface_id": surface_id, "reason": "append_missing_content"},
                )
            if has_patches:
                # append 也接受 patches。模型常把追加项写成 add 到 items 数组
                # （可能带 /- 也可能不带）。在 append 语境下 add 到数组=追加条目，
                # 不是 RFC 6902 的整组替换。
                data = _append_apply_patches(data, args["patches"])
            items = copy.deepcopy(args.get("items") or [])
            if args.get("text") is not None:
                items.append(str(args.get("text")))
            if items:
                parts = json_pointer_parts(path)
                target = data
                for part in parts:
                    if not isinstance(target, dict):
                        raise ValueError("append path must address an array")
                    if part not in target:
                        target[part] = [] if part == parts[-1] else {}
                    target = target[part]
                if not isinstance(target, list):
                    raise ValueError("append path must address an array")
                target.extend(items)
            data = normalize_web_surface_definition(data, current=current)
        result = scene_store.upsert(
            surface_id,
            kind=current_surface.get("kind") or "web-surface",
            data=data,
            intent=current_surface.get("intent") or "inform",
            focus=bool(current_surface.get("focus")),
            order=current_surface.get("order"),
            visible=bool(current_surface.get("visible")),
        )
    else:
        return "surface_manage failed: unknown action.", {"ok": False, "action": action}

    rev = int(result.get("rev") or 0)
    ready = scene_store.wait_surface_ready(surface_id, min_rev=rev, timeout=1.2)
    applied_content = ((scene_store.get(surface_id) or {}).get("data") or {}).get("content") or {}
    visible_after = bool((scene_store.get(surface_id) or {}).get("visible"))
    content_needs_receipt = visible_after and str(applied_content.get("type") or "") in {
        "html", "app", "chart", "image", "url", "stream", "terminal", "text",
    }
    if content_needs_receipt:
        scene_store.wait_content_result(surface_id, timeout=1.2)
    inspection = scene_store.inspect(scope="id", surface_id=surface_id)
    runtime = (inspection.get("surfaces") or [{}])[0]
    applied_surface = scene_store.get(surface_id) or {}
    expected_visible = bool(applied_surface.get("visible"))
    needs_shell_receipt = action in ("open", "close") or (
        action in ("set", "patch", "append") and expected_visible
    )
    visibility_matches = (
        bool(runtime.get("visible")) == expected_visible if needs_shell_receipt else True
    )
    content_status = runtime.get("content_status") or "unknown"
    # url 窗口是「跳转加载」语义：壳已创建窗口并开始加载 url 即为打开成功，
    # content_status=loading 是正常中间态（页面加载可能慢），不应判失败。
    # html/app/chart/image/stream/terminal/text 才是渲染型，必须等 ready。
    _content_type = str(applied_content.get("type") or "")
    if content_needs_receipt and _content_type == "url":
        content_matches = content_status != "error"
    else:
        content_matches = (content_status == "ready") if content_needs_receipt else True
    changed = bool(result.get("changed", True)) if action in ("set", "patch", "append") else True
    # 空操作检测：set/patch/append 提交的内容与当前完全一致时，upsert 判定
    # changed=False，不会向壳推送、rev 不递增，等于什么都没做。绝不能谎报成功，
    # 否则模型会以为"强制重推了"而反复重发相同内容。
    no_op_reason = ""
    if action in ("set", "patch", "append") and not changed:
        no_op_reason = (
            "提交的内容与窗口当前内容完全一致，判定为无变化（changed=false），"
            "不会重新推送或重新渲染。若要强制刷新，请先修改内容再提交；"
            "若确实需要重载，用 surface_manage 的 set 提交包含实际差异的内容。"
        )
    meta = {
        "ok": (
            bool(((not needs_shell_receipt) or (ready and visibility_matches)) and content_matches)
            and changed
        ),
        "accepted": True,
        "action": action,
        "surface_id": surface_id,
        "rev": rev,
        "changed": changed,
        "rendered": ready,
        "created": bool(action == "create" or ensured_during_open),
        "visible": bool(runtime.get("visible")),
        "focused": bool(runtime.get("focused")),
        "bounds": runtime.get("bounds"),
        "content_status": content_status,
        "content_error": runtime.get("content_error") or "",
        "title": str((applied_surface.get("data") or {}).get("title") or ""),
        "content_type": str(applied_content.get("type") or ""),
    }
    # 确定性变更动作的即时播报语：让用户立刻听到结果，不等二轮 LLM。
    # 只对成功且可见的动作播；只读/失败动作不播（回执回填给模型处理）。
    if meta["ok"] and action in ("create", "open", "close", "delete", "set", "patch", "append"):
        title = meta["title"] or surface_id
        if action == "close":
            meta["speech"] = (
                "状态栏已隐藏"
                if surface_id == STATUS_TIMELINE_SURFACE
                else "窗口已关闭"
            )
        elif action == "delete":
            meta["speech"] = "窗口已删除"
        elif action == "open":
            meta["speech"] = (
                "状态栏已显示"
                if surface_id == STATUS_TIMELINE_SURFACE
                else "%s已打开" % title
            )
        elif action == "create":
            meta["speech"] = "%s已创建" % title
        elif action in ("set", "patch", "append"):
            meta["speech"] = "%s已更新" % title
    if no_op_reason:
        meta["reason"] = "no_content_change"
        meta["detail"] = no_op_reason
    return json.dumps(meta, ensure_ascii=False), meta


def surface_inspect_execute(arguments):
    args = arguments if isinstance(arguments, dict) else {}
    scope = str(args.get("scope") or "all")
    if scope not in ("all", "visible", "focused", "id"):
        scope = "all"
    surface_id = surface_resolve_id(args.get("surface_id")) if scope == "id" else str(args.get("surface_id") or "")
    result = scene_store.inspect(scope=scope, surface_id=surface_id)
    return json.dumps(result, ensure_ascii=False), {"ok": True, "action": "inspect", **result}


def surface_expect_input_execute(arguments, aid):
    args = arguments if isinstance(arguments, dict) else {}
    action = str(args.get("action") or "")
    with _PENDING_LOCK:
        if action == "stop":
            _PENDING_INPUT.pop(aid, None)
            meta = {"ok": True, "action": "stop"}
            return json.dumps(meta, ensure_ascii=False), meta
        surface_id = surface_resolve_id(args.get("surface_id"))
        if not surface_id or not scene_store.get(surface_id):
            return "surface_expect_input failed: target surface does not exist.", {"ok": False, "action": action}
        count = max(1, min(100, int(args.get("count") or 1)))
        _PENDING_INPUT[aid] = {
            "surface_id": surface_id,
            "remaining": count,
            "path": str(args.get("path") or "/content/items"),
        }
    meta = {
        "ok": True, "action": "start", "surface_id": surface_id, "remaining": count,
    }
    return json.dumps(meta, ensure_ascii=False), meta
