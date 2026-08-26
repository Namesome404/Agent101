# -*- coding: utf-8 -*-
"""Provider-neutral work-agent runtime used by coding, PCB and CAD workflows."""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from coding import path_policy
from devices.coding.agent_runtime import claude_adapter, codex_app_server
from devices.coding.agent_runtime.protocol import event, public_event


ProgressCb = Optional[Callable[[Dict[str, Any]], None]]
EventCb = Optional[Callable[[Dict[str, Any]], None]]
DoneCb = Optional[Callable[[Dict[str, Any]], None]]

_LOCK = threading.RLock()
_ACTIVE: Dict[str, Dict[str, Any]] = {}
_EVENTS: Dict[str, List[Dict[str, Any]]] = {}
_RESULTS: Dict[str, Dict[str, Any]] = {}
_EVENT_LIMIT = 500


def _setting(get_setting, key: str, default: str = "") -> str:
    try:
        value = get_setting(key)
    except Exception:
        value = None
    return str(value if value not in (None, "") else default)


def selected_provider(get_setting) -> str:
    forced = str(os.environ.get("EV_WORK_AGENT_PROVIDER") or "").strip().lower()
    selected = forced or _setting(get_setting, "agent_runtime.provider", "auto").strip().lower()
    if selected in {"codex", "claude"}:
        return selected
    if codex_app_server.available():
        return "codex"
    if claude_adapter.available():
        return "claude"
    return "codex"


def load_config(get_setting, set_setting=None) -> Dict[str, Any]:
    provider = selected_provider(get_setting)
    return {
        "enabled": _setting(get_setting, "agent_runtime.enabled", "1").lower() not in {"0", "false", "off"},
        "provider": provider,
        "configured_provider": _setting(get_setting, "agent_runtime.provider", "auto"),
        "available": codex_app_server.available() if provider == "codex" else claude_adapter.available(),
        "providers": {
            "codex": {"available": codex_app_server.available()},
            "claude": {"available": claude_adapter.available()},
        },
        "timeout_s": max(30, int(_setting(get_setting, "agent_runtime.timeout_s", "900") or 900)),
    }


def public_config(get_setting, set_setting=None) -> Dict[str, Any]:
    return load_config(get_setting, set_setting)


def apply_config_update(get_setting, set_setting, payload: Dict[str, Any]) -> Dict[str, Any]:
    body = dict(payload or {})
    if "provider" in body:
        provider = str(body.get("provider") or "auto").strip().lower()
        if provider not in {"auto", "codex", "claude"}:
            raise ValueError("provider 只能是 auto、codex 或 claude")
        set_setting("agent_runtime.provider", provider)
    if "enabled" in body:
        set_setting("agent_runtime.enabled", "1" if bool(body.get("enabled")) else "0")
    if "timeout_s" in body:
        timeout_s = max(30, min(7200, int(body.get("timeout_s") or 900)))
        set_setting("agent_runtime.timeout_s", str(timeout_s))
    return load_config(get_setting, set_setting)


def _push_event(run_id: str, item: Dict[str, Any], callback: EventCb = None) -> Dict[str, Any]:
    with _LOCK:
        events = _EVENTS.setdefault(run_id, [])
        normalized = public_event(item)
        normalized["seq"] = int(events[-1].get("seq") or 0) + 1 if events else 1
        normalized["run_id"] = run_id
        normalized.setdefault("ts", time.time())
        events.append(normalized)
        if len(events) > _EVENT_LIMIT:
            del events[: len(events) - _EVENT_LIMIT]
        meta = _ACTIVE.get(run_id)
        if meta is not None:
            meta["phase"] = normalized.get("phase") or meta.get("phase")
            meta["detail"] = normalized.get("detail") or meta.get("detail")
            path = str(normalized.get("path") or "")
            if path and path not in meta["files"]:
                meta["files"].append(path)
                del meta["files"][:-12]
            if normalized.get("kind", "").startswith("check."):
                meta["checks"] = int(meta.get("checks") or 0) + 1
    if callback:
        try:
            callback(dict(normalized))
        except Exception:
            pass
    return normalized


def _progress(run_id: str, *, done=False, ok=None) -> Dict[str, Any]:
    with _LOCK:
        meta = dict(_ACTIVE.get(run_id) or {})
        events = list(_EVENTS.get(run_id) or [])[-3:]
    return {
        "title": "工作 Agent",
        "window_id": "work-hud",
        "data": {
            "kind": "agent-work", "run_id": run_id,
            "phase": meta.get("phase") or "starting",
            "status": meta.get("status") or ("done" if done else "running"),
            "detail": meta.get("detail") or "准备开始",
            "files": list(meta.get("files") or []),
            "checks": int(meta.get("checks") or 0),
            "events": events, "done": bool(done), "ok": ok,
            "provider": meta.get("provider") or "",
        },
    }


def get_events(run_id: str, *, after: int = 0) -> List[Dict[str, Any]]:
    with _LOCK:
        items = list(_EVENTS.get(str(run_id or "")) or [])
    return [item for item in items if int(item.get("seq") or 0) > int(after or 0)]


