# -*- coding: utf-8 -*-
"""工程编排：确认后的工作单 → 通用 Agent Runtime → 可验证回执。"""
from __future__ import annotations

import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from coding import path_policy
from devices.coding import checkpoint as checkpoint_mod
from devices.coding import agent_runtime
from devices.coding import project_fsm
from devices.coding import turn_trace
from devices.coding.scene_store import scene_store

OnSpeech = Optional[Callable[[str], None]]

STUDIO_ID = "work-hud"
LEGACY_STUDIO_ID = "coding-studio"
SITE_ID = "site-preview"


def _default_cwd(get_setting, aid: int, mode: str = "external") -> str:
    st = project_fsm.load(aid)
    cwd = (st.get("brief") or {}).get("cwd") or ""
    valid = path_policy.validate_cwd(cwd, mode, get_setting)[0] if cwd else False
    if cwd and valid:
        return cwd
    return str(path_policy.muse_root() if mode == "self_extend" else path_policy.default_external_root(get_setting))


def push_studio(
    aid: int,
    *,
    status: str,
    detail: str = "",
    phase: str = "",
    percent: Optional[int] = None,
    files: Optional[List[str]] = None,
    log: Optional[List[str]] = None,
    plan_steps: Optional[List[str]] = None,
    risks: Optional[List[Any]] = None,
    preview_url: str = "",
    preview_path: str = "",
    preview_locked: bool = False,
    done: bool = False,
    ok: Optional[bool] = None,
    cwd: str = "",
    summary: str = "",
) -> Dict[str, Any]:
    """发布紧凑工作 HUD；保留函数名供旧调用方平滑迁移。"""
    st = project_fsm.load(aid)
    del percent, plan_steps, risks, preview_url, preview_path, preview_locked, cwd, summary
    active = agent_runtime.get_active_run() or {}
    run_id = str(active.get("run_id") or (st.get("active_run") or {}).get("run_id") or "")
    recent = agent_runtime.get_events(run_id)[-3:] if run_id else []
    if not recent and log:
        recent = [{"label": "处理", "detail": str(line)[:120]} for line in log[-3:] if str(line).strip()]
    state = {
        "run_id": run_id,
        "phase": str((recent[-1].get("phase") if recent else "") or phase or st.get("phase") or "working"),
        "status": str(status or "处理中"),
        "detail": str(detail or "")[:160],
        "files": list(files or active.get("files") or [])[-6:],
        "checks": int(active.get("checks") or 0),
        "events": recent,
        "done": bool(done),
        "ok": ok,
        "agent_id": int(aid),
    }
    panel = {"title": "工作 Agent", "window_id": STUDIO_ID, "data": state}
    visible = bool(done or (phase or st.get("phase")) == "writing" or active.get("alive"))
    scene_store.upsert(
        STUDIO_ID,
        kind="web-surface",
        data={
            "title": "工作 Agent",
            "window": {
                "width": 152, "height": 224, "compact": True,
                "always_on_top": False, "anchored_to": "status-timeline",
            },
            "app": {"id": "agent-work", "state": state},
            "content": {"type": "app", "app_id": "agent-work", "source": {"type": "agent-runtime"}},
            "source_state": state,
        },
        intent="inform", focus=False, order=9, visible=visible,
    )
    if scene_store.get(LEGACY_STUDIO_ID):
        scene_store.remove(LEGACY_STUDIO_ID)
    if done:
        def hide_finished(expected_run_id: str):
            time.sleep(7)
            current = scene_store.get(STUDIO_ID) or {}
            data = current.get("data") if isinstance(current.get("data"), dict) else {}
            source_state = data.get("source_state") if isinstance(data.get("source_state"), dict) else {}
            if str(source_state.get("run_id") or "") == expected_run_id and not agent_runtime.get_active_run():
                scene_store.upsert(
                    STUDIO_ID,
                    kind=current.get("kind") or "web-surface",
                    data=data,
                    intent=current.get("intent") or "inform",
                    focus=False,
                    visible=False,
                )
        threading.Thread(target=hide_finished, args=(run_id,), daemon=True).start()
    return panel


