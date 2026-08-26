# -*- coding: utf-8 -*-
"""面板呈现契约：把任意工具/MCP 的返回值收敛成可渲染的规范结构。

设计目标是「新增能力不需要改 surface_control」：
- 谁产生内容，谁在返回值里附带 panel 说明怎么显示；
- 没附带的（第三方 MCP 不会按我们的约定写），这里按数据形状推断；
- kind 是数据不是 schema：渲染器按 kind 匹配，匹配不到走兜底，所以
  零适配的能力也能显示，写了专用渲染器只是更好看。

渲染层只接受本模块的输出，脏数据进不了前端。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

# 呈现形态。kind 说「这是什么」，layout 说「怎么摆」——两者正交：
# 同样是 search，问接线图要 figure，问概念要 text。mixed 是 AI 的默认画布，
# 可以在同一次推送里组合文字、图片、列表、表格、图表和 3D 模型。
LAYOUTS = ("mixed", "figure", "text", "list", "table", "chart", "model", "html")
BLOCK_TYPES = ("text", "list", "table", "images", "figure", "chart", "model", "html")
CANVAS_NODE_TYPES = ("text", "image", "source", "table", "chart", "model", "html")

_ITEM_KEYS = ("items", "results", "articles", "entries", "rows", "list", "messages")
_IMAGE_KEYS = ("images", "image", "img", "thumbnail", "photos", "figure")
_TEXT_KEYS = ("summary", "text", "content", "answer", "description", "body")
_MODEL_KEYS = ("model", "model3d", "model_url", "three_d", "asset")


def _s(value: Any, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _first(data: Dict[str, Any], keys) -> Any:
    for key in keys:
        if isinstance(data, dict) and data.get(key):
            return data[key]
    return None


def _norm_items(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in (raw or [])[:12]:
        if isinstance(row, str):
            title, url, snippet = row, "", ""
        elif isinstance(row, dict):
            title = _s(row.get("title") or row.get("name") or row.get("subject"), 140)
            url = _s(row.get("url") or row.get("link") or row.get("href"), 500)
            snippet = _s(
                row.get("snippet") or row.get("summary") or row.get("description")
                or row.get("preview") or row.get("text"), 240,
            )
        else:
            continue
        if not (title or snippet or url):
            continue
        out.append({
            "title": title or url,
            "url": url,
            "snippet": snippet,
            "image": _s(row.get("image") or row.get("thumbnail"), 700) if isinstance(row, dict) else "",
            "date": _s(row.get("date") or row.get("published_at"), 40) if isinstance(row, dict) else "",
        })
    return out


def _norm_images(raw: Any) -> List[Dict[str, str]]:
    rows = raw if isinstance(raw, list) else ([raw] if raw else [])
    out: List[Dict[str, str]] = []
    for row in rows[:8]:
        if isinstance(row, str):
            url, desc = row, ""
        elif isinstance(row, dict):
            url = _s(row.get("url") or row.get("src") or row.get("img_src"), 700)
            desc = _s(row.get("description") or row.get("title") or row.get("alt"), 140)
        else:
            continue
        if url.startswith(("http://", "https://", "data:image/")):
            out.append({"url": url, "description": desc})
    return out


def _norm_chart(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").lower()
    if kind not in ("bar", "line", "pie"):
        return None
    labels = [_s(x, 24) for x in (raw.get("labels") or [])][:12]
    values: List[float] = []
    for item in (raw.get("values") or [])[:12]:
        try:
            values.append(round(float(item), 4))
        except (TypeError, ValueError):
            values.append(0.0)
    if not labels or not values:
        return None
    size = min(len(labels), len(values))
    return {
        "type": kind,
        "title": _s(raw.get("title"), 60),
        "unit": _s(raw.get("unit"), 12),
        "labels": labels[:size],
        "values": values[:size],
    }


def _norm_table(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    column_keys = list(raw.get("columns") or [])[:12]
    rows_in = raw.get("rows") or []
    if not isinstance(rows_in, list):
        return None
    if not column_keys:
        for row in rows_in:
            if isinstance(row, dict):
                column_keys = list(row)[:12]
                break
    columns = [_s(column, 40) for column in column_keys]
    if not columns:
        return None
    rows: List[List[str]] = []
    for row in rows_in[:80]:
        if isinstance(row, dict):
            rows.append([_s(row.get(column), 240) for column in column_keys])
        elif isinstance(row, (list, tuple)):
            rows.append([_s(value, 240) for value in list(row)[:len(columns)]])
    if not rows:
        return None
    return {
        "title": _s(raw.get("title"), 80),
        "columns": columns,
        "rows": rows,
    }


def _safe_remote_url(value: Any, *, image: bool = False) -> str:
    url = _s(value, 1000)
    allowed = ("http://", "https://")
    if image:
        allowed = allowed + ("data:image/",)
    # 产品自带的离线模型/海报由同源静态目录提供。只放行固定前缀，避免把
    # 任意相对路径或 javascript: 一类协议带进渲染层。
    allowed = allowed + ("/static/",)
    return url if url.startswith(allowed) else ""


def _is_model_asset_url(value: Any) -> bool:
    url = str(value or "").strip()
    if url.startswith("/static/"):
        return url.split("?", 1)[0].split("#", 1)[0].lower().endswith((".glb", ".gltf"))
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.path.lower().endswith((".glb", ".gltf"))
    )


def _norm_model(raw: Any) -> Optional[Dict[str, str]]:
    if isinstance(raw, str):
        source: Dict[str, Any] = {"url": raw}
    elif isinstance(raw, dict):
        source = raw
    else:
        return None
    url = _safe_remote_url(
        source.get("url") or source.get("src") or source.get("model_url")
    )
    if not url or not _is_model_asset_url(url):
        return None
    return {
        "url": url,
        "poster": _safe_remote_url(source.get("poster") or source.get("thumbnail"), image=True),
        "title": _s(source.get("title") or source.get("name"), 100),
        "description": _s(source.get("description") or source.get("caption"), 240),
    }


def _model_from_items(items: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    for item in items:
        url = str(item.get("url") or "")
        if _is_model_asset_url(url):
            return _norm_model({
                "url": url,
                "title": item.get("title"),
                "description": item.get("snippet"),
                "poster": item.get("image"),
            })
    return None


def _norm_focus(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or raw.get("kind") or "").lower()
    if kind not in ("image", "item", "model"):
        return None
    try:
        index = max(0, int(raw.get("index") or 0))
    except (TypeError, ValueError):
        index = 0
    return {"type": kind, "index": index}


def _norm_blocks(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for block in raw[:16]:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "").lower()
        if kind not in BLOCK_TYPES:
            continue
        clean: Dict[str, Any] = {"type": kind}
        if kind == "text":
            clean["title"] = _s(block.get("title"), 100)
            clean["text"] = _s(_first(block, _TEXT_KEYS), 2400)
            if not clean["text"]:
                continue
        elif kind == "list":
            clean["title"] = _s(block.get("title"), 100)
            clean["items"] = _norm_items(_first(block, _ITEM_KEYS))
            if not clean["items"]:
                continue
        elif kind == "table":
            table = _norm_table(block)
            if not table:
                continue
            clean.update(table)
        elif kind in ("images", "figure"):
            clean["title"] = _s(block.get("title"), 100)
            clean["images"] = _norm_images(_first(block, _IMAGE_KEYS))
            if not clean["images"]:
                continue
        elif kind == "chart":
            chart = _norm_chart(block.get("chart") or block)
            if not chart:
                continue
            clean["chart"] = chart
        elif kind == "model":
            model = _norm_model(block.get("model") or block)
            if not model:
                continue
            clean["model"] = model
        elif kind == "html":
            clean["html"] = _s(block.get("html"), 20000)
            if not clean["html"]:
                continue
        out.append(clean)
    return out


def _infer_layout(items, images, chart, table, model, html, text, blocks) -> str:
    """没人指定 layout 时按数据形状推断。第三方 MCP 靠这条零改造可用。"""
    if blocks:
        return "mixed"
    populated = sum(bool(value) for value in (items, images, chart, table, model, html, text))
    if populated > 1:
        return "mixed"
    if model:
        return "model"
    if table:
        return "table"
    if chart:
        return "chart"
    if images and not items:
        return "figure"
    if html:
        return "html"
    if items:
        return "list"
    return "text"


def normalize(
    raw: Any, *, kind: str = "", layout: str = "", pending: bool = False,
) -> Optional[Dict[str, Any]]:
    """把任意返回值收敛成规范面板结构；实在没有可呈现内容则返回 None。

    raw 可以是：①带 panel 字段的工具回执 ②panel 本身 ③第三方 MCP 的裸结果。
    """
    if not isinstance(raw, dict):
        return None
    # 带 panel 字段就以它为准（我们自己的能力走这条）
    source = raw.get("panel") if isinstance(raw.get("panel"), dict) else raw

    items = _norm_items(_first(source, _ITEM_KEYS))
    images = _norm_images(_first(source, _IMAGE_KEYS))
    chart = _norm_chart(source.get("chart") or source.get("graph"))
    table_source = source.get("table")
    if not table_source and source.get("columns") and source.get("rows"):
        table_source = source
    table = _norm_table(table_source)
    model = _norm_model(_first(source, _MODEL_KEYS)) or _model_from_items(items)
    blocks = _norm_blocks(source.get("blocks"))
    focus = _norm_focus(source.get("focus"))
    html = _s(source.get("html"), 20000)
    url = _s(source.get("url") or source.get("link"), 700)
    text = _s(_first(source, _TEXT_KEYS), 1200)
    title = _s(source.get("title") or source.get("query") or source.get("name"), 140)

    if not (items or images or chart or table or model or blocks or html or text or url):
        return None

    want = str(layout or source.get("layout") or "").lower()
    if want not in LAYOUTS:
        want = _infer_layout(items, images, chart, table, model, html, text, blocks)

    return {
        "kind": _s(kind or source.get("kind") or "generic", 32) or "generic",
        "query": _s(source.get("query"), 240),
        "layout": want,
        "title": title,
        "summary": text,
        "items": items,
        "images": images,
        "chart": chart,
        "table": table,
        "model": model,
        "blocks": blocks,
        "focus": focus,
        "html": html,
        "url": url,
        "prefer": "window" if str(source.get("prefer") or "") == "window" else "panel",
        # 模型声明的「用户最终想看到什么」；服务端据此组装，不再靠关键词猜。
        "want": _s(source.get("want"), 16).lower(),
        # 快路径先出结果、后台补正文与配图；这段空窗期渲染层要给占位提示，
        # 否则用户看到的是「搜完了却没有图」。
        "pending": bool(pending or source.get("pending")),
    }


# ==================== Research canvas document ====================
#
# ``normalize`` above remains the boundary for arbitrary tool/MCP payloads.  The
# renderer no longer consumes its layout enum directly.  It consumes the canvas
# document below: addressable content nodes plus a recursive layout tree. Both
# AI and direct gestures use the same constrained state changes; the store may
# translate them internally, but callers never guess arbitrary patch paths.

_PRICE_QUERY_RE = re.compile(
    r"售价|价格|多少钱|报价|费用|价位|price|pricing|cost|how\s+much", re.I,
)
_VISUAL_QUERY_RE = re.compile(
    r"图片|照片|外观|长什么样|接线图|引脚图|原理图|示意图|结构图|"
    r"image|photo|diagram|schematic|pinout|wiring", re.I,
)
_MODEL_QUERY_RE = re.compile(r"3d|三维|模型|glb|gltf", re.I)
_SAFE_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _stable_tab_id(kind: str, query: str, title: str) -> str:
    identity = "%s\n%s\n%s" % (kind, query, title)
    digest = hashlib.sha1(identity.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return "research-%s" % digest


def _container(
    container_id: str,
    children: List[Any],
    *,
    axis: str = "column",
    columns: int = 1,
    gap: int = 16,
    weights: Optional[List[float]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "id": container_id,
        "type": "container",
        "axis": axis,
        "children": [child for child in children if child],
        "gap": gap,
    }
    if axis == "grid":
        result["columns"] = columns
    if weights:
        result["weights"] = weights
    return result


def _source_table(items: List[Dict[str, Any]], title: str = "结果对比") -> Optional[Dict[str, Any]]:
    if not items:
        return None
    return {
        "title": title,
        "columns": ["结果", "关键信息", "来源"],
        "rows": [
            [item.get("title") or "", item.get("snippet") or "", _source_host(item.get("url"))]
            for item in items[:8]
        ],
    }


def _source_host(url: Any) -> str:
    match = re.match(r"^https?://([^/?#]+)", str(url or ""), re.I)
    return (match.group(1) if match else "").removeprefix("www.")


def to_canvas_document(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn a normalized payload into an editable, addressable canvas.

    The composition is assembled from the actual modalities and the question,
    rather than selecting a renderer template.  Every visible object receives a
    stable ID.  The recursive layout is data and can be replaced or patched by
    the model after it inspects the current revision.
    """
    if not isinstance(payload, dict):
        return None
    clean = normalize(payload) if "kind" not in payload else payload
    if not isinstance(clean, dict):
        return None

    kind = _s(clean.get("kind") or "generic", 32) or "generic"
    # 呈现形态由模型声明的意图决定：要图就只给图，要清单就把项目列全。
    # 以前不论用户想要什么都摆成「摘要+2条来源」，等于把请求当模板套。
    want = _s(clean.get("want"), 16).lower()
    query = _s(clean.get("query") or clean.get("title"), 240)
    title = _s(clean.get("title") or query or "研究结果", 140) or "研究结果"
    nodes: Dict[str, Dict[str, Any]] = {}
    summary_id = ""
    if clean.get("summary") or kind == "search":
        summary_text = _s(clean.get("summary"), 1200)
        # 搜索刚返回时不要拿「N 条结果」冒充内容。等待回答模型把证据
        # 收敛成结论期间，只展示用户正在查的主题；渲染层会隐藏这段内部占位。
        if kind == "search" and clean.get("pending"):
            summary_text = query or title
        summary_id = "summary"
        nodes[summary_id] = {
            "id": summary_id,
            "type": "text",
            "role": "answer",
            "title": title,
            "text": summary_text,
        }

    table_id = ""
    table = clean.get("table")
    if not table and want == "compare":
        # 用户要的是「哪个更合适」，那就把候选并排铺开自己比，
        # 而不是给一段散文再附几条链接让他自己去对。
        table = _source_table(clean.get("items") or [], title=query or "对比")
    if table:
        table_id = "table-1"
        nodes[table_id] = {"id": table_id, "type": "table", **table}

    chart_id = ""
    if clean.get("chart"):
        chart_id = "chart-1"
        nodes[chart_id] = {"id": chart_id, "type": "chart", "chart": clean["chart"]}

    model_id = ""
    if clean.get("model"):
        model_id = "model-1"
        nodes[model_id] = {"id": model_id, "type": "model", **clean["model"]}

    image_ids: List[str] = []
    # 用户明确要看图时给一组，而不是配图式的两张
    image_limit = 6 if want == "images" else 2
    for index, image in enumerate((clean.get("images") or [])[:image_limit], 1):
        node_id = "image-%d" % index
        nodes[node_id] = {
            "id": node_id,
            "type": "image",
            "url": image.get("url") or "",
            "caption": _s(image.get("description"), 80),
        }
        image_ids.append(node_id)

    source_ids: List[str] = []
    if want == "images":
        source_limit = 0          # 要看图就只给图，不拿链接充数
    elif want == "answer":
        source_limit = 1          # 一句事实答案，留一条出处即可
    elif want in ("list", "compare"):
        source_limit = 5          # 「找几个项目」就得真的列出几个
    else:
        source_limit = 2 if kind == "search" else 3
    for index, item in enumerate((clean.get("items") or [])[:source_limit], 1):
        node_id = "source-%d" % index
        nodes[node_id] = {
            "id": node_id,
            "type": "source",
            "title": item.get("title") or item.get("url") or "",
            "url": item.get("url") or "",
            "snippet": item.get("snippet") or "",
            "date": item.get("date") or "",
            "image": item.get("image") or "",
        }
        source_ids.append(node_id)

    # Explicit blocks are content, not page templates.  Convert each block to a
    # node and let the same recursive layout primitive place it.
    block_ids: List[str] = []
    for index, block in enumerate(clean.get("blocks") or [], 1):
        node_id = "block-%d" % index
        block_type = block.get("type")
        if block_type == "text":
            nodes[node_id] = {
                "id": node_id, "type": "text", "title": block.get("title") or "",
                "text": block.get("text") or "",
            }
        elif block_type == "table":
            nodes[node_id] = {"id": node_id, "type": "table", **block}
            nodes[node_id].pop("type", None)
            nodes[node_id]["type"] = "table"
        elif block_type == "chart":
            nodes[node_id] = {"id": node_id, "type": "chart", "chart": block.get("chart")}
        elif block_type == "model":
            nodes[node_id] = {"id": node_id, "type": "model", **(block.get("model") or {})}
        elif block_type == "html":
            nodes[node_id] = {"id": node_id, "type": "html", "html": block.get("html") or ""}
        elif block_type in ("images", "figure"):
            # Images already have individual addressable nodes above.  Do not
            # hide them inside another opaque gallery block.
            continue
        else:
            continue
        block_ids.append(node_id)

    if clean.get("html"):
        nodes["html-1"] = {"id": "html-1", "type": "html", "html": clean["html"]}
        block_ids.append("html-1")

    if not nodes:
        return None

    sources = _container("sources", source_ids, gap=0) if source_ids else None
    media_grid = _container(
        "media-grid", image_ids, axis="grid", columns=2 if len(image_ids) < 5 else 3, gap=10,
    ) if image_ids else None
    data_nodes = [node_id for node_id in (table_id, chart_id) if node_id]

    # Build a layout tree from individual evidence objects.  These are not
    # renderer modes: the result is ordinary container data, freely patchable.
    if want == "images" and image_ids:
        # 要看图：一张大图打头、其余成阵，说明文字退到图后面
        lead = image_ids[0]
        remaining = _container(
            "more-images", image_ids[1:], axis="grid", columns=3, gap=10,
        ) if len(image_ids) > 1 else None
        children = [lead, remaining, summary_id, *block_ids]
    elif want == "list" and source_ids:
        # 要清单：编号条目就是主体，摘要只当一句引子
        children = [summary_id, sources, *data_nodes, *block_ids, media_grid]
    elif want == "compare":
        # 要对比：先看表，再看结论与出处
        children = [*data_nodes, summary_id, *block_ids, sources, media_grid]
    elif want == "answer" and not model_id:
        # 要答案：结论占主位，出处压到最后一行
        children = [summary_id, *data_nodes, *block_ids, media_grid, sources]
    elif model_id or (_MODEL_QUERY_RE.search(query) and model_id):
        aside_children = [summary_id, *data_nodes, sources]
        body = _container(
            "model-body",
            [model_id, _container("model-aside", aside_children, gap=14)],
            axis="row",
            gap=18,
            weights=[1.7, 1.0],
        )
        children = [body, media_grid, *block_ids]
    elif image_ids and _VISUAL_QUERY_RE.search(query):
        lead = image_ids[0]
        remaining = _container(
            "more-images", image_ids[1:], axis="grid", columns=3, gap=10,
        ) if len(image_ids) > 1 else None
        children = [lead, remaining, summary_id, *data_nodes, *block_ids, sources]
    elif _PRICE_QUERY_RE.search(query):
        children = [summary_id, *data_nodes, *block_ids, sources, media_grid]
    else:
        evidence = [*data_nodes, *block_ids, media_grid]
        children = [summary_id, *evidence, sources]

    layout = _container("canvas-root", children, gap=18)
    document = {
        "id": _stable_tab_id(kind, query, title),
        "kind": kind,
        "title": title,
        "query": query,
        "pending": bool(clean.get("pending")),
        "nodes": nodes,
        "layout": layout,
        "view": {
            "selected_id": "",
            "focus_id": "",
            "zoom": 1.0,
            "fit": "contain",
            "fullscreen": False,
        },
    }
    return sanitize_canvas_document(document)


