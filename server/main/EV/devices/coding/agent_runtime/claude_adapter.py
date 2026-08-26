# -*- coding: utf-8 -*-
"""Compatibility adapter for existing Claude Code installations."""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from devices.coding import claude_code
from devices.coding.agent_runtime.protocol import event


def available() -> bool:
    return bool(claude_code.find_claude_binary())


def public_config(get_setting, set_setting) -> Dict[str, Any]:
    return claude_code.public_config(get_setting, set_setting)


def run_task(
    task: str,
    *,
    run_id: str,
    get_setting,
    set_setting,
    cwd: str,
    mode: str,
    base_url: str,
    timeout_s: Optional[int],
    resume_session_id: str,
    on_event: Optional[Callable[[Dict[str, Any]], None]],
) -> Dict[str, Any]:
    def bridge(raw: Dict[str, Any]):
        tool = str(raw.get("tool") or "").lower()
        path = str(raw.get("path") or "")
        text = str(raw.get("text") or "")
        if path or any(key in tool for key in ("write", "edit", "patch")):
            normalized = event("file.changed", phase="editing", detail="修改文件", path=path)
        elif tool:
            normalized = event("check.started", phase="checking", detail=text or "运行检查", command=tool)
        elif raw.get("kind") == "done":
            normalized = event("turn.completed", phase="completed" if raw.get("ok") else "failed", detail=text, ok=raw.get("ok"))
        else:
            normalized = event("activity", phase="working", detail=text or "处理中")
        if on_event:
            on_event(normalized)

    result = claude_code.run_task(
        task,
        get_setting=get_setting,
        set_setting=set_setting,
        cwd=cwd,
        mode=mode,
        base_url=base_url,
        timeout_s=timeout_s,
        on_event=bridge,
        resume_session_id=resume_session_id,
        run_id=run_id,
    )
    return {**result, "provider": "claude"}


def cancel(run_id: str) -> bool:
    return bool(claude_code.cancel_run(run_id))
