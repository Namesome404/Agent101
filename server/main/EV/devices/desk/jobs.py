# -*- coding: utf-8 -*-
"""长任务（安装等）：进度 + 可取消，与写码互斥/排队。"""
from __future__ import annotations

import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from devices.coding import agent_runtime
from devices.desk import hub

_LOCK = threading.RLock()
_JOBS: Dict[str, Dict[str, Any]] = {}

COMMANDS = {
    "npm_i": ["npm", "install"],
    "npm_ci": ["npm", "ci"],
    "pip_r": ["pip", "install", "-r", "requirements.txt"],
    "pnpm_i": ["pnpm", "install"],
}


def active_job() -> Optional[Dict[str, Any]]:
    with _LOCK:
        for jid, meta in _JOBS.items():
            if meta.get("alive"):
                return dict(meta)
    return None


def start_install(
    *,
    cwd: str,
    command_key: str = "npm_i",
    get_setting=None,
    window_id: str = "prereq-job",
) -> Dict[str, Any]:
    if agent_runtime.get_active_run():
        return {"ok": False, "error": "正在写码，安装已拒绝（请稍后再装或排队）", "busy": "coding"}
    if active_job():
        return {"ok": False, "error": "已有安装任务进行中", "busy": "job"}

    cmd = COMMANDS.get(command_key)
    if not cmd:
        return {"ok": False, "error": "未知安装命令"}
    root = Path(cwd or "").expanduser()
    if not root.is_dir():
        return {"ok": False, "error": "cwd 无效"}

    jid = "job_%s" % uuid.uuid4().hex[:10]
    hub.upsert_window({
        "id": window_id,
        "title": "安装任务",
        "preset": "studio",
        "sections": [{
            "title": "日志",
            "blocks": [{"type": "log", "id": "job-log", "text": ""}],
        }],
    })
    hub.append_log(window_id, "开始：%s @ %s" % (" ".join(cmd), root))

    def _worker():
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception as exc:
            with _LOCK:
                _JOBS[jid]["alive"] = False
                _JOBS[jid]["ok"] = False
                _JOBS[jid]["error"] = str(exc)
            hub.append_log(window_id, "失败：%s" % exc, level="error")
            return
        with _LOCK:
            _JOBS[jid]["proc"] = proc
            _JOBS[jid]["pid"] = proc.pid
        assert proc.stdout is not None
        for line in proc.stdout:
            hub.append_log(window_id, line.rstrip()[:400])
            with _LOCK:
                if _JOBS.get(jid, {}).get("cancelled"):
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
        code = proc.wait()
        with _LOCK:
            _JOBS[jid]["alive"] = False
            _JOBS[jid]["ok"] = code == 0
            _JOBS[jid]["exit_code"] = code
        hub.append_log(window_id, "结束 exit=%s" % code, level="info" if code == 0 else "error")

    with _LOCK:
        _JOBS[jid] = {
            "job_id": jid,
            "alive": True,
            "cwd": str(root),
            "command": cmd,
            "window_id": window_id,
            "started_at": time.time(),
            "cancelled": False,
        }
    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "job_id": jid, "window_id": window_id}


def cancel(job_id: str = "") -> bool:
    with _LOCK:
        targets = [job_id] if job_id and job_id in _JOBS else [j for j, m in _JOBS.items() if m.get("alive")]
        ok = False
        for jid in targets:
            meta = _JOBS.get(jid) or {}
            meta["cancelled"] = True
            proc = meta.get("proc")
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    ok = True
                except Exception:
                    pass
            meta["alive"] = False
        return ok
