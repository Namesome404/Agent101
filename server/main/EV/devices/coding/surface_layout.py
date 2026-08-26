# -*- coding: utf-8 -*-
"""窗口几何与内容规范化：JSON patch、AABB 布局、定义归一化。

从 surfaces.py 拆出的纯工具层，与 scene_store 共同支撑 surface_tools。
本模块内部按依赖顺序排列：先是无依赖纯函数，再是依赖 scene_store 的定位辅助。
"""
from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlparse

from devices.coding.scene_store import scene_store


# ==================== 纯工具：颜色 / 合并 / JSON patch ====================
def safe_surface_color(value: str, fallback: str) -> str:
    value = str(value or "").strip()[:160]
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value):
        return value
    if re.fullmatch(r"(?:rgb|rgba|hsl|hsla)\([0-9.,%\s]+\)", value):
        return value
    if re.fullmatch(r"linear-gradient\([#0-9a-zA-Z(),.%\s-]+\)", value):
        return value
    return fallback


def deep_merge_dict(base, patch):
    result = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key_name, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key_name), dict):
            result[key_name] = deep_merge_dict(result[key_name], value)
        else:
            result[key_name] = copy.deepcopy(value)
    return result


def json_pointer_parts(path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("patch path must be a JSON pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def json_patch_apply(document, operations):
    result = copy.deepcopy(document)
    for operation in operations or []:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op") or "")
        parts = json_pointer_parts(operation.get("path") or "")
        if parts and parts[0] not in {"title", "window", "theme", "content"}:
            raise ValueError(
                "patch path must target /title, /window, /theme, or /content; got /%s"
                % parts[0]
            )
        if not parts:
            if op in ("add", "replace"):
                result = copy.deepcopy(operation.get("value"))
                continue
            raise ValueError("cannot remove the surface root")
        target = result
        leaf = parts[-1]
        for part in parts[:-1]:
            if isinstance(target, list):
                target = target[int(part)]
            else:
                if part not in target or not isinstance(target[part], (dict, list)):
                    # 追加到数组的容器（path=/content/items/- 的 items）必须建 list
                    target[part] = [] if (leaf == "-" and part == parts[-2]) else {}
                target = target[part]
        if isinstance(target, list):
            if op == "add" and leaf == "-":
                target.append(copy.deepcopy(operation.get("value")))
            elif op == "remove":
                target.pop(int(leaf))
            else:
                target[int(leaf)] = copy.deepcopy(operation.get("value"))
        elif op == "remove":
            if leaf not in target:
                raise ValueError("patch remove target missing: /%s" % "/".join(parts))
            target.pop(leaf, None)
        elif op == "replace":
            if leaf not in target:
                raise ValueError(
                    "patch replace target missing: /%s（窗口当前没有该字段，"
                    "不能用 replace 凭空添加；要新增用 add，或改用 set 提交完整内容）"
                    % "/".join(parts)
                )
            target[leaf] = copy.deepcopy(operation.get("value"))
        elif op == "add":
            target[leaf] = copy.deepcopy(operation.get("value"))
        else:
            raise ValueError("unsupported patch operation")
    return result


def _append_apply_patches(document, operations):
    """append 动作的 patches 解释：add 到数组路径视为追加条目。

    RFC 6902 里 add /content/items 是替换整个数组，但 append 语境下模型
    意图是「再加一条」，所以：
    - add 到数组（路径不带 /-）：value 若是列表则逐条追加，否则追加单条；
    - add 到数组末尾（路径带 /-）：照常追加；
    - 其余操作回退到 json_patch_apply 严格语义。
    """
    result = copy.deepcopy(document)
    for operation in operations or []:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op") or "")
        path = str(operation.get("path") or "")
        parts = json_pointer_parts(path)
        if op != "add" or not parts:
            result = json_patch_apply(result, [operation])
            continue
        leaf = parts[-1]
        if leaf == "-":
            result = json_patch_apply(result, [operation])
            continue
        target = result
        for part in parts[:-1]:
            if isinstance(target, list):
                target = target[int(part)]
            else:
                if part not in target or not isinstance(target[part], (dict, list)):
                    target[part] = []
                target = target[part]
        if not isinstance(target, dict) or not isinstance(target.get(leaf), list):
            # 字段不存在或不是数组：append 语境下 add 意图是「加一条」，
            # 应创建数组；若字段已存在且非数组则回退严格 patch 语义。
            if isinstance(target, dict) and leaf not in target:
                value = operation.get("value")
                target[leaf] = [copy.deepcopy(value)] if not isinstance(value, list) else copy.deepcopy(value)
                continue
            result = json_patch_apply(result, [operation])
            continue
        value = operation.get("value")
        if isinstance(value, list):
            target[leaf].extend(copy.deepcopy(value))
        else:
            target[leaf].append(copy.deepcopy(value))
    return result


# ==================== 内容 / 定义规范化 ====================
# 窗口装饰高度：标题栏 46px（--ev-bar-h）。内容区就是窗口高度减掉它。
_SURFACE_BAR_H = 46


def _wrapped_lines(text, columns):
    """按可视宽度估算一段文字要占几行（中日韩字符按两格算）。"""
    total = 0
    for raw_line in str(text or "").split("\n"):
        width = 0
        for char in raw_line:
            width += 2 if ord(char) > 0x2E80 else 1
        total += max(1, -(-width // max(8, columns)))
    return total


def estimate_content_height(data, width):
    """估算内容自然需要多高；无法判断（网页/自定义 HTML）时返回 None。

    窗口尺寸本该由内容决定，但服务端组装内容时把这件事整个跳过了：
    一行备忘和一长串清单拿到同样的 380px。真正精确的测量在窗口里
    （surface.js 上报 content.size），这里给的是「开窗那一刻」的合理高度，
    否则用户先看到一个明显不对的窗口，再等它跳一下。
    """
    if not isinstance(data, dict):
        return None
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    app = data.get("app") if isinstance(data.get("app"), dict) else {}
    # 网页、自定义 HTML、内置小程序的高度这里量不出来，交给窗口内的实测
    if app or content.get("app_id") or str(content.get("type") or "") in ("url", "html", "app", "chart"):
        return None

    blocks = data.get("blocks")
    if isinstance(blocks, list) and blocks:
        columns = max(10, (int(width) - 60) // 8)
        height = 56  # .surface-blocks 上下内边距
        if data.get("title"):
            height += 54
        for block in blocks[:60]:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or "text")
            if kind == "heading":
                height += 46
            elif kind == "list":
                items = block.get("items") if isinstance(block.get("items"), list) else []
                height += 16 + sum(24 * _wrapped_lines(item, columns) for item in items[:40])
            else:
                height += 12 + 24 * _wrapped_lines(block.get("text"), columns)
        return height

    lines = []
    if content.get("text"):
        lines.append(str(content.get("text")))
    if isinstance(content.get("items"), list):
        lines.extend(str(item) for item in content["items"][:80])
    if not lines:
        return None
    # .surface-text：13px/1.65 等宽字体，左右内边距 26
    columns = max(10, (int(width) - 52) // 8)
    rows = sum(_wrapped_lines(line, columns) for line in lines)
    return 48 + int(round(21.5 * rows))

def normalize_web_surface_definition(definition, *, current=None):
    raw = definition if isinstance(definition, dict) else {}
    current = current if isinstance(current, dict) else {}
    merged = deep_merge_dict(current, raw)

    def bounded(value, fallback, low, high):
        try:
            return max(low, min(high, int(value)))
        except Exception:
            return fallback

    current_window = current.get("window") if isinstance(current.get("window"), dict) else {}
    window = merged.get("window") if isinstance(merged.get("window"), dict) else {}
    minimum_height = 76 if window.get("compact") is True else 220
    # 尺寸的「出处」必须留下来：这里无条件补 520x380 之后，窗口层就再也分不清
    # 「模型指定了高度」和「没人指定、只是默认值」——于是一行字和一长串清单
    # 拿到同样大的窗口。没人指定就标 fit=content，由内容量出来的高度说了算。
    raw_window = raw.get("window") if isinstance(raw.get("window"), dict) else {}
    if str(raw_window.get("fit") or "") in ("content", "fixed"):
        fit = str(raw_window["fit"])          # 显式声明最大：实测回填就走这条
    elif isinstance(raw_window.get("height"), (int, float)):
        fit = "fixed"                          # 有人指定了高度，别再自作主张
    else:
        fit = str(current_window.get("fit") or "") or "content"
    resolved_width = bounded(window.get("width"), int(current_window.get("width") or 520), 320, 2000)
    resolved_height = bounded(
        window.get("height"), int(current_window.get("height") or 380), minimum_height, 1400,
    )
    if fit == "content":
        measured = estimate_content_height(merged, resolved_width)
        if measured:
            resolved_height = max(minimum_height, min(1400, _SURFACE_BAR_H + int(measured)))
    merged["window"] = {
        **window,
        "width": resolved_width,
        "height": resolved_height,
        "fit": fit,
    }
    merged["title"] = str(merged.get("title") or "窗口")[:160]
    theme = merged.get("theme") if isinstance(merged.get("theme"), dict) else {}
    merged["theme"] = {
        **theme,
        "background": safe_surface_color(theme.get("background"), "#111318"),
        "foreground": safe_surface_color(theme.get("foreground"), "#f4f5f2"),
        "accent": safe_surface_color(theme.get("accent"), "#8fefbd"),
    }
    content = merged.get("content") if isinstance(merged.get("content"), dict) else {}
    raw_content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    has_code = ("html" in content) or ("js" in content) or ("css" in content)
    # 本次是否携带了新的内容字段：带了就按新内容重新推断类型，不沿用旧窗口的 type。
    # 否则 open 已有 url 窗口再给 html 时，deep_merge 保留的 type=url 会把 html 过滤掉。
    new_fields = {k for k in ("html", "css", "js", "url", "text") if k in raw_content}
    if new_fields:
        if new_fields & {"html", "css", "js"}:
            content_type = "html"
        elif "url" in new_fields:
            content_type = "url"
        else:
            content_type = "text"
    elif "type" not in content:
        # 窗口即代码：模型不再声明 content.type，按内容自动推断。
        if "url" in content and not has_code:
            content_type = "url"
        elif "text" in content and not has_code:
            content_type = "text"
        else:
            content_type = "html"
    else:
        content_type = str(content.get("type") or "html").lower()
    if content_type not in ("text", "html", "app", "chart", "image", "url", "stream", "terminal"):
        content_type = "html"
    content["type"] = content_type
    if "html" in content:
        raw_html = str(content.get("html") or "")[:120000]
        # 只拦外部资源加载与内联事件：script 是「窗口即代码」的核心（前端在
        # sandbox iframe 里执行，allow-scripts 但不含 allow-same-origin，无法
        # 触达 __TAURI__ 命令面，剥 script 只会把用户要的交互逻辑删光）。
        raw_html = re.sub(r"<\s*(iframe|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>", "", raw_html, flags=re.I | re.S)
        raw_html = re.sub(r"<\s*(iframe|object|embed|base)\b[^>]*/?\s*>", "", raw_html, flags=re.I)
        raw_html = re.sub(r"\s+on[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", raw_html, flags=re.I)
        content["html"] = raw_html
    if "css" in content:
        content["css"] = str(content.get("css") or "")[:80000]
    if "js" in content:
        content["js"] = str(content.get("js") or "")[:80000]
    if content_type in ("url", "image"):
        parsed = urlparse(str(content.get("url") or ""))
        content["url"] = str(content.get("url") or "") if parsed.scheme in ("http", "https") and parsed.netloc else ""
    if not isinstance(content.get("items"), list):
        content["items"] = []
    content_allowed = {
        "url": {"type", "url", "source"},
        "image": {"type", "url", "alt", "source"},
        "text": {"type", "text", "items", "source"},
        "stream": {"type", "text", "items", "source"},
        "terminal": {"type", "text", "items", "source"},
        "html": {"type", "html", "css", "js", "items", "source"},
        "app": {"type", "html", "css", "js", "items", "source"},
        "chart": {"type", "html", "css", "js", "spec", "items", "source"},
    }
    content = {
        key: value for key, value in content.items()
        if key in content_allowed.get(content_type, {"type", "text", "items", "source"})
    }
    merged["content"] = content
    # Keep the Scene definition canonical. Unknown top-level keys cannot affect
    # the renderer and previously allowed semantic pseudo-patches to look
    # successful while changing nothing on screen.
    allowed_keys = {
        "title", "window", "theme", "content", "source_state",
        "blocks", "document", "capture", "show_rev",
    }
    merged = {key: value for key, value in merged.items() if key in allowed_keys}
    json.dumps(merged, ensure_ascii=False)
    return merged


# ==================== 布局定位（依赖 scene_store） ====================
def _aabb_overlap(x, y, w, h, rects):
    """AABB 矩形重叠检测：新窗口 (x,y,w,h) 与任一现有窗口相交即 True。"""
    for (bx, by, bw, bh) in rects:
        if x < bx + bw and x + w > bx and y < by + bh and y + h > by:
            return True
    return False


def find_free_position(existing, width, height, viewport=(1920, 1040), gap=24):
    """极快的窗口不遮挡定位算法。

    在屏幕（工作区 viewport）内找一个不遮挡任何现有窗口的位置。延迟关键：
    这里是纯本地几何计算（微秒级），由服务端在 open/create 时自动完成，
    LLM 不需要先调用任何"算法工具"——零额外 LLM 往返、零额外延迟。

    算法：候选 y 取「0 与每个窗口底边+gap」，候选 x 取「0 与每个窗口右边+gap」，
    逐行逐列尝试第一个不与任何窗口重叠的位置。候选数 ≈ O(K^2)，K 通常 ≤6，
    每次 AABB 检测 O(1)，总比较 ≤ 几百次，实测 <1ms。满屏时回退右上角。
    """
    vw, vh = viewport
    width = min(int(width) or 520, vw)
    height = min(int(height) or 380, vh)
    existing = [(int(bx), int(by), int(bw), int(bh)) for (bx, by, bw, bh) in existing]
    cand_y = sorted(set([0] + [by + bh + gap for (bx, by, bw, bh) in existing]))
    for cy in cand_y:
        if cy + height > vh:
            break
        cand_x = sorted(set([0] + [bx + bw + gap for (bx, by, bw, bh) in existing]))
        for cx in cand_x:
            if cx + width > vw:
                continue
            if not _aabb_overlap(cx, cy, width, height, existing):
                return (cx, cy)
    # 满屏兜底：右上角
    return (max(0, vw - width - gap), gap)


def _visible_bounds(exclude_surface_id=""):
    """当前可见窗口的 (x,y,w,h) 列表，供不遮挡定位使用。

    优先用运行时回报的 bounds（壳的真实位置）；没有回报时退回 data.window
    里的几何。不可见窗口不占位。
    """
    bounds = []
    try:
        snapshot = scene_store.inspect(scope="visible")
    except Exception:
        return bounds
    for item in snapshot.get("surfaces") or []:
        if exclude_surface_id and str(item.get("id") or "") == exclude_surface_id:
            continue
        b = item.get("bounds")
        if isinstance(b, dict) and all(isinstance(b.get(k), (int, float)) for k in ("x", "y", "width", "height")):
            bounds.append((int(b["x"]), int(b["y"]), int(b["width"]), int(b["height"])))
            continue
        w = (item.get("data") or {}).get("window") or {}
        if isinstance(w, dict) and all(isinstance(w.get(k), (int, float)) for k in ("width", "height")):
            bounds.append((
                int(w.get("x") or 0),
                int(w.get("y") or 0),
                int(w["width"]),
                int(w["height"]),
            ))
    return bounds


# 窗口内容跑在无 allow-same-origin 的沙箱 iframe 里，外壳读不到它的
# contentDocument，也就量不出自然高度——所以让内容自己报。
# 走 HTTP 而不是 postMessage：外壳是编译进二进制的资源，改了要重编；
# 这段注入的脚本随内容下发，当场生效。CSP 已放行 connect-src 到 :8002。
_FIT_BEACON_MARK = "/*ev-fit-beacon*/"
_FIT_BEACON = """<script>%s(function(){
var last=0,posts=0;
function measure(){var b=document.body;if(!b)return 0;var bottom=0,kids=b.children;
for(var i=0;i<kids.length;i++){var el=kids[i];var tag=el.tagName;
if(tag==='SCRIPT'||tag==='STYLE'||tag==='LINK')continue;
var r=el.getBoundingClientRect();bottom=Math.max(bottom,r.bottom+(window.scrollY||0))}
if(!bottom)return 0;var cs=getComputedStyle(b);
return Math.ceil(bottom+(parseFloat(cs.marginBottom)||0)+(parseFloat(cs.paddingBottom)||0))}
function send(){if(posts>24)return;var h=measure();if(!h||Math.abs(h-last)<5)return;last=h;posts++;
try{fetch('%s/api/scene/surfaces/%s/content_size',{method:'POST',mode:'no-cors',keepalive:true,
headers:{'Content-Type':'text/plain'},body:JSON.stringify({height:h})})}catch(e){}}
addEventListener('load',send);setTimeout(send,80);setTimeout(send,420);
try{new ResizeObserver(send).observe(document.body)}catch(e){}})()</script>"""


def apply_measured_window_size(surface_id, *, height, width=0, declared_fit="content",
                               add_chrome=False):
    """把实测出的所需尺寸落回窗口。

    以前实测值只被记进回执，没人据此改窗口，所以窗口高度和内容多少始终没关系。
    只处理 fit=content 的窗口：用户或模型指定过高度的一律不碰。
    add_chrome=True 时传入的是内容高度（信标量的），需要补上标题栏。
    """
    from devices.coding.scene_store import scene_store

    # 必须显式声明 fit=content 才认。旧版外壳报的是容器矩形（窗口高度减掉标题栏），
    # 放行空值等于让窗口拿自己的尺寸当内容尺寸，每报一次缩一截。
    if declared_fit != "content":
        return False
    height = int(height or 0)
    if height <= 0:
        return False
    surface = scene_store.get(surface_id) or {}
    data = surface.get("data") if isinstance(surface.get("data"), dict) else {}
    if not data:
        return False
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    if str(window.get("fit") or "content") != "content":
        return False
    if add_chrome:
        height += _SURFACE_BAR_H
    current_height = int(window.get("height") or 0)
    current_width = int(window.get("width") or 0)
    # 宽度只涨不缩：文字块天生填满容器，按它回缩会把窗口挤成一条
    target_width = max(current_width, min(2000, int(width or 0)))
    target_height = max(160, min(1400, height))
    if abs(target_height - current_height) < 8 and target_width == current_width:
        return False
    next_data = copy.deepcopy(data)
    next_data["window"] = {**window, "width": target_width, "height": target_height, "fit": "content"}
    try:
        scene_store.upsert(
            surface_id,
            kind=str(surface.get("kind") or "web"),
            data=next_data,
            intent="inform",
        )
    except Exception:
        return False
    return True


def attach_fit_beacon(data, surface_id="", *, origin="http://127.0.0.1:8002"):
    """给自适应窗口的 HTML 内容挂上尺寸信标；不适用时原样返回。"""
    if not isinstance(data, dict) or not surface_id:
        return data
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    if str(window.get("fit") or "content") != "content":
        return data
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    html = str(content.get("html") or "")
    if not html or _FIT_BEACON_MARK in html:
        return data
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", str(surface_id))[:64]
    if not safe_id:
        return data
    out = copy.deepcopy(data)
    out["content"] = {
        **content,
        "html": html + (_FIT_BEACON % (_FIT_BEACON_MARK, origin, safe_id)),
    }
    return out


def auto_place_window(data, surface_id=""):
    """为新建窗口填上不遮挡的 x/y（模型没显式给坐标时）。

    返回 data 的副本。已有显式 x/y 的窗口保持原样——用户/模型指定了位置就不改。
    """
    if not isinstance(data, dict):
        return data
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    if isinstance(window.get("x"), (int, float)) and isinstance(window.get("y"), (int, float)):
        return data
    width = int(window.get("width") or 520)
    height = int(window.get("height") or 380)
    x, y = find_free_position(
        _visible_bounds(exclude_surface_id=surface_id), width, height
    )
    out = copy.deepcopy(data)
    out["window"] = {**window, "x": x, "y": y}
    return out


def surface_resolve_id(value=""):
    """把 surface_id 参数解析成真实窗口 id：current 指当前聚焦/最近可见窗口。"""
    requested = str(value or "").strip()
    if not requested:
        return ""
    if requested != "current":
        return requested[:120]
    focused = scene_store.focused()
    if focused:
        return str(focused.get("id") or "")
    surfaces = scene_store.snapshot().get("surfaces") or []
    visible = [item for item in surfaces if item.get("visible") is True]
    candidate = (visible or surfaces)[-1] if (visible or surfaces) else None
    return str((candidate or {}).get("id") or "")


def reject_relative_geometry(args):
    """拒绝『更宽/大一点』等非数值几何：返回错误信息或 None。

    查询-计算-执行兜底：模型必须先 surface_inspect 查当前 bounds，再算出
    目标数值用 set/patch。几何字段出现非数字（含相对词）时不得静默 fallback。
    """
    candidates = []
    if isinstance(args.get("window"), dict):
        candidates.append(("window", args.get("window")))
    definition = args.get("definition") if isinstance(args.get("definition"), dict) else {}
    if isinstance(definition.get("window"), dict):
        candidates.append(("definition.window", definition.get("window")))
    patches = args.get("patches") if isinstance(args.get("patches"), list) else []
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        path = str(patch.get("path") or "")
        value = patch.get("value")
        if path.startswith("/window/") and "value" in patch:
            field = path.rsplit("/", 1)[-1]
            if field in ("width", "height", "x", "y"):
                candidates.append(("patch %s" % path, {field: value}))
    for label, window in candidates:
        for field in ("width", "height", "x", "y"):
            if field not in window:
                continue
            value = window[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return (
                    "%s.%s 是相对描述（%r），不是确定数值。"
                    "请先 surface_inspect 查当前 bounds，再算出目标值。"
                    % (label, field, value)
                )
    return None