def ensure_preview_window(
    aid: int,
    *,
    url: str = "",
    path: str = "",
    open_native: bool = False,
    base_url: str = "http://127.0.0.1:8002",
) -> str:
    """Publish/reload the reusable project-result surface."""
    del open_native, base_url
    if url:
        refresh_rev = time.time_ns()
        separator = "&" if "?" in url else "?"
        refreshed_url = "%s%s__ev_rev=%s" % (url, separator, refresh_rev)
        scene_store.upsert(
            SITE_ID,
            kind="web-surface",
            data={
                "title": "项目结果",
                "window": {"width": 900, "height": 680},
                "theme": {"background": "#111318", "foreground": "#f4f5f2", "accent": "#8fefbd"},
                "content": {"type": "url", "url": refreshed_url, "source": {"type": "project-result"}},
                "source_state": {"path": path or "", "locked": False, "artifact_rev": refresh_rev},
            },
            intent="inform",
            focus=True,
            order=20,
        )
        # 仅把预览 URL 写回 Studio 数据，不强行把 Studio 拉到前台
        st = project_fsm.load(aid)
        brief = st.get("brief") or {}
        if brief.get("preview_url") != url or brief.get("preview_path") != path:
            project_fsm.set_preview(aid, path=path, url=url, locked=False)
    return url or ""


def ensure_result_window(aid: int, result: Dict[str, Any], *, base_url: str, get_setting=None) -> str:
    """Show any verified project output; HTML is live, other artifacts get a manifest."""
    url = str(result.get("preview_url") or "")
    path = str(result.get("preview_path") or "")
    if url:
        return ensure_preview_window(aid, url=url, path=path, base_url=base_url)
    artifacts = [item for item in (result.get("artifacts") or []) if isinstance(item, dict)]
    if not artifacts:
        return ""
    refresh_rev = time.time_ns()
    content: Dict[str, Any] = {
        "type": "stream",
        "text": str(result.get("summary") or "项目执行完成"),
        "items": ["%s · %s bytes" % (item.get("path"), item.get("bytes", 0)) for item in artifacts],
        "source": {"type": "project-result"},
    }
    first_path = str(artifacts[0].get("path") or "")
    if get_setting and first_path:
        try:
            root = Path(path_policy.default_external_root(get_setting)).expanduser().resolve()
            target = (Path(str(result.get("cwd") or root)).expanduser().resolve() / first_path).resolve()
            public_path = str(target.relative_to(root)).replace("\\", "/")
            artifact_url = "%s/api/agent-runtime/preview/%s?__ev_rev=%s" % (
                base_url.rstrip("/"), urllib.parse.quote(public_path, safe="/"), refresh_rev,
            )
            suffix = target.suffix.lower()
            if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
                content = {"type": "image", "url": artifact_url, "source": {"type": "project-result"}}
            elif suffix in {".txt", ".md", ".json", ".pdf"}:
                content = {"type": "url", "url": artifact_url, "source": {"type": "project-result"}}
        except Exception:
            pass
    scene_store.upsert(
        SITE_ID,
        kind="web-surface",
        data={
            "title": "项目结果",
            "window": {"width": 900, "height": 680},
            "theme": {"background": "#111318", "foreground": "#f4f5f2", "accent": "#8fefbd", "font": "mono"},
            "content": content,
            "source_state": {"artifacts": artifacts, "artifact_rev": refresh_rev},
        },
        intent="inform", focus=True, order=20,
    )
    return SITE_ID


def ensure_terminal_window(aid: int, *, base_url: str = "http://127.0.0.1:8002", open_native: bool = False) -> str:
    """显示当前工作 Agent 的紧凑活动流。"""
    del base_url, open_native
    run = agent_runtime.get_active_run() or {}
    run_id = run.get("run_id") or ""
    lines: List[str] = []
    if run_id:
        for ev in agent_runtime.get_events(run_id):
            t = (ev.get("detail") or "").strip()
            if t:
                lines.append(t)
    push_studio(
        aid,
        status="处理中" if run_id else "空闲",
        detail="实时活动流",
        phase="writing" if run_id else project_fsm.get_phase(aid),
        log=lines[-40:],
        percent=40 if run_id else None,
    )
    return STUDIO_ID


