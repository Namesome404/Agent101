# -*- coding: utf-8 -*-
"""Append-only audit trail for every observable EV action and receipt.

The audit does not infer tool use from assistant prose.  Anomalies are derived
only from the selected capability, actual tool calls, runtime receipts and
renderer acknowledgements.
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterator, List

from common.paths import TMP_DIR

_LOCK = threading.RLock()
TRACE_PATH = Path(TMP_DIR) / "voice_tool_trace.jsonl"
ACTION_TRACE_PATH = Path(TMP_DIR) / "ev_action_trace.jsonl"
_SESSION_ID = uuid.uuid4().hex[:10]
_STARTED_AT = time.monotonic()
_SEQUENCE = 0
_TURN_STATE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_RECENT_USERS: List[Dict[str, Any]] = []
_CONTEXT: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "ev_action_context", default={}
)

_ACTION_SCOPES = {
    "surface_manage", "surface_inspect", "surface_capture",
    "project", "web",
}
_TOOLS_BY_SCOPE = {
    "surface_manage": {"surface_manage"},
    "surface_inspect": {"surface_inspect"},
    "surface_capture": {"surface_expect_input", "surface_manage"},
    "project": {"coding_flow"},
    "web": {"web_search", "web_extract"},
}


def _compact(value: Any, limit: int = 16000) -> Any:
    """Keep receipts useful without allowing generated HTML to flood logs."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return {"serialization_error": True, "preview": str(value)[:1000]}
    if len(encoded) <= limit:
        return value
    return {
        "truncated": True,
        "original_chars": len(encoded),
        "preview": encoded[:limit],
    }


def _state(turn_id: str) -> Dict[str, Any]:
    state = _TURN_STATE.get(turn_id)
    if state is None:
        state = {
            "started_mono": time.monotonic(),
            "decision": {},
            "tool_calls": 0,
            "tool_results": 0,
            "tool_failures": 0,
            "pending_tools": 0,
        }
        _TURN_STATE[turn_id] = state
    else:
        _TURN_STATE.move_to_end(turn_id)
    while len(_TURN_STATE) > 512:
        _TURN_STATE.popitem(last=False)
    return state


def _derive_anomalies(turn_id: str, event: str, data: Dict[str, Any]) -> List[str]:
    if not turn_id:
        return []
    state = _state(turn_id)
    anomalies: List[str] = []
    if event == "user":
        now_mono = time.monotonic()
        agent_id = str(data.get("agent_id") or "")
        text = str(data.get("text") or "").strip()
        _RECENT_USERS[:] = [
            item for item in _RECENT_USERS
            if now_mono - float(item.get("at") or 0) <= 5.0
        ]
        for previous in _RECENT_USERS:
            previous_text = str(previous.get("text") or "").strip()
            if (
                previous.get("turn_id") != turn_id
                and previous.get("agent_id") == agent_id
                and text and previous_text
                and (text.startswith(previous_text) or previous_text.startswith(text))
            ):
                anomalies.append("overlapping_utterance_requests")
                state["related_turn_id"] = previous.get("turn_id")
                break
        _RECENT_USERS.append({
            "turn_id": turn_id, "agent_id": agent_id,
            "text": text, "at": now_mono,
        })
        state.update({
            "started_mono": now_mono, "decision": {},
            "tool_calls": 0, "tool_results": 0, "tool_failures": 0,
            "pending_tools": 0,
        })
    elif event == "decision":
        state["decision"] = dict(data)
        if not data.get("addressed") and data.get("scope") != "ignore":
            anomalies.append("unaddressed_turn_not_ignored")
    elif event == "tool_call":
        state["tool_calls"] += 1
        state["pending_tools"] += 1
        scope = str((state.get("decision") or {}).get("scope") or "")
        name = str(data.get("name") or "")
        allowed = _TOOLS_BY_SCOPE.get(scope)
        if allowed is not None and name not in allowed:
            anomalies.append("tool_outside_selected_scope")
    elif event == "tool_result":
        state["tool_results"] += 1
        state["pending_tools"] = max(0, int(state["pending_tools"]) - 1)
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        if result.get("ok") is False:
            state["tool_failures"] += 1
            anomalies.append("runtime_tool_failed")
    elif event == "assistant":
        scope = str((state.get("decision") or {}).get("scope") or "")
        if state["pending_tools"]:
            anomalies.append("assistant_before_tool_receipt")
        if scope in _ACTION_SCOPES and not state["tool_calls"]:
            anomalies.append("action_scope_without_tool_call")
        if state["tool_failures"]:
            anomalies.append("assistant_after_failed_tool")
    return anomalies


