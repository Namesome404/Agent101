# -*- coding: utf-8 -*-
"""工程会话状态机：相位 + brief + pending + 落盘。"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.paths import TMP_DIR

PHASES = ("idle", "clarifying", "planning", "awaiting_confirm", "writing")
WORK_ORDER_TTL_SECONDS = 2 * 60 * 60

_LOCK = threading.RLock()
_CACHE: Dict[int, Dict[str, Any]] = {}

_ALLOWED = {
    "idle": {"clarifying", "planning", "writing", "idle"},
    "clarifying": {"clarifying", "planning", "writing", "idle", "awaiting_confirm"},
    "planning": {"planning", "awaiting_confirm", "clarifying", "writing", "idle"},
    "awaiting_confirm": {"awaiting_confirm", "planning", "clarifying", "writing", "idle"},
    "writing": {"writing", "idle", "clarifying", "awaiting_confirm"},
}


def _path(aid: int) -> Path:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    return TMP_DIR / ("coding_fsm_%s.json" % int(aid))


def _default_brief() -> Dict[str, Any]:
    return {
        "goal": "",
        "constraints": [],
        "open_questions": [],
        "plan_steps": [],
        "risks": [],
        "diagrams": [],
        "cwd": "",
        "mode": "external",
        "venv": "",
        "preview_path": "",
        "preview_url": "",
        "preview_mode": "static",
    }


def _default_state(aid: int) -> Dict[str, Any]:
    return {
        "agent_id": int(aid),
        "phase": "idle",
        "brief": _default_brief(),
        "session_id": None,
        "pending_patch": None,
        "pending_job": None,
        "last_checkpoint": None,
        "active_run": None,
        "last_run": {"ok": None, "at": 0, "error": "", "summary": ""},
        "work_order": None,
        "preview_locked": False,
        "updated_at": time.time(),
        "interrupted": False,
    }


def load(aid: int) -> Dict[str, Any]:
    aid = int(aid or 0)
    with _LOCK:
        if aid in _CACHE:
            return json.loads(json.dumps(_CACHE[aid]))
        p = _path(aid)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                base = _default_state(aid)
                base.update(data or {})
                migrated = False
                if not isinstance(base.get("brief"), dict):
                    base["brief"] = _default_brief()
                else:
                    merged = _default_brief()
                    merged.update(base["brief"])
                    base["brief"] = merged
                order = base.get("work_order") if isinstance(base.get("work_order"), dict) else {}
                order_age = time.time() - float(order.get("created_at") or base.get("updated_at") or 0)
                if (
                    base.get("phase") == "awaiting_confirm"
                    and order.get("status") == "awaiting_approval"
                    and order_age > WORK_ORDER_TTL_SECONDS
                ):
                    order["status"] = "expired"
                    base["work_order"] = order
                    base["phase"] = "idle"
                    base["last_transition_reason"] = "work_order_expired"
                    migrated = True
                # 重启后：活跃 run 进程若已死，标中断
                run = base.get("active_run") or {}
                pid = run.get("pid")
                if base.get("phase") == "writing" and pid:
                    alive = False
                    try:
                        os.kill(int(pid), 0)
                        alive = True
                    except Exception:
                        alive = False
                    if not alive:
                        base["phase"] = "idle"
                        base["active_run"] = None
                        base["preview_locked"] = False
                        base["interrupted"] = True
                        migrated = True
                _CACHE[aid] = base
                if migrated:
                    tmp = p.with_suffix(".tmp")
                    tmp.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
                    tmp.replace(p)
                return json.loads(json.dumps(base))
            except Exception:
                pass
        st = _default_state(aid)
        _CACHE[aid] = st
        return json.loads(json.dumps(st))


def save(aid: int, state: Dict[str, Any]) -> Dict[str, Any]:
    aid = int(aid or 0)
    with _LOCK:
        state = dict(state or {})
        state["agent_id"] = aid
        state["updated_at"] = time.time()
        _CACHE[aid] = state
        p = _path(aid)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return json.loads(json.dumps(state))


def get_phase(aid: int) -> str:
    return str(load(aid).get("phase") or "idle")


def transition(aid: int, to_phase: str, *, reason: str = "") -> Dict[str, Any]:
    to_phase = (to_phase or "idle").strip()
    if to_phase not in PHASES:
        raise ValueError("unknown phase: %s" % to_phase)
    st = load(aid)
    cur = st.get("phase") or "idle"
    if to_phase != cur and to_phase not in _ALLOWED.get(cur, set()):
        # 允许强制写入 writing→idle 等已声明边；非法则仍写入但记 reason
        pass
    st["phase"] = to_phase
    st["interrupted"] = False
    if reason:
        st["last_transition_reason"] = reason
    if to_phase != "writing":
        st["preview_locked"] = False
    return save(aid, st)


def update_brief(aid: int, patch: Dict[str, Any]) -> Dict[str, Any]:
    st = load(aid)
    brief = dict(st.get("brief") or _default_brief())
    for k, v in (patch or {}).items():
        if v is None:
            continue
        if k in ("constraints", "open_questions", "plan_steps", "risks", "diagrams") and isinstance(v, list):
            brief[k] = v
        else:
            brief[k] = v
    st["brief"] = brief
    return save(aid, st)


def set_active_run(aid: int, run: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    st = load(aid)
    st["active_run"] = run
    if run:
        st["phase"] = "writing"
        st["preview_locked"] = True
    return save(aid, st)


def set_last_run(
    aid: int,
    *,
    ok: bool,
    summary: str = "",
    error: str = "",
    session_id: str = None,
    verified_changes: bool = False,
    task_outcome: str = "",
    executor_ok: Optional[bool] = None,
) -> Dict[str, Any]:
    st = load(aid)
    st["last_run"] = {
        "ok": bool(ok),
        "at": time.time(),
        "summary": (summary or "")[:800],
        "error": (error or "")[:500],
        "verified_changes": bool(verified_changes),
        "task_outcome": str(task_outcome or ("completed" if ok else "failed")),
        "executor_ok": bool(ok if executor_ok is None else executor_ok),
    }
    if session_id:
        st["session_id"] = session_id
    st["active_run"] = None
    st["preview_locked"] = False
    st["phase"] = "idle" if ok else "clarifying"
    return save(aid, st)


def set_pending_patch(aid: int, text: Optional[str]) -> Dict[str, Any]:
    st = load(aid)
    st["pending_patch"] = (text or "").strip() or None
    return save(aid, st)


def pop_pending_patch(aid: int) -> Optional[str]:
    st = load(aid)
    pending = st.get("pending_patch")
    st["pending_patch"] = None
    save(aid, st)
    return (pending or "").strip() or None


def set_pending_job(aid: int, job: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    st = load(aid)
    st["pending_job"] = dict(job) if isinstance(job, dict) else None
    return save(aid, st)


def pop_pending_job(aid: int) -> Optional[Dict[str, Any]]:
    st = load(aid)
    pending = st.get("pending_job")
    st["pending_job"] = None
    save(aid, st)
    return dict(pending) if isinstance(pending, dict) else None


def set_checkpoint(aid: int, meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    st = load(aid)
    st["last_checkpoint"] = meta
    return save(aid, st)


def set_preview(aid: int, *, path: str = "", url: str = "", locked: Optional[bool] = None) -> Dict[str, Any]:
    st = load(aid)
    brief = dict(st.get("brief") or _default_brief())
    if path:
        brief["preview_path"] = path
    if url:
        brief["preview_url"] = url
    st["brief"] = brief
    if locked is not None:
        st["preview_locked"] = bool(locked)
    return save(aid, st)


def prepare_work_order(aid: int, *, goal: str, plan_steps: List[str]) -> Dict[str, Any]:
    """Create a durable approval target; conversation state alone cannot approve it."""
    st = load(aid)
    order = {
        "id": "work_" + uuid.uuid4().hex[:12],
        "revision": 1,
        "status": "awaiting_approval",
        "goal": str(goal or "")[:500],
        "plan_steps": [str(item)[:500] for item in (plan_steps or [])][:40],
        "approved_revision": None,
        "approved_at": 0,
        "approval_source": "",
        "created_at": time.time(),
    }
    st["work_order"] = order
    return save(aid, st)["work_order"]


def update_work_order(aid: int, *, work_id: str, expected_revision: int, plan_steps: List[str]) -> Optional[Dict[str, Any]]:
    st = load(aid)
    order = dict(st.get("work_order") or {})
    if (
        not order
        or order.get("id") != work_id
        or int(order.get("revision") or 0) != int(expected_revision or 0)
        or order.get("status") != "awaiting_approval"
    ):
        return None
    order["revision"] = int(order["revision"]) + 1
    order["plan_steps"] = [str(item)[:500] for item in (plan_steps or [])][:40]
    st["work_order"] = order
    save(aid, st)
    return dict(order)


def approve_work_order(
    aid: int,
    *,
    work_id: str,
    expected_revision: int,
    plan_steps: List[str],
    approval_source: str = "plan_surface_submit",
) -> Optional[Dict[str, Any]]:
    order = update_work_order(
        aid, work_id=work_id, expected_revision=expected_revision, plan_steps=plan_steps,
    )
    if not order:
        return None
    st = load(aid)
    current = dict(st.get("work_order") or {})
    current["status"] = "approved"
    current["approved_revision"] = current.get("revision")
    current["approved_at"] = time.time()
    current["approval_source"] = str(approval_source or "conversation_confirmed")[:80]
    st["work_order"] = current
    save(aid, st)
    return dict(current)


def approve_current_work_order(aid: int, *, approval_source: str = "conversation_confirmed") -> Optional[Dict[str, Any]]:
    """Approve exactly the revision EV most recently described to the user."""
    st = load(aid)
    order = dict(st.get("work_order") or {})
    if not order or order.get("status") != "awaiting_approval":
        return None
    return approve_work_order(
        aid,
        work_id=str(order.get("id") or ""),
        expected_revision=int(order.get("revision") or 0),
        plan_steps=list(order.get("plan_steps") or []),
        approval_source=approval_source,
    )


def status_speech(aid: int) -> str:
    """进度追问用：不交给模型编。"""
    st = load(aid)
    phase = st.get("phase") or "idle"
    if st.get("interrupted"):
        return "上次写到一半已经中断了，要继续的话跟我说改什么。"
    if phase == "writing" or st.get("active_run"):
        pending = st.get("pending_patch")
        if pending:
            return "还在写这一轮，你刚说的修改我记下了，写完马上改。"
        return "还在写，没写完。"
    if phase in ("clarifying", "planning"):
        return "还没开写，在问清需求/做计划。"
    if phase == "awaiting_confirm":
        return "还没开写，在等你确认计划。"
    last = st.get("last_run") or {}
    age = time.time() - float(last.get("at") or 0)
    if last.get("task_outcome") == "needs_input" and age < 600:
        return "上一轮工作 Agent 已退出，但没有检测到文件内容变化，不能算改好了。"
    if last.get("ok") and age < 600:
        if last.get("verified_changes"):
            return "上一轮已经结束，文件改动有运行时回执。还想改直接说。"
        return "上一轮工作 Agent 已结束，但没有检测到文件改动回执。"
    if last.get("ok") is False and age < 600:
        return "上一轮失败了：%s" % (last.get("error") or "未知错误")
    return "现在没有在写。"


def phase_system_prompt(aid: int) -> str:
    st = load(aid)
    phase = st.get("phase") or "idle"
    brief = st.get("brief") or {}
    last = st.get("last_run") or {}
    bits = [
        "【工程相位=%s】相位由系统控制，你不得自行宣布已进入编写或已写完。" % phase,
        "已知目标：%s" % (brief.get("goal") or "（未定）"),
        "如实原则：只陈述系统已记录的相位与最近一次写码结果；未发生的完成态一律禁止。",
    ]
    if brief.get("diagrams"):
        bits.append("已有逻辑图：%s" % (brief["diagrams"][0].get("title") or "diagram"))
    if brief.get("preview_url"):
        bits.append("预览URL已记录（可提及此URL，勿编造其它端口）。")
    else:
        bits.append("尚无系统记录的预览URL。")
    if phase != "writing" and not st.get("active_run"):
        bits.append("禁止说「正在写/已经写好/重写中/改好了」；若用户在等确认，明确说还没开写。")
    else:
        bits.append("正在编写中：禁止说「我还没开始」或「已经写好」；只能说还在写/没写完。可闲聊或搜索，写码进度须诚实。")
    if last:
        age = time.time() - float(last.get("at") or 0)
        if age < 900:
            bits.append(
                "最近一次写码=%s（约%d秒前）。"
                % (("成功" if last.get("ok") else "失败"), int(age))
            )
            if last.get("error"):
                bits.append("失败原因：%s" % str(last.get("error"))[:100])
    if st.get("pending_patch"):
        bits.append("用户有一条排队中的修改，写完后系统会自动执行。")
    return " ".join(bits)


def can_run_claude(aid: int) -> bool:
    return get_phase(aid) == "writing" or bool(load(aid).get("active_run"))