def start_writing(
    aid: int,
    task: str,
    *,
    get_setting,
    set_setting,
    base_url: str = "http://127.0.0.1:8002",
    mode: str = "external",
    cwd: str = "",
    open_desk: bool = True,
) -> Dict[str, Any]:
    """进入 writing：快照 → 通用工作 Agent → 文件哈希回执。"""
    turn_trace.record_runtime("coding.start_requested", {
        "agent_id": aid, "mode": mode, "cwd": cwd,
        "task_preview": str(task or "")[:500], "open_surface": bool(open_desk),
    }, category="coding")
    cfg = agent_runtime.load_config(get_setting, set_setting)
    if not cfg.get("enabled"):
        turn_trace.record_runtime("coding.start_rejected", {
            "agent_id": aid, "reason": "agent_disabled",
        }, category="coding", severity="warning")
        return {"ok": False, "queued": False, "speech": "工作 Agent 已关闭，任务还没有开始。"}
    if not cfg.get("available"):
        turn_trace.record_runtime("coding.start_rejected", {
            "agent_id": aid, "reason": "agent_unavailable", "provider": cfg.get("provider"),
        }, category="coding", severity="warning")
        return {"ok": False, "queued": False, "speech": "工作 Agent 现在不可用，任务还没有开始。"}
    if agent_runtime.get_active_run():
        project_fsm.set_pending_patch(aid, task)
        push_studio(aid, status="已排队", detail="这轮写完马上改", phase="writing", percent=8)
        turn_trace.record_runtime("coding.queued", {
            "agent_id": aid, "reason": "active_run", "task_preview": str(task or "")[:500],
        }, category="coding")
        return {
            "ok": True,
            "queued": True,
            "speech": "记下了，这轮写完马上改。",
        }

    cwd = cwd or _default_cwd(get_setting, aid, mode)
    project_fsm.update_brief(aid, {
        "cwd": cwd,
        "goal": (project_fsm.load(aid).get("brief") or {}).get("goal") or task[:120],
        "current_request": task[:500],
    })
    project_fsm.transition(aid, "writing", reason="start_writing")

    ck = checkpoint_mod.create_checkpoint(cwd, label=task[:80])
    project_fsm.set_checkpoint(aid, ck)

    st = project_fsm.load(aid)
    prev_url = (st.get("brief") or {}).get("preview_url") or ""
    project_fsm.set_preview(aid, locked=True)

    log_lines: List[str] = ["准备工作区"]
    if open_desk:
        push_studio(
            aid,
            status="处理中",
            detail=(task or "")[:120],
            phase="writing",
            percent=8,
            log=list(log_lines),
            preview_url=prev_url,
            preview_locked=True,
            cwd=cwd,
        )

    session_id = st.get("session_id") or ""
    last_push = {"t": 0.0}

    def on_event(ev: Dict[str, Any]):
        text = (ev.get("detail") or "").strip()
        if text:
            log_lines.append(text)
            if len(log_lines) > 60:
                del log_lines[:-50]
        if ev.get("path"):
            log_lines.append("文件 · %s" % ev.get("path"))

    def on_progress(panel: Dict[str, Any]):
        # 节流：最多 ~4 次/秒，始终更新同一 window_id
        now = time.time()
        if now - last_push["t"] < 0.25 and not (panel.get("data") or {}).get("done"):
            return
        last_push["t"] = now
        data = panel.get("data") if isinstance(panel.get("data"), dict) else {}
        push_studio(
            aid,
            status=str(data.get("status") or "处理中"),
            detail=str(data.get("detail") or "")[:160],
            phase=str(data.get("phase") or "writing"),
            percent=data.get("percent"),
            files=list(data.get("files") or []),
            log=list(log_lines)[-40:],
            preview_url=str(data.get("preview_url") or prev_url or ""),
            preview_path=str(data.get("preview_path") or ""),
            preview_locked=True,
            done=bool(data.get("done")),
            ok=data.get("ok"),
            cwd=cwd,
            summary=str(data.get("summary") or ""),
        )

    def on_done(result: Dict[str, Any]):
        _finish_run(aid, result, get_setting=get_setting, set_setting=set_setting, base_url=base_url, mode=mode)

    box = agent_runtime.start_task_background(
        task,
        get_setting=get_setting,
        set_setting=set_setting,
        cwd=cwd,
        mode=mode,
        base_url=base_url,
        on_progress=on_progress,
        on_event=on_event,
        on_done=on_done,
        resume_session_id=session_id or "",
    )
    if not box.get("started"):
        project_fsm.transition(aid, "idle", reason="agent_start_failed")
        project_fsm.set_preview(aid, locked=False)
        return {
            "ok": False, "queued": False,
            "speech": str(box.get("error") or "工作 Agent 没能启动。"),
            "error": str(box.get("error") or ""),
        }
    run_id = box.get("run_id")
    turn_trace.record_runtime("coding.submitted", {
        "agent_id": aid, "run_id": run_id, "cwd": cwd, "checkpoint_ok": bool(ck.get("ok")),
    }, category="coding")
    project_fsm.set_active_run(aid, {
        "run_id": run_id,
        "pid": None,
        "started_at": time.time(),
        "cwd": cwd,
    })

    def _fill_pid():
        time.sleep(0.3)
        meta = agent_runtime.get_active_run()
        if meta:
            project_fsm.set_active_run(aid, {
                "run_id": meta.get("run_id"),
                "pid": meta.get("pid"),
                "started_at": meta.get("started_at"),
                "cwd": cwd,
            })
    threading.Thread(target=_fill_pid, daemon=True).start()
    return {"ok": True, "queued": False, "run_id": run_id, "speech": "开始处理。"}


