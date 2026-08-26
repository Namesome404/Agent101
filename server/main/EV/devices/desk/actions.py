# -*- coding: utf-8 -*-
"""Desk 白名单 action handler。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from devices.coding import checkpoint as checkpoint_mod
from devices.coding import project_fsm
from devices.coding import agent_runtime
from devices.desk import hub, style_spec, jobs
from coding import path_policy


def dispatch(action: str, payload: Optional[Dict[str, Any]] = None, *, aid: int = 0, get_setting=None) -> Dict[str, Any]:
    action = (action or "").strip()
    payload = payload or {}
    wid = str(payload.get("window_id") or "")

    if action == "window.close":
        return {"ok": hub.close_window(wid), "action": action}
    if action == "window.refresh":
        w = hub.get_window(wid)
        return {"ok": bool(w), "window": w, "action": action}
    if action == "window.set_style":
        try:
            w = hub.patch_style(wid, payload.get("style") or payload)
            return {"ok": True, "window": w}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    if action == "window.reset_style":
        try:
            return {"ok": True, "window": hub.reset_style(wid)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if action == "venv.set_active":
        path = str(payload.get("path") or payload.get("item_id") or "")
        if not path:
            return {"ok": False, "error": "缺少 path"}
        st = project_fsm.update_brief(aid, {"venv": path})
        return {"ok": True, "venv": path, "brief": st.get("brief")}

    if action == "prereq.install":
        return jobs.start_install(
            cwd=str(payload.get("cwd") or (project_fsm.load(aid).get("brief") or {}).get("cwd") or ""),
            command_key=str(payload.get("command") or "npm_i"),
            get_setting=get_setting,
            window_id=wid or "prereq-job",
        )

    if action == "job.cancel":
        return {"ok": jobs.cancel(str(payload.get("job_id") or "")), "action": action}

    if action == "preview.lock":
        hub.set_preview(wid or "site-preview", locked=True)
        project_fsm.set_preview(aid, locked=True)
        return {"ok": True}
    if action == "preview.unlock":
        hub.set_preview(wid or "site-preview", locked=False)
        project_fsm.set_preview(aid, locked=False)
        return {"ok": True}
    if action == "preview.reload":
        st = project_fsm.load(aid)
        url = (st.get("brief") or {}).get("preview_url") or ""
        hub.set_preview(wid or "site-preview", url=url, locked=False)
        return {"ok": True, "preview_url": url}

    if action == "open_in_browser":
        from devices.coding import native_ui
        url = str(payload.get("url") or (project_fsm.load(aid).get("brief") or {}).get("preview_url") or "")
        ok = native_ui.open_url(url) if url else False
        return {"ok": ok, "url": url}

    if action == "coding.cancel":
        ok = agent_runtime.cancel_run()
        project_fsm.transition(aid, "idle", reason="user_cancel")
        hub.set_preview("site-preview", locked=False)
        return {"ok": ok}

    if action == "coding.revert_last":
        st = project_fsm.load(aid)
        meta = st.get("last_checkpoint")
        result = checkpoint_mod.revert_checkpoint(meta or {})
        if result.get("ok"):
            url = (st.get("brief") or {}).get("preview_url") or ""
            if url:
                hub.set_preview("site-preview", url=url, locked=False)
        return result

    if action == "coding.confirm_write":
        project_fsm.transition(aid, "writing", reason="confirm")
        return {"ok": True, "phase": "writing"}

    return {"ok": False, "error": "未注册或禁止的 action: %s" % action}