def get_result(run_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        result = _RESULTS.get(str(run_id or ""))
        return dict(result) if isinstance(result, dict) else None


def active_run_ids() -> List[str]:
    with _LOCK:
        return [run_id for run_id, meta in _ACTIVE.items() if meta.get("alive")]


def get_active_run() -> Optional[Dict[str, Any]]:
    with _LOCK:
        for meta in _ACTIVE.values():
            if meta.get("alive"):
                return {key: value for key, value in meta.items() if key not in {"adapter"}}
    return None


def cancel_run(run_id: str = "") -> bool:
    with _LOCK:
        if run_id:
            meta = _ACTIVE.get(run_id)
        else:
            meta = next((item for item in _ACTIVE.values() if item.get("alive")), None)
        if not meta:
            return False
        rid = str(meta.get("run_id") or "")
        provider = str(meta.get("provider") or "")
        adapter = meta.get("adapter")
    ok = bool(adapter.cancel()) if provider == "codex" and adapter else claude_adapter.cancel(rid)
    if ok:
        _push_event(rid, event("turn.cancelled", phase="cancelled", detail="正在停止", ok=False))
    return ok


def steer_run(text: str, run_id: str = "") -> bool:
    with _LOCK:
        meta = _ACTIVE.get(run_id) if run_id else next((item for item in _ACTIVE.values() if item.get("alive")), None)
        adapter = meta.get("adapter") if meta else None
        rid = str((meta or {}).get("run_id") or "")
    if not adapter or not hasattr(adapter, "steer"):
        return False
    ok = bool(adapter.steer(text))
    if ok:
        _push_event(rid, event("turn.steered", phase="working", detail="已接收你的补充"))
    return ok


def start_task_background(
    task: str,
    *,
    get_setting,
    set_setting,
    cwd: str = "",
    mode: str = "external",
    base_url: str = "http://127.0.0.1:8002",
    timeout_s: Optional[int] = None,
    on_progress: ProgressCb = None,
    on_event: EventCb = None,
    on_done: DoneCb = None,
    resume_session_id: str = "",
) -> Dict[str, Any]:
    config = load_config(get_setting, set_setting)
    if not config.get("enabled"):
        return {"started": False, "error": "工作 Agent 已关闭"}
    if get_active_run():
        return {"started": False, "busy": True, "error": "已有任务在执行"}
    ok, error_text, resolved = path_policy.validate_cwd(cwd or "", mode, get_setting)
    if not ok:
        return {"started": False, "error": error_text}
    provider = str(config.get("provider") or "codex")
    available = codex_app_server.available() if provider == "codex" else claude_adapter.available()
    if not available:
        return {"started": False, "error": "当前工作 Agent 不可用", "provider": provider}

    run_id = "work_%s" % int(time.time() * 1000)
    with _LOCK:
        _EVENTS[run_id] = []
        _ACTIVE[run_id] = {
            "run_id": run_id, "provider": provider, "alive": True, "pid": None,
            "cwd": str(resolved), "started_at": time.time(), "status": "running",
            "phase": "starting", "detail": "准备开始", "files": [], "checks": 0,
            "adapter": None,
        }

    def bridge(item: Dict[str, Any]):
        _push_event(run_id, item, on_event)
        if on_progress:
            try:
                on_progress(_progress(run_id))
            except Exception:
                pass

    adapter = None
    if provider == "codex":
        adapter = codex_app_server.CodexAppServerRun(
            run_id=run_id, cwd=resolved, base_url=base_url, on_event=bridge,
        )
        with _LOCK:
            _ACTIVE[run_id]["adapter"] = adapter

    def worker():
        nonlocal adapter
        bridge(event("runtime.started", phase="starting", detail="正在准备工作区"))
        try:
            if provider == "codex":
                result = adapter.run(
                    task, resume_session_id=resume_session_id,
                    timeout_s=int(timeout_s or config.get("timeout_s") or 900),
                )
                if (
                    str(config.get("configured_provider") or "auto") == "auto"
                    and result.get("task_outcome") == "failed"
                    and not result.get("verified_changes")
                    and claude_adapter.available()
                ):
                    bridge(event(
                        "runtime.fallback", phase="starting",
                        detail="主执行器连接失败，正在切换本机兼容执行器",
                    ))
                    with _LOCK:
                        _ACTIVE[run_id]["provider"] = "claude"
                        _ACTIVE[run_id]["adapter"] = None
                    result = claude_adapter.run_task(
                        task, run_id=run_id, get_setting=get_setting, set_setting=set_setting,
                        cwd=str(resolved), mode=mode, base_url=base_url,
                        timeout_s=timeout_s, resume_session_id="", on_event=bridge,
                    )
            else:
                result = claude_adapter.run_task(
                    task, run_id=run_id, get_setting=get_setting, set_setting=set_setting,
                    cwd=str(resolved), mode=mode, base_url=base_url,
                    timeout_s=timeout_s, resume_session_id=resume_session_id, on_event=bridge,
                )
        except Exception as exc:
            result = {
                "ok": False, "run_id": run_id, "provider": provider,
                "cwd": str(resolved), "error": str(exc), "summary": "",
                "verified_changes": False, "task_outcome": "failed", "artifacts": [],
            }
        outcome = str(result.get("task_outcome") or ("completed" if result.get("ok") else "failed"))
        phase = "completed" if outcome == "completed" else outcome
        with _LOCK:
            meta = _ACTIVE.get(run_id) or {}
            meta.update({"alive": False, "status": outcome, "phase": phase, "detail": str(result.get("error") or result.get("summary") or "")[:160]})
            _RESULTS[run_id] = dict(result)
        bridge(event("runtime.completed", phase=phase, detail=str(result.get("error") or "完成")[:180], ok=outcome == "completed"))
        if on_progress:
            try:
                on_progress(_progress(run_id, done=True, ok=outcome == "completed"))
            except Exception:
                pass
        if on_done:
            try:
                on_done(dict(result))
            except Exception:
                pass

    thread = threading.Thread(target=worker, daemon=True, name="ev-work-agent-%s" % run_id)
    thread.start()
    return {"started": True, "run_id": run_id, "provider": provider}