def _finish_run(aid: int, result: Dict[str, Any], *, get_setting, set_setting, base_url: str, mode: str) -> None:
    executor_ok = bool(result.get("ok"))
    outcome = str(result.get("task_outcome") or ("completed" if result.get("verified_changes") else "failed"))
    ok = executor_ok and outcome == "completed" and bool(result.get("verified_changes"))
    turn_trace.record_runtime("coding.runtime_receipt", {
        "agent_id": aid, "run_id": result.get("run_id"), "executor_ok": executor_ok,
        "task_outcome": outcome, "verified_changes": bool(result.get("verified_changes")),
        "accepted_as_complete": ok, "error": str(result.get("error") or "")[:1000],
        "artifacts": [item.get("path") for item in (result.get("artifacts") or []) if isinstance(item, dict)][:80],
    }, category="coding", severity="info" if ok else "warning")
    preview = result.get("preview_path") or ""
    url = result.get("preview_url") or ""
    project_fsm.set_last_run(
        aid,
        ok=ok,
        verified_changes=bool(result.get("verified_changes")),
        summary=str(result.get("summary") or "")[:800],
        error=str(result.get("error") or ""),
        session_id=str(result.get("session_id") or "") or None,
        task_outcome=outcome,
        executor_ok=executor_ok,
    )
    try:
        from devices.coding import run_memory
        run_memory.remember(aid, result, task="")
    except Exception:
        pass
    if url or preview:
        project_fsm.set_preview(aid, path=preview, url=url, locked=False)
    if ok and (url or preview or result.get("artifacts")):
        ensure_result_window(aid, result, base_url=base_url, get_setting=get_setting)

    push_studio(
        aid,
        status="完成" if ok else ("需要补充" if outcome == "needs_input" else "失败"),
        detail=(
            "已生成可预览页面"
            if url and ok
            else (
                result.get("error")
                or ("文件改动已经核验" if result.get("verified_changes") else "执行结束，但没有检测到文件改动")
            )
        ),
        phase="idle",
        percent=100,
        files=[a.get("path") for a in (result.get("artifacts") or []) if isinstance(a, dict)][:16],
        preview_url=url,
        preview_path=preview,
        preview_locked=False,
        done=True,
        ok=ok,
        summary=str(result.get("summary") or "")[:800],
        log=["完成" if ok else ("未完成: " + str(result.get("error") or "没有文件变化回执"))],
    )
    project_fsm.transition(
        aid,
        "idle" if ok else ("clarifying" if outcome == "needs_input" else "idle"),
        reason="run_done" if ok else ("run_needs_input" if outcome == "needs_input" else "run_failed"),
    )

    # EV owns the user-facing handoff. The provider summary is evidence, not a
    # second assistant persona speaking directly to the user.
    changed_files = [
        str(item.get("path") or "")
        for item in (result.get("artifacts") or [])
        if isinstance(item, dict) and str(item.get("path") or "")
    ]
    raw_summary = " ".join(str(result.get("summary") or "").split())[:220]
    if ok:
        file_note = ""
        if changed_files:
            shown = "、".join(changed_files[:3])
            file_note = "改动已经核验：%s%s。" % (shown, " 等 %d 个文件" % len(changed_files) if len(changed_files) > 3 else "")
        completion_notice = "这项工作完成了。%s%s" % (file_note, raw_summary)
    elif outcome == "needs_input":
        completion_notice = "这项工作还不能算完成：没有检测到可核验的文件改动。%s" % raw_summary
    elif outcome == "cancelled":
        completion_notice = "这项工作已经停止。"
    else:
        completion_notice = "这项工作没有完成：%s" % str(result.get("error") or raw_summary or "执行器返回失败")[:220]
    try:
        from control_plane import database as db
        from control_plane import live_hub
        db.append_conversation_message(aid, "assistant", completion_notice, source="work-agent")
        live_hub.push_utterance(aid, "assistant", completion_notice, turn_id="work:%s" % str(result.get("run_id") or ""), final=True)
        live_hub.push_status(aid, "done" if ok else "error", completion_notice[:100], turn_id="work:%s" % str(result.get("run_id") or ""))
    except Exception:
        pass

    pending = project_fsm.pop_pending_patch(aid)
    if pending:
        time.sleep(0.2)
        start_writing(
            aid,
            pending,
            get_setting=get_setting,
            set_setting=set_setting,
            base_url=base_url,
            mode=mode,
            open_desk=True,
        )


def handle_status_query(aid: int) -> str:
    return project_fsm.status_speech(aid)


def handle_revert(aid: int) -> Dict[str, Any]:
    st = project_fsm.load(aid)
    result = checkpoint_mod.revert_checkpoint(st.get("last_checkpoint") or {})
    if result.get("ok"):
        url = (st.get("brief") or {}).get("preview_url") or ""
        push_studio(aid, status="已回滚", detail="回到改之前", phase="idle", preview_url=url)
    return result
