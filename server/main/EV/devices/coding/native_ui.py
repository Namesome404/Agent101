# -*- coding: utf-8 -*-
"""macOS 系统级进度窗 + 通知 + 用默认浏览器打开预览。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from common.paths import TMP_DIR

_STATE_PATH = TMP_DIR / "claude_code_native_progress.json"
_LOCK = threading.Lock()
_PROC: Optional[subprocess.Popen] = None


def notify(title: str, body: str) -> None:
    title = (title or "EV").replace('"', "")
    body = (body or "").replace('"', "")[:180]
    script = (
        'display notification "%s" with title "%s" sound name "Tink"'
        % (body, title)
    )
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def open_url(url: str) -> bool:
    url = (url or "").strip()
    if not url:
        return False
    try:
        subprocess.Popen(
            ["open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def open_desk_window(url: str, *, title: str = "EV Desk") -> bool:
    """优先 Chrome --app= 系统级打开 Desk；失败再普通 open。"""
    url = (url or "").strip()
    if not url:
        return False
    chrome_candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for bin_path in chrome_candidates:
        if Path(bin_path).exists():
            try:
                subprocess.Popen(
                    [bin_path, "--app=%s" % url, "--new-window"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                continue
    # macOS open -a Chrome
    try:
        subprocess.Popen(
            ["open", "-na", "Google Chrome", "--args", "--app=%s" % url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        pass
    return open_url(url)


def reveal_in_finder(path: str) -> bool:
    p = Path(path or "").expanduser()
    if not p.exists():
        return False
    try:
        if p.is_file():
            subprocess.Popen(["open", "-R", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _write_state(payload: Dict[str, Any]) -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_STATE_PATH)


def _window_script() -> str:
    # 独立小窗：读状态文件刷新进度，完成后可点「打开网站」
    return r'''
import json, time, subprocess, tkinter as tk
from pathlib import Path
STATE = Path(r"""''' + str(_STATE_PATH) + r'''""")

root = tk.Tk()
root.title("EV · Claude Code")
root.attributes("-topmost", True)
try:
    root.lift()
    root.focus_force()
except Exception:
    pass
try:
    root.configure(bg="#1a1a1a")
except Exception:
    pass
# 居中，避免藏在角落看不见
try:
    root.update_idletasks()
    w, h = 440, 280
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = max(40, (sw - w) // 2)
    y = max(40, (sh - h) // 3)
    root.geometry("%dx%d+%d+%d" % (w, h, x, y))
except Exception:
    root.geometry("440x280+120+120")

title = tk.Label(root, text="Claude Code", fg="#f0ece4", bg="#1a1a1a", font=("Helvetica", 16, "bold"))
title.pack(anchor="w", padx=16, pady=(16, 4))
status = tk.Label(root, text="启动中…", fg="#d6d2ca", bg="#1a1a1a", font=("Helvetica", 13))
status.pack(anchor="w", padx=16)
detail = tk.Label(root, text="", fg="#a8a49c", bg="#1a1a1a", font=("Helvetica", 11), wraplength=380, justify="left")
detail.pack(anchor="w", padx=16, pady=(6, 8))
bar_bg = tk.Frame(root, bg="#2a2a2a", height=10)
bar_bg.pack(fill="x", padx=16)
bar_fg = tk.Frame(bar_bg, bg="#7cb8ff", height=10, width=8)
bar_fg.place(x=0, y=0, relheight=1.0)
files = tk.Label(root, text="", fg="#c8c4bc", bg="#1a1a1a", font=("Menlo", 10), justify="left", wraplength=380)
files.pack(anchor="w", padx=16, pady=(10, 4))
btns = tk.Frame(root, bg="#1a1a1a")
btns.pack(fill="x", padx=16, pady=10)
url_holder = {"url": ""}

def open_site():
    u = url_holder.get("url") or ""
    if u:
        subprocess.Popen(["open", u])

btn_open = tk.Button(btns, text="打开网站", command=open_site, state="disabled")
btn_open.pack(side="left")
btn_close = tk.Button(btns, text="关闭", command=root.destroy)
btn_close.pack(side="right")

def tick():
    try:
        if STATE.exists():
            data = json.loads(STATE.read_text(encoding="utf-8"))
            status.config(text=str(data.get("status") or ""))
            detail.config(text=str(data.get("detail") or ""))
            pct = max(0, min(100, int(data.get("percent") or 0)))
            bar_fg.place(x=0, y=0, relheight=1.0, relwidth=max(0.02, pct / 100.0))
            fl = data.get("files") or []
            files.config(text=("文件：\n" + "\n".join(fl[:8])) if fl else "")
            url_holder["url"] = str(data.get("preview_url") or "")
            if data.get("done") and url_holder["url"]:
                btn_open.config(state="normal")
            if data.get("close"):
                root.destroy()
                return
    except Exception:
        pass
    root.after(400, tick)

root.after(200, tick)
root.mainloop()
'''


def ensure_progress_window() -> None:
    global _PROC
    with _LOCK:
        if _PROC is not None and _PROC.poll() is None:
            return
        script = _window_script()
        log_path = TMP_DIR / "claude_code_native_ui.log"
        try:
            log_f = open(log_path, "ab")
        except Exception:
            log_f = subprocess.DEVNULL
        _PROC = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
        )
        # 给窗口一点启动时间；失败则打通知兜底
        time.sleep(0.25)
        if _PROC.poll() is not None:
            notify("EV · Claude Code", "进度窗启动失败，请看通知更新")
            try:
                print("[native_ui] window exited early code=%s" % _PROC.returncode, flush=True)
            except Exception:
                pass
        else:
            _frontmost_pid(_PROC.pid)


def _frontmost_pid(pid: int) -> None:
    if not pid:
        return
    script = (
        'tell application "System Events"\n'
        '  set frontmost of (first process whose unix id is %d) to true\n'
        "end tell"
    ) % int(pid)
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def update_progress(
    *,
    status: str,
    detail: str = "",
    percent: int = 0,
    files: Optional[list] = None,
    done: bool = False,
    ok: Optional[bool] = None,
    preview_url: str = "",
    send_notification: bool = False,
) -> None:
    ensure_progress_window()
    payload = {
        "status": status,
        "detail": detail,
        "percent": int(percent or 0),
        "files": list(files or [])[:12],
        "done": bool(done),
        "ok": ok,
        "preview_url": preview_url or "",
        "ts": time.time(),
    }
    _write_state(payload)
    with _LOCK:
        proc = _PROC
    if proc is not None and proc.poll() is None:
        _frontmost_pid(proc.pid)
    if send_notification:
        notify("EV · Claude Code", "%s %s" % (status, detail or ""))


def close_progress(delay_s: float = 0.0) -> None:
    def _later():
        if delay_s > 0:
            time.sleep(delay_s)
        try:
            if _STATE_PATH.exists():
                data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            else:
                data = {}
            data["close"] = True
            _write_state(data)
        except Exception:
            pass

    threading.Thread(target=_later, daemon=True).start()


def on_coding_panel(panel: Dict[str, Any], *, open_browser_when_done: bool = False) -> None:
    """把 EV coding panel 同步到系统进度窗；完成时可选打开浏览器。"""
    if not isinstance(panel, dict):
        return
    data = panel.get("data") if isinstance(panel.get("data"), dict) else panel
    if (data.get("kind") or panel.get("panel")) not in ("coding", None) and panel.get("panel") not in ("coding", "site"):
        # site panel → 直接开浏览器
        if panel.get("panel") == "site" or data.get("kind") == "site":
            url = panel.get("url") or data.get("url") or ""
            if url:
                open_url(url)
                notify("EV", "已打开网站预览")
            return
    status = str(data.get("status") or panel.get("title") or "进行中")
    detail = str(data.get("detail") or "")
    percent = int(data.get("percent") or 0)
    files = data.get("files") or []
    done = bool(data.get("done"))
    preview_url = str(data.get("preview_url") or "")
    update_progress(
        status=status,
        detail=detail,
        percent=percent,
        files=files,
        done=done,
        ok=data.get("ok"),
        preview_url=preview_url,
        send_notification=done or percent in (5, 15, 35) or (not done and percent <= 8),
    )
    if done and open_browser_when_done and preview_url and data.get("ok"):
        open_url(preview_url)
        notify("EV · 写好了", "已在浏览器打开预览")