def _sanitize_layout(raw: Any, node_ids: set, *, depth: int = 0) -> Any:
    if isinstance(raw, str):
        return raw if raw in node_ids else None
    if not isinstance(raw, dict) or depth > 6:
        return None
    children = []
    for child in (raw.get("children") or [])[:48]:
        clean = _sanitize_layout(child, node_ids, depth=depth + 1)
        if clean:
            children.append(clean)
    if not children:
        return None
    axis = str(raw.get("axis") or "column")
    if axis not in ("row", "column", "grid"):
        axis = "column"
    try:
        gap = max(0, min(32, int(raw.get("gap", 16))))
    except (TypeError, ValueError):
        gap = 16
    try:
        columns = max(1, min(6, int(raw.get("columns", 2))))
    except (TypeError, ValueError):
        columns = 2
    result = {
        "id": _s(raw.get("id") or "group-%d" % depth, 64),
        "type": "container",
        "axis": axis,
        "gap": gap,
        "children": children,
    }
    if axis == "grid":
        result["columns"] = columns
    weights = raw.get("weights")
    if isinstance(weights, list):
        clean_weights = []
        for value in weights[:len(children)]:
            try:
                clean_weights.append(max(.1, min(8.0, float(value))))
            except (TypeError, ValueError):
                clean_weights.append(1.0)
        if clean_weights:
            result["weights"] = clean_weights
    return result


