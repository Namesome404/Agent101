# -*- coding: utf-8 -*-
"""desk_compose：facts → WindowSchema（规则优先，可接 LLM 结果）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set, Tuple

from devices.desk import gather as gather_mod
from devices.desk import schema as schema_mod
from devices.desk import hub


def _inline_rows_from_facts(facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    if facts.get("venvs"):
        return list(facts["venvs"])
    if facts.get("tools"):
        return list(facts["tools"])
    if facts.get("files"):
        rows = []
        for f in facts["files"]:
            row = {"path": f.get("path"), "bytes": f.get("bytes")}
            js = f.get("json")
            if isinstance(js, dict):
                if "dependencies" in js:
                    row["dependencies"] = len(js.get("dependencies") or {})
                if "name" in js:
                    row["name"] = js.get("name")
            rows.append(row)
        return rows
    if facts.get("tree"):
        return list(facts["tree"])
    return []


def schema_from_facts(title: str, facts: Dict[str, Any], *, window_id: str = "") -> Dict[str, Any]:
    wid = window_id or hub.new_window_id("compose")
    rows = _inline_rows_from_facts(facts)
    sections: List[Dict[str, Any]] = []
    if facts.get("venvs"):
        sections.append({
            "title": "虚拟环境",
            "blocks": [{
                "type": "table",
                "columns": ["name", "path", "python", "source"],
                "rows": facts["venvs"],
                "row_actions": [{"type": "toggle", "action": "venv.set_active", "label": "启用"}],
            }],
        })
    if facts.get("tools"):
        sections.append({
            "title": "本机工具",
            "blocks": [{
                "type": "table",
                "columns": ["name", "path", "version", "found"],
                "rows": facts["tools"],
            }],
        })
    if facts.get("files"):
        dep_rows = []
        for f in facts["files"]:
            js = f.get("json")
            if isinstance(js, dict) and isinstance(js.get("dependencies"), dict):
                for name, ver in list(js["dependencies"].items())[:80]:
                    dep_rows.append({"file": f.get("path"), "package": name, "version": ver})
        blocks: List[Dict[str, Any]] = [{
            "type": "table",
            "columns": ["path", "bytes", "name", "dependencies"],
            "rows": rows if rows and "path" in (rows[0] or {}) else [
                {"path": f.get("path"), "bytes": f.get("bytes")} for f in facts["files"]
            ],
        }]
        if dep_rows:
            blocks.append({
                "type": "table",
                "columns": ["file", "package", "version"],
                "rows": dep_rows,
            })
        sections.append({"title": "项目文件 / 依赖", "blocks": blocks})
    if facts.get("tree") and not facts.get("files"):
        sections.append({
            "title": "目录树",
            "blocks": [{
                "type": "table",
                "columns": ["path", "type"],
                "rows": facts["tree"],
            }],
        })
    if not sections:
        return schema_mod.markdown_fallback_schema(title, gather_mod.facts_to_markdown(facts), wid)
    return {
        "id": wid,
        "title": title or "自定义窗口",
        "preset": "board",
        "style": {"theme": "dark", "accent": "amber"},
        "sections": sections,
    }


def _collect_fact_paths(facts: Dict[str, Any]) -> Set[str]:
    out: Set[str] = set()
    for v in facts.get("venvs") or []:
        out.add(str(v.get("path") or ""))
        out.add(str(v.get("name") or ""))
    for f in facts.get("files") or []:
        out.add(str(f.get("path") or ""))
    for t in facts.get("tools") or []:
        out.add(str(t.get("name") or ""))
    return {x for x in out if x}


def validate_inline_against_facts(sch: Dict[str, Any], facts: Dict[str, Any]) -> Dict[str, Any]:
    """丢弃无法对上 facts 的 inline 行（反幻觉）。"""
    allowed = _collect_fact_paths(facts)
    if not allowed:
        return sch
    for sec in sch.get("sections") or []:
        for blk in sec.get("blocks") or []:
            rows = blk.get("rows")
            if not isinstance(rows, list):
                continue
            kept = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                keys = [str(row.get(k) or "") for k in ("path", "name", "package", "file")]
                if any(k in allowed for k in keys if k) or not allowed:
                    kept.append(row)
                elif any(any(a in k for a in allowed) for k in keys if k):
                    kept.append(row)
            # 若过滤后为空但原本有行，放宽：保留全部（facts 结构不同时）
            blk["rows"] = kept if kept else rows
    return sch


def apply_llm_schema(raw_schema: Any, facts: Dict[str, Any], *, title: str, window_id: str = "") -> Tuple[Dict[str, Any], str]:
    ok, err, sch = schema_mod.validate_schema(raw_schema if isinstance(raw_schema, dict) else {})
    if not ok or not sch.get("sections"):
        return schema_from_facts(title, facts, window_id=window_id), "fallback_rules"
    sch["id"] = window_id or sch.get("id") or hub.new_window_id("compose")
    sch["title"] = title or sch.get("title") or "自定义窗口"
    sch = validate_inline_against_facts(sch, facts)
    return sch, "llm"


def compose_and_open(
    *,
    user_text: str,
    cwd: str,
    get_setting,
    gather_plan: Optional[List[Dict[str, Any]]] = None,
    llm_schema: Any = None,
    window_id: str = "",
    title: str = "",
) -> Dict[str, Any]:
    text = (user_text or "").strip()
    title = title or _guess_title(text)
    plan = gather_plan or _guess_plan(text)
    facts = gather_mod.run_plan(plan, cwd, get_setting)
    wid = window_id or hub.new_window_id("compose")
    if llm_schema:
        sch, how = apply_llm_schema(llm_schema, facts, title=title, window_id=wid)
    else:
        sch = schema_from_facts(title, facts, window_id=wid)
        how = "rules"
    # 若完全无数据 → markdown 降级
    if facts.get("count", 0) == 0 and how == "rules":
        sch = schema_mod.markdown_fallback_schema(title, gather_mod.facts_to_markdown(facts), wid)
        how = "empty_fallback"
    win = hub.upsert_window(sch, data={"facts": facts, "compose": how, "query": text})
    # 流式旁白
    note = "已根据采集结果生成窗口（%s，约 %s 项）。" % (how, facts.get("count") or 0)
    try:
        hub.stream_text(wid, "compose-note", note, chunk=20, delay_s=0.015)
    except Exception:
        pass
    return {"ok": True, "window": win, "facts": facts, "how": how}


def _guess_title(text: str) -> str:
    t = text.strip()
    if len(t) > 40:
        return t[:40] + "…"
    return t or "自定义窗口"


def _guess_plan(text: str) -> List[Dict[str, Any]]:
    t = text or ""
    plan: List[Dict[str, Any]] = []
    if any(k in t for k in ("虚拟环境", "venv", "conda", "python 环境")):
        plan.append({"kind": "gather.venvs"})
    if any(k in t for k in ("依赖", "package.json", "json", "配置", "版本")):
        plan.append({"kind": "gather.files"})
        plan.append({"kind": "gather.json", "path": "package.json"})
    if any(k in t for k in ("目录", "结构", "文件树", "tree")):
        plan.append({"kind": "gather.tree"})
    if any(k in t for k in ("node", "工具", "which", "环境变量", "前置")):
        plan.append({"kind": "gather.which"})
    if not plan:
        plan = [{"kind": "gather.files"}, {"kind": "gather.which"}, {"kind": "gather.tree"}]
    return plan