@contextlib.contextmanager
def action_context(turn_id: str, *, actor: str = "ev") -> Iterator[None]:
    """Attach nested Scene/runtime actions to the originating voice turn."""
    token = _CONTEXT.set({"turn_id": str(turn_id or ""), "actor": str(actor or "ev")})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def record(turn_id: str, event: str, data: Dict[str, Any], *,
           category: str = "turn", severity: str = "info") -> Dict[str, Any]:
    global _SEQUENCE
    turn_id = str(turn_id or "")
    event = str(event or "")
    payload = data if isinstance(data, dict) else {}
    now = time.time()
    with _LOCK:
        anomalies = _derive_anomalies(turn_id, event, payload)
        if anomalies and severity == "info":
            severity = "warning"
        _SEQUENCE += 1
        state = _TURN_STATE.get(turn_id) if turn_id else None
        item = {
            "seq": _SEQUENCE,
            "time": dt.datetime.fromtimestamp(now).astimezone().isoformat(timespec="milliseconds"),
            "at": now,
            "session_id": _SESSION_ID,
            "process_id": os.getpid(),
            "thread": threading.current_thread().name,
            "turn_id": turn_id,
            "elapsed_ms": round(
                (time.monotonic() - float(state.get("started_mono"))) * 1000, 1
            ) if state else None,
            "category": str(category or "runtime"),
            "event": event,
            "severity": severity,
            "anomalies": anomalies,
            "data": _compact(payload),
        }
        if state and state.get("related_turn_id"):
            item["related_turn_id"] = state["related_turn_id"]
        encoded = json.dumps(item, ensure_ascii=False, default=str)
        ACTION_TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        for path in (ACTION_TRACE_PATH, TRACE_PATH):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
        return item


def record_runtime(event: str, data: Dict[str, Any], *,
                   category: str = "runtime", severity: str = "info") -> Dict[str, Any]:
    context = _CONTEXT.get()
    return record(
        context.get("turn_id") or "", event,
        {"actor": context.get("actor") or "ev", **(data or {})},
        category=category, severity=severity,
    )


def read_recent(*, limit: int = 200, anomalies_only: bool = False,
                turn_id: str = "") -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 200), 2000))
    if not ACTION_TRACE_PATH.exists():
        return []
    with _LOCK:
        lines = ACTION_TRACE_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    items: List[Dict[str, Any]] = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except Exception:
            continue
        if turn_id and str(item.get("turn_id") or "") != turn_id:
            continue
        if anomalies_only and not item.get("anomalies"):
            continue
        items.append(item)
        if len(items) >= limit:
            break
    items.reverse()
    return items


def read_recent_executions(*, turns: int = 3,
                           exclude_turn_id: str = "") -> List[Dict[str, Any]]:
    """最近几轮真正执行过的工具调用摘要（tool_call + tool_result 配对）。

    供 voice 提示注入「最近执行记录」：模型问『关了/开了/改了哪个窗口』这类回顾
    时，只能依据这里列出的 ok:true 回执回答，禁止用窗口现状反推自己做过什么。
    只返回最近 turns 个有 tool_call 的 turn_id，每个 turn 收敛成
    {"turn_id", "name", "arguments", "result"}，result 缺省时表示无回执（未完成）。
    """
    turns = max(1, min(int(turns or 3), 8))
    if not ACTION_TRACE_PATH.exists():
        return []
    with _LOCK:
        lines = ACTION_TRACE_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    by_turn: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    seen = 0
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except Exception:
            continue
        tid = str(item.get("turn_id") or "")
        if not tid or (exclude_turn_id and tid == exclude_turn_id):
            continue
        event = str(item.get("event") or "")
        if event not in ("tool_call", "tool_result"):
            continue
        if tid not in by_turn:
            if seen >= turns:
                continue
            by_turn[tid] = []
            seen += 1
        by_turn[tid].append({
            "event": event,
            "seq": int(item.get("seq") or 0),
            "name": str((item.get("data") or {}).get("name") or ""),
            "arguments": (item.get("data") or {}).get("arguments"),
            "result": (item.get("data") or {}).get("result"),
        })
    out: List[Dict[str, Any]] = []
    for tid, events in by_turn.items():
        # 同一 turn 内按 seq 升序（文件扫描是倒序，需还原时间顺序）FIFO 配对：
        # call 先入，同名 result 到来时接上。tool_result 事件不含 arguments，只能按 name 匹配。
        queue: List[Dict[str, Any]] = []
        for ev in sorted(events, key=lambda x: x["seq"]):
            if ev["event"] == "tool_call":
                queue.append({
                    "name": ev["name"], "arguments": ev["arguments"], "result": None,
                })
            elif ev["event"] == "tool_result":
                for entry in queue:
                    if entry["result"] is None and entry["name"] == ev["name"]:
                        entry["result"] = ev["result"]
                        break
        for entry in queue:
            out.append({"turn_id": tid, **entry})
    return out[:turns * 4]