def sanitize_canvas_document(raw: Any) -> Optional[Dict[str, Any]]:
    """Validate a canvas after model-authored patches before it reaches DOM."""
    if not isinstance(raw, dict):
        return None
    raw_nodes = raw.get("nodes") if isinstance(raw.get("nodes"), dict) else {}
    nodes: Dict[str, Dict[str, Any]] = {}
    for raw_id, raw_node in list(raw_nodes.items())[:120]:
        node_id = str(raw_id or "")
        if not _SAFE_NODE_ID_RE.fullmatch(node_id) or not isinstance(raw_node, dict):
            continue
        kind = str(raw_node.get("type") or "").lower()
        node: Optional[Dict[str, Any]] = None
        if kind == "text":
            text = _s(raw_node.get("text"), 10000)
            if text:
                node = {"id": node_id, "type": kind, "title": _s(raw_node.get("title"), 100), "text": text}
                if str(raw_node.get("role") or "") == "answer":
                    node["role"] = "answer"
        elif kind == "image":
            url = _safe_remote_url(raw_node.get("url"), image=True)
            if url:
                node = {"id": node_id, "type": kind, "url": url, "caption": _s(raw_node.get("caption"), 240)}
        elif kind == "source":
            url = _safe_remote_url(raw_node.get("url"))
            title = _s(raw_node.get("title"), 180)
            if url or title:
                node = {
                    "id": node_id, "type": kind, "title": title or url, "url": url,
                    "snippet": _s(raw_node.get("snippet"), 700),
                    "date": _s(raw_node.get("date"), 40),
                    "image": _safe_remote_url(raw_node.get("image"), image=True),
                }
        elif kind == "table":
            table = _norm_table(raw_node)
            if table:
                node = {"id": node_id, "type": kind, **table}
        elif kind == "chart":
            chart = _norm_chart(raw_node.get("chart") or raw_node)
            if chart:
                node = {"id": node_id, "type": kind, "chart": chart}
        elif kind == "model":
            model = _norm_model(raw_node)
            if model:
                node = {"id": node_id, "type": kind, **model}
        elif kind == "html":
            html = _s(raw_node.get("html"), 30000)
            if html:
                node = {"id": node_id, "type": kind, "html": html}
        if node:
            display = raw_node.get("display")
            if isinstance(display, dict):
                try:
                    min_height = max(0, min(900, int(display.get("min_height") or 0)))
                except (TypeError, ValueError):
                    min_height = 0
                node["display"] = {
                    "hidden": bool(display.get("hidden")),
                    "min_height": min_height,
                }
            nodes[node_id] = node
    if not nodes:
        return None

    node_ids = set(nodes)
    layout = _sanitize_layout(raw.get("layout"), node_ids)
    if not layout:
        layout = _container("canvas-root", list(nodes), gap=16)
    view = raw.get("view") if isinstance(raw.get("view"), dict) else {}
    selected_id = str(view.get("selected_id") or "")
    focus_id = str(view.get("focus_id") or "")
    try:
        zoom = max(.25, min(6.0, float(view.get("zoom") or 1.0)))
    except (TypeError, ValueError):
        zoom = 1.0
    fit = str(view.get("fit") or "contain")
    if fit not in ("contain", "cover", "width", "actual"):
        fit = "contain"
    return {
        "id": _s(raw.get("id") or "research", 64),
        "kind": _s(raw.get("kind") or "generic", 32),
        "title": _s(raw.get("title") or "研究结果", 140),
        "query": _s(raw.get("query"), 240),
        "pending": bool(raw.get("pending")),
        "answer_locked": bool(raw.get("answer_locked")),
        "nodes": nodes,
        "layout": layout,
        "view": {
            "selected_id": selected_id if selected_id in node_ids else "",
            "focus_id": focus_id if focus_id in node_ids else "",
            "zoom": zoom,
            "fit": fit,
            "fullscreen": bool(view.get("fullscreen")),
        },
    }
