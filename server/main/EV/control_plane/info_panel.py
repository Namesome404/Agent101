# -*- coding: utf-8 -*-
"""Versioned research-canvas store shared by search, AI and direct gestures.

Search writes semantic canvas documents into tabs. AI commands and browser
gestures both reach :func:`apply` through constrained state changes; there is no
separate "enlarge image" or "switch to table layout" capability. A revision
check and strict schema validation make every visible change verifiable.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from typing import Any, Dict, List, Tuple


_LOCK = threading.RLock()
_MAX_TABS = 8
_STATE: Dict[str, Any] = {
    "rev": 0,
    "expanded": False,
    "active_tab_id": "",
    "tab_order": [],
    "tabs": {},
}


def _pointer_parts(path: Any) -> List[str]:
    value = str(path or "")
    if not value.startswith("/"):
        raise ValueError("patch path must be a JSON pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in value[1:].split("/")]


def _lookup(document: Any, parts: List[str]) -> Any:
    target = document
    for part in parts:
        if isinstance(target, list):
            target = target[int(part)]
        elif isinstance(target, dict):
            if part not in target:
                raise ValueError("patch target missing: /%s" % "/".join(parts))
            target = target[part]
        else:
            raise ValueError("patch target is not a container")
    return target


def _parent(document: Any, parts: List[str]) -> Tuple[Any, str]:
    if not parts:
        return None, ""
    return _lookup(document, parts[:-1]) if len(parts) > 1 else document, parts[-1]


def _remove(document: Any, parts: List[str]) -> Any:
    parent, leaf = _parent(document, parts)
    if parent is None:
        raise ValueError("cannot remove the canvas root")
    if isinstance(parent, list):
        return parent.pop(int(leaf))
    if not isinstance(parent, dict) or leaf not in parent:
        raise ValueError("patch remove target missing")
    return parent.pop(leaf)


def _write(document: Any, parts: List[str], value: Any, *, replace: bool) -> Any:
    if not parts:
        if replace:
            return copy.deepcopy(value)
        raise ValueError("root add is not supported")
    parent, leaf = _parent(document, parts)
    clean = copy.deepcopy(value)
    if isinstance(parent, list):
        if leaf == "-":
            if replace:
                raise ValueError("cannot replace list append position")
            parent.append(clean)
        else:
            index = int(leaf)
            if replace:
                parent[index] = clean
            else:
                parent.insert(index, clean)
        return document
    if not isinstance(parent, dict):
        raise ValueError("patch target is not a container")
    if replace and leaf not in parent:
        raise ValueError("patch replace target missing; use add")
    parent[leaf] = clean
    return document


def _json_patch(document: Any, operations: List[Dict[str, Any]]) -> Any:
    """Small strict RFC-6902 implementation for the canvas transaction."""
    result = copy.deepcopy(document)
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("every patch operation must be an object")
        op = str(operation.get("op") or "").lower()
        parts = _pointer_parts(operation.get("path"))
        if op == "test":
            if _lookup(result, parts) != operation.get("value"):
                raise ValueError("patch test failed")
            continue
        if op == "remove":
            _remove(result, parts)
            continue
        if op in ("add", "replace"):
            result = _write(result, parts, operation.get("value"), replace=op == "replace")
            continue
        if op in ("move", "copy"):
            source_parts = _pointer_parts(operation.get("from"))
            value = copy.deepcopy(_lookup(result, source_parts))
            if op == "move":
                _remove(result, source_parts)
            result = _write(result, parts, value, replace=False)
            continue
        raise ValueError("unsupported patch op: %s" % op)
    return result


def _workspace_locked() -> Dict[str, Any]:
    return {
        "expanded": bool(_STATE.get("expanded")),
        "active_tab_id": _STATE["active_tab_id"],
        "tab_order": copy.deepcopy(_STATE["tab_order"]),
        "tabs": copy.deepcopy(_STATE["tabs"]),
    }


def _active_document_locked() -> Dict[str, Any] | None:
    active = str(_STATE.get("active_tab_id") or "")
    document = (_STATE.get("tabs") or {}).get(active)
    return copy.deepcopy(document) if isinstance(document, dict) else None


def _tab_summaries_locked() -> List[Dict[str, Any]]:
    tabs = _STATE.get("tabs") or {}
    result = []
    for tab_id in _STATE.get("tab_order") or []:
        document = tabs.get(tab_id)
        if not isinstance(document, dict):
            continue
        result.append({
            "id": tab_id,
            "title": document.get("title") or "研究结果",
            "kind": document.get("kind") or "generic",
            "pending": bool(document.get("pending")),
            "updated_at": float(document.get("updated_at") or 0),
        })
    return result


def _snapshot_locked() -> Dict[str, Any]:
    document = _active_document_locked()
    return {
        "rev": int(_STATE.get("rev") or 0),
        "expanded": bool(_STATE.get("expanded")),
        "active_tab_id": str(_STATE.get("active_tab_id") or ""),
        "tabs": _tab_summaries_locked(),
        "document": document,
        # One-release compatibility for consumers of the old endpoint. The
        # value is now a canvas document, not a layout-enum payload.
        "payload": document,
    }


def push(
    payload: dict,
    *,
    expand: bool = True,
    activate: bool = True,
    kind: str = "",
    layout: str = "",
    pending: bool = False,
) -> dict:
    """Normalize a retrieval result and upsert it as a research tab.

    ``layout`` is accepted only for source compatibility and intentionally
    ignored: presentation now lives in the editable layout tree.
    """
    del layout
    from control_plane import panel_contract

    clean = panel_contract.normalize(payload, kind=kind, pending=pending)
    if not clean:
        return snapshot()
    document = panel_contract.to_canvas_document(clean)
    if not document:
        return snapshot()
    now = time.time()
    document["updated_at"] = now
    tab_id = document["id"]
    with _LOCK:
        existing = (_STATE.get("tabs") or {}).get(tab_id)
        if isinstance(existing, dict):
            # Background enrichment must not kick the user out of an image or
            # model they already focused while the full result was loading.
            old_view = existing.get("view")
            if isinstance(old_view, dict):
                next_view = dict(document.get("view") or {})
                for key in ("selected_id", "focus_id", "zoom", "fit", "fullscreen"):
                    if key in old_view:
                        next_view[key] = old_view[key]
                document["view"] = next_view
            # 模型依据检索回执生成的最终答案比抓取器的聚合摘要更适合用户阅读。
            # 后台补图/补正文可以更新证据，但不能再把清晰答案覆盖成抓取文本。
            if existing.get("answer_locked"):
                old_summary = (existing.get("nodes") or {}).get("summary")
                if isinstance(old_summary, dict):
                    document.setdefault("nodes", {})["summary"] = copy.deepcopy(old_summary)
                document["answer_locked"] = True
            document["created_at"] = float(existing.get("created_at") or now)
        else:
            document["created_at"] = now
            _STATE["tab_order"].append(tab_id)
        _STATE["tabs"][tab_id] = document
        # 后台补图/补正文只更新对应结果，不能在用户已经开始下一次搜索后
        # 把旧结果重新抢到前台。
        if activate or not _STATE.get("active_tab_id"):
            _STATE["active_tab_id"] = tab_id
        while len(_STATE["tab_order"]) > _MAX_TABS:
            stale = _STATE["tab_order"].pop(0)
            if stale != tab_id:
                _STATE["tabs"].pop(stale, None)
        if expand:
            _STATE["expanded"] = True
        _STATE["rev"] += 1
        return _snapshot_locked()


def inspect(tab_id: str = "") -> Dict[str, Any]:
    with _LOCK:
        target = str(tab_id or _STATE.get("active_tab_id") or "")
        document = (_STATE.get("tabs") or {}).get(target)
        if not isinstance(document, dict):
            return {"ok": False, "error": "empty_canvas", **_snapshot_locked()}
        return {
            "ok": True,
            "rev": int(_STATE.get("rev") or 0),
            "active_tab_id": str(_STATE.get("active_tab_id") or ""),
            "tab_id": target,
            "tabs": _tab_summaries_locked(),
            "document": copy.deepcopy(document),
        }


_SPEECH_LEAD_RE = re.compile(
    r"^(?:好的|可以|行|没问题|搜到了|查到了|我(?:已经)?(?:查|搜)(?:到|了)|"
    r"我看了(?:一下)?|根据(?:搜索|查询)结果)[，,：:\s]*",
)
_QUESTION_RE = re.compile(r"[？?]\s*$")
_TITLE_SUFFIX_RE = re.compile(
    r"(?:\s+(?:预约|门票|注意事项|攻略|官网|图片|照片|价格|售价|资料|信息)){1,}$",
)


def _compact_display_summary(text: str) -> str:
    """Turn spoken prose into a short factual card, never a transcript copy."""
    source = re.sub(r"\s+", " ", str(text or "")).strip()
    if not source:
        return ""
    # 口播稿里的 **强调** 是给语音合成看的，面板上只会露出一对星号
    source = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", source)
    source = _SPEECH_LEAD_RE.sub("", source).strip()
    sentences = [
        part.strip()
        for part in re.findall(r"[^。！？!?]+[。！？!?]?", source)
        if part.strip()
    ]
    factual = []
    for sentence in sentences:
        if _QUESTION_RE.search(sentence):
            continue
        sentence = re.sub(r"(?:官网|页面|资料)(?:上)?显示", "", sentence)
        sentence = sentence.replace("参观要", "参观需").replace("晚上8点", "20:00")
        sentence = sentence.strip(" ，,：:")
        if sentence:
            factual.append(sentence)
        if len("".join(factual)) >= 150 or len(factual) >= 1:
            break
    summary = "".join(factual).strip()
    if not summary:
        summary = source[:180].rstrip("，,：:")
    if summary == source:
        clauses = [part.strip() for part in re.split(r"[，,；;]", summary) if part.strip()]
        if len(clauses) > 2:
            summary = "，".join(clauses[:2])
    summary = summary[:180].rstrip("，,；;：:")
    if summary and summary[-1] not in "。！？!?":
        summary += "。"
    return summary


def _compact_summary_title(value: str) -> str:
    title = re.sub(r"^搜索[：:]\s*", "", str(value or "")).strip()
    title = _TITLE_SUFFIX_RE.sub("", title).strip()
    return (title or "信息摘要")[:48]


def set_answer(tab_id: str, text: str, entries: Any = None) -> Dict[str, Any]:
    """Commit a concise evidence summary, separate from the spoken reply."""
    answer = str(text or "").strip()
    if not answer:
        return {"ok": False, "error": "empty_answer", **snapshot()}
    display_summary = _compact_display_summary(answer)
    with _LOCK:
        target = str(tab_id or _STATE.get("active_tab_id") or "")
        document = (_STATE.get("tabs") or {}).get(target)
        if not isinstance(document, dict):
            return {"ok": False, "error": "tab_not_found", **_snapshot_locked()}
        before = copy.deepcopy(document)
        nodes = document.setdefault("nodes", {})
        summary = nodes.get("summary") if isinstance(nodes.get("summary"), dict) else {}
        nodes["summary"] = {
            "id": "summary",
            "type": "text",
            "role": "answer",
            "title": _compact_summary_title(document.get("query") or document.get("title")),
            "text": display_summary,
        }
        # 模型声明了要摆什么，就按它的来：面板列的是回答里真正讲到的对象，
        # 各带一行提纯说明，而不是搜索命中的那些「XX 大盘点」文章页。
        clean_entries = [
            item for item in (entries or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ][:8]
        if clean_entries:
            # 条目已经把内容说清楚了，结论区只留一句引子；否则同一件事在面板上
            # 写两遍（上面一段散文、下面一条条目），把本就有限的高度浪费掉。
            lead = display_summary
            for item in clean_entries:
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                position = lead.find(name)
                if position > 0:
                    lead = lead[:position]
            # 引子必须是完整的一句：在条目名处切开后若没有句末标点，说明剩下的
            # 只是「给你说几个适合入门的」这种半截话，摆出来反而像坏掉了。
            cut = max(lead.rfind(mark) for mark in "。！？!?：:")
            lead = (lead[:cut + 1] if cut >= 0 else "").strip(" ，,：:；;。-—")
            nodes["summary"]["text"] = lead if len(lead) >= 6 else ""
            for node_id in [
                key for key, node in nodes.items()
                if isinstance(node, dict) and node.get("type") == "source"
            ]:
                nodes.pop(node_id, None)
            for index, item in enumerate(clean_entries, 1):
                node_id = "source-%d" % index
                nodes[node_id] = {
                    "id": node_id,
                    "type": "source",
                    "title": str(item.get("name") or "")[:120],
                    "url": str(item.get("url") or "")[:900],
                    "snippet": str(item.get("note") or "")[:400],
                    "date": "",
                    "image": "",
                }
            document["layout"] = {
                "id": "canvas-root",
                "type": "container",
                "axis": "column",
                "gap": 14,
                "children": (["summary"] if nodes["summary"].get("text") else []) + [
                    {
                        "id": "sources",
                        "type": "container",
                        "axis": "column",
                        "gap": 0,
                        "children": ["source-%d" % n for n in range(1, len(clean_entries) + 1)],
                    },
                ],
            }
        document["answer_locked"] = True
        document["pending"] = False
        document["updated_at"] = time.time()
        if document != before:
            _STATE["rev"] += 1
        return {"ok": True, "tab_id": target, **_snapshot_locked()}


def apply(
    *,
    patches: List[Dict[str, Any]],
    tab_id: str = "",
    base_rev: int = 0,
) -> Dict[str, Any]:
    """Atomically patch a canvas document or the tab workspace.

    Document paths are relative to the selected tab (``/view/focus_id``,
    ``/layout/children/0`` or ``/nodes/image-2/display/hidden``). Workspace
    paths start with ``/active_tab_id``, ``/tab_order`` or ``/tabs``.
    """
    from control_plane import panel_contract

    if int(base_rev or 0) <= 0:
        return {"ok": False, "error": "base_rev_required", **snapshot()}
    operations = patches if isinstance(patches, list) else []
    if not operations or len(operations) > 32:
        return {"ok": False, "error": "invalid_patches", **snapshot()}
    try:
        encoded = json.dumps(operations, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"ok": False, "error": "patches_not_json", **snapshot()}
    if len(encoded) > 160000:
        return {"ok": False, "error": "patches_too_large", **snapshot()}

    affected = [str(op.get("path") or "") for op in operations if isinstance(op, dict)]
    workspace_roots = {"expanded", "active_tab_id", "tab_order", "tabs"}
    try:
        roots = {_pointer_parts(path)[0] for path in affected}
    except (ValueError, IndexError):
        return {"ok": False, "error": "invalid_patch_path", **snapshot()}
    workspace_scope = bool(roots & workspace_roots)
    if workspace_scope and not roots <= workspace_roots:
        return {"ok": False, "error": "mixed_patch_scope", **snapshot()}

    with _LOCK:
        current_rev = int(_STATE.get("rev") or 0)
        if int(base_rev or 0) and int(base_rev) != current_rev:
            return {
                "ok": False,
                "error": "revision_conflict",
                "expected_rev": current_rev,
                **_snapshot_locked(),
            }
        try:
            if workspace_scope:
                before = _workspace_locked()
                candidate = _json_patch(before, operations)
                if not isinstance(candidate, dict):
                    raise ValueError("workspace root must be an object")
                raw_tabs = candidate.get("tabs") if isinstance(candidate.get("tabs"), dict) else {}
                tabs: Dict[str, Dict[str, Any]] = {}
                for candidate_id, raw_document in list(raw_tabs.items())[:_MAX_TABS]:
                    document = panel_contract.sanitize_canvas_document(raw_document)
                    if document:
                        raw_core = {
                            key: copy.deepcopy(value) for key, value in raw_document.items()
                            if key not in {"created_at", "updated_at"}
                        }
                        if document != raw_core:
                            raise ValueError(
                                "patch contains unsupported canvas fields; transaction rejected"
                            )
                        document["id"] = str(candidate_id)
                        document["updated_at"] = float(raw_document.get("updated_at") or time.time())
                        document["created_at"] = float(raw_document.get("created_at") or document["updated_at"])
                        tabs[str(candidate_id)] = document
                order = [str(value) for value in (candidate.get("tab_order") or []) if str(value) in tabs]
                for candidate_id in tabs:
                    if candidate_id not in order:
                        order.append(candidate_id)
                active = str(candidate.get("active_tab_id") or "")
                if active not in tabs:
                    active = order[-1] if order else ""
                after = {
                    "expanded": bool(candidate.get("expanded") and tabs),
                    "active_tab_id": active,
                    "tab_order": order,
                    "tabs": tabs,
                }
                changed = after != before
                if changed:
                    _STATE.update(after)
            else:
                target = str(tab_id or _STATE.get("active_tab_id") or "")
                before = (_STATE.get("tabs") or {}).get(target)
                if not isinstance(before, dict):
                    return {"ok": False, "error": "tab_not_found", **_snapshot_locked()}
                candidate = _json_patch(before, operations)
                after_core = panel_contract.sanitize_canvas_document(candidate)
                if not after_core:
                    raise ValueError("patch produced an invalid canvas")
                candidate_core = {
                    key: copy.deepcopy(value) for key, value in candidate.items()
                    if key not in {"created_at", "updated_at"}
                }
                if after_core != candidate_core:
                    raise ValueError(
                        "patch contains unsupported canvas fields; transaction rejected"
                    )
                before_core = {
                    key: copy.deepcopy(value) for key, value in before.items()
                    if key not in {"created_at", "updated_at"}
                }
                after_core["id"] = target
                changed = after_core != before_core
                if changed:
                    after = after_core
                    after["created_at"] = float(before.get("created_at") or time.time())
                    after["updated_at"] = time.time()
                    _STATE["tabs"][target] = after
                    _STATE["active_tab_id"] = target
            if changed:
                if not workspace_scope:
                    _STATE["expanded"] = bool(_STATE.get("tabs"))
                _STATE["rev"] += 1
            snap = _snapshot_locked()
        except (ValueError, TypeError, IndexError, KeyError) as error:
            return {"ok": False, "error": "patch_failed", "detail": str(error)[:240], **_snapshot_locked()}
    return {
        "ok": True,
        "changed": changed,
        "affected_paths": affected,
        "tab_id": str(tab_id or snap.get("active_tab_id") or ""),
        **snap,
    }


def set_expanded(expanded: bool) -> dict:
    with _LOCK:
        want = bool(expanded and _STATE.get("tabs"))
        if _STATE["expanded"] != want:
            _STATE["expanded"] = want
            _STATE["rev"] += 1
        return _snapshot_locked()


def has_content() -> bool:
    with _LOCK:
        return bool(_STATE.get("tabs"))


def snapshot() -> dict:
    with _LOCK:
        return _snapshot_locked()


def clear() -> dict:
    with _LOCK:
        _STATE["tabs"] = {}
        _STATE["tab_order"] = []
        _STATE["active_tab_id"] = ""
        _STATE["expanded"] = False
        _STATE["rev"] += 1
        return _snapshot_locked()
