# -*- coding: utf-8 -*-
"""Claude Code CLI 桥：配置、探测、带路径策略的安全启动。"""
from __future__ import annotations

import json
import hashlib
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from coding import path_policy
from common.paths import MUSE_DIR, TMP_DIR
from devices.coding import turn_trace

ProgressCb = Optional[Callable[[Dict[str, Any]], None]]
EventCb = Optional[Callable[[Dict[str, Any]], None]]
_SKIP_NAMES = {".ev-claude-env", "claude-ev", "CLAUDE.ev-sandbox.md", ".DS_Store", ".git"}
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".claude"}

SETTING_PREFIX = "skill.claude_code."

_RUN_LOCK = threading.RLock()
_ACTIVE_RUNS: Dict[str, Dict[str, Any]] = {}
_EVENT_BUFFERS: Dict[str, List[Dict[str, Any]]] = {}
_EVENT_LIMIT = 400


def _g(get_setting, key: str, default=None):
    try:
        v = get_setting(SETTING_PREFIX + key, default)
    except Exception:
        v = default
    return v


def _s(set_setting, key: str, value):
    set_setting(SETTING_PREFIX + key, value)


def ensure_gateway_token(get_setting, set_setting) -> str:
    tok = (_g(get_setting, "gateway_token") or "").strip()
    if tok:
        return tok
    tok = secrets.token_urlsafe(32)
    _s(set_setting, "gateway_token", tok)
    return tok


def load_config(get_setting, set_setting=None) -> Dict[str, Any]:
    enabled = str(_g(get_setting, "enabled", "1") or "1").strip() not in ("0", "false", "False", "off")
    agent_raw = _g(get_setting, "agent_id", "")
    try:
        agent_id = int(agent_raw) if str(agent_raw or "").strip() else None
    except Exception:
        agent_id = None
    timeout_s = 300
    try:
        timeout_s = int(_g(get_setting, "timeout_s", "300") or 300)
    except Exception:
        timeout_s = 300
    token = (_g(get_setting, "gateway_token") or "").strip()
    if set_setting is not None and not token:
        token = ensure_gateway_token(get_setting, set_setting)
    ext = str(path_policy.default_external_root(get_setting))
    return {
        "enabled": enabled,
        "agent_id": agent_id,
        "timeout_s": max(30, min(timeout_s, 3600)),
        "gateway_token_set": bool(token),
        "gateway_token_masked": (token[:4] + "…" + token[-4:]) if token and len(token) > 10 else ("***" if token else ""),
        "external_root": ext,
        "default_mode": (_g(get_setting, "default_mode", "external") or "external").strip() or "external",
        "enable_thinking": str(_g(get_setting, "enable_thinking", "1") or "1").strip() not in ("0", "false", "off"),
        "binary": find_claude_binary(),
    }


def public_config(get_setting, set_setting=None) -> Dict[str, Any]:
    cfg = load_config(get_setting, set_setting)
    pol = path_policy.public_policy(get_setting)
    st = status()
    return {
        **cfg,
        "policy": pol,
        "cli": st,
        "gateway_path": "/v1/messages",
    }


def apply_config_update(get_setting, set_setting, payload: dict) -> Dict[str, Any]:
    payload = payload or {}
    if "enabled" in payload:
        _s(set_setting, "enabled", "1" if payload.get("enabled") else "0")
    if "agent_id" in payload:
        aid = payload.get("agent_id")
        _s(set_setting, "agent_id", "" if aid in (None, "") else str(int(aid)))
    if "timeout_s" in payload:
        try:
            _s(set_setting, "timeout_s", str(int(payload.get("timeout_s"))))
        except Exception:
            pass
    if "external_root" in payload and payload.get("external_root"):
        _s(set_setting, "external_root", str(payload.get("external_root")).strip())
    if "default_mode" in payload:
        mode = str(payload.get("default_mode") or "external").strip().lower()
        if mode in ("external", "self_extend"):
            _s(set_setting, "default_mode", mode)
    if "enable_thinking" in payload:
        _s(set_setting, "enable_thinking", "1" if payload.get("enable_thinking") else "0")
    if payload.get("rotate_token"):
        _s(set_setting, "gateway_token", secrets.token_urlsafe(32))
    ensure_gateway_token(get_setting, set_setting)
    return public_config(get_setting, set_setting)


def find_claude_binary() -> Optional[str]:
    env = (os.environ.get("CLAUDE_CODE_BIN") or "").strip()
    if env and Path(env).exists():
        return env
    which = shutil.which("claude")
    if which:
        return which
    # common npm global locations
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "claude",
        home / ".npm-global" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def status() -> Dict[str, Any]:
    binary = find_claude_binary()
    out: Dict[str, Any] = {"installed": bool(binary), "binary": binary or "", "version": ""}
    if not binary:
        return out
    try:
        r = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        ver = (r.stdout or r.stderr or "").strip().splitlines()
        out["version"] = ver[0] if ver else ""
    except Exception as e:
        out["version_error"] = str(e)
    return out


def gateway_env(base_url: str, token: str) -> Dict[str, str]:
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = (base_url or "").rstrip("/")
    env["ANTHROPIC_AUTH_TOKEN"] = token or ""
    env["ANTHROPIC_API_KEY"] = ""
    # avoid claude.ai oauth taking precedence
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return env


def _write_self_extend_rules(cwd: Path) -> Path:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    rules = path_policy.self_extend_rules_markdown()
    # Prefer project CLAUDE.md in cwd if under EV and allow-listed path
    target = cwd / "CLAUDE.ev-sandbox.md"
    try:
        target.write_text(rules, encoding="utf-8")
    except Exception:
        target = TMP_DIR / "CLAUDE.ev-sandbox.md"
        target.write_text(rules, encoding="utf-8")
    return target


def _rel_files(root: Path) -> Set[str]:
    out: Set[str] = set()
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.name in _SKIP_NAMES:
            continue
        try:
            out.add(str(p.relative_to(root)).replace("\\", "/"))
        except Exception:
            continue
    return out


def _file_manifest(root: Path) -> Dict[str, str]:
    """Content evidence used for completion; model prose and mtimes are not proof."""
    manifest: Dict[str, str] = {}
    for rel in _rel_files(root):
        path = root / rel
        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            manifest[rel] = digest.hexdigest()
        except Exception:
            continue
    return manifest


def list_artifacts(cwd: str, *, since_mtime: float = 0.0, limit: int = 40) -> List[Dict[str, Any]]:
    root = Path(cwd or "").expanduser().resolve()
    items: List[Dict[str, Any]] = []
    if not root.is_dir():
        return items
    for rel in sorted(_rel_files(root)):
        path = root / rel
        try:
            st = path.stat()
        except Exception:
            continue
        if since_mtime and st.st_mtime + 0.01 < since_mtime:
            continue
        items.append({
            "path": rel,
            "bytes": int(st.st_size),
            "mtime": st.st_mtime,
            "is_html": rel.lower().endswith((".html", ".htm")),
        })
    items.sort(key=lambda x: (-float(x.get("mtime") or 0), x.get("path") or ""))
    return items[:limit]


def pick_preview_path(artifacts: List[Dict[str, Any]]) -> str:
    htmls = [a["path"] for a in artifacts if a.get("is_html")]
    for preferred in ("index.html", "index.htm"):
        if preferred in htmls:
            return preferred
    return htmls[0] if htmls else ""


STUDIO_WINDOW_ID = "coding-studio"
SITE_WINDOW_ID = "site-preview"


def progress_panel(
    *,
    status: str,
    detail: str = "",
    files: Optional[List[str]] = None,
    percent: Optional[int] = None,
    done: bool = False,
    ok: Optional[bool] = None,
    preview_path: str = "",
    preview_url: str = "",
    cwd: str = "",
    summary: str = "",
    phase: str = "",
    log: Optional[List[str]] = None,
    plan_steps: Optional[List[str]] = None,
    risks: Optional[List[Any]] = None,
    preview_locked: bool = False,
) -> Dict[str, Any]:
    """Muse 终端内 Coding Studio 浮窗（固定 window_id，就地更新）。"""
    title = "Coding Studio"
    if done:
        title = "Coding Studio · 完成" if ok else "Coding Studio · 未完成"
    elif phase in ("clarifying", "planning", "awaiting_confirm"):
        title = "Coding Studio · 计划"
    return {
        "panel": "coding",
        "window_id": STUDIO_WINDOW_ID,
        "title": title,
        "width": 480,
        "height": 560 if (preview_url or plan_steps or log) else 420,
        "position": "right-top",
        "data": {
            "kind": "coding",
            "phase": phase or ("done" if done else "writing"),
            "status": status,
            "detail": detail,
            "files": list(files or []),
            "percent": percent,
            "done": bool(done),
            "ok": ok,
            "preview_path": preview_path or "",
            "preview_url": preview_url or "",
            "preview_locked": bool(preview_locked),
            "cwd": cwd or "",
            "summary": (summary or "")[:1200],
            "log": list(log or [])[-40:],
            "plan_steps": list(plan_steps or []),
            "risks": list(risks or []),
        },
    }


def site_panel(*, preview_url: str, title: str = "网站预览", path: str = "") -> Dict[str, Any]:
    return {
        "panel": "site",
        "window_id": SITE_WINDOW_ID,
        "title": title,
        "url": preview_url,
        "width": 720,
        "height": 560,
        "position": "right",
        "data": {
            "kind": "site",
            "url": preview_url,
            "path": path or "",
        },
    }


def preview_url_for(rel_path: str, base_url: str = "http://127.0.0.1:8002") -> str:
    rel = (rel_path or "").lstrip("/")
    if not rel:
        return ""
    return "%s/api/skills/claude-code/preview/%s" % (base_url.rstrip("/"), urllib.parse.quote(rel, safe="/"))


def _buf_push(run_id: str, event: Dict[str, Any]) -> None:
    with _RUN_LOCK:
        buf = _EVENT_BUFFERS.setdefault(run_id, [])
        buf.append(event)
        if len(buf) > _EVENT_LIMIT:
            del buf[: len(buf) - _EVENT_LIMIT]


def get_events(run_id: str, *, after: int = 0) -> List[Dict[str, Any]]:
    with _RUN_LOCK:
        buf = list(_EVENT_BUFFERS.get(run_id) or [])
    if after <= 0:
        return buf
    return [e for e in buf if int(e.get("seq") or 0) > after]


def active_run_ids() -> List[str]:
    with _RUN_LOCK:
        return [rid for rid, meta in _ACTIVE_RUNS.items() if meta.get("alive")]


def get_active_run() -> Optional[Dict[str, Any]]:
    with _RUN_LOCK:
        for rid, meta in _ACTIVE_RUNS.items():
            if meta.get("alive"):
                return dict(meta)
    return None


def cancel_run(run_id: str = "") -> bool:
    with _RUN_LOCK:
        targets = []
        if run_id and run_id in _ACTIVE_RUNS:
            targets = [run_id]
        else:
            targets = [rid for rid, m in _ACTIVE_RUNS.items() if m.get("alive")]
        killed = False
        for rid in targets:
            meta = _ACTIVE_RUNS.get(rid) or {}
            proc = meta.get("proc")
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    killed = True
                except Exception:
                    try:
                        proc.kill()
                        killed = True
                    except Exception:
                        pass
            meta["alive"] = False
            meta["cancelled"] = True
        turn_trace.record_runtime("claude.cancel", {
            "requested_run_id": run_id, "target_run_ids": targets, "signal_sent": killed,
        }, category="claude", severity="warning")
        return killed


def _parse_stream_line(line: str) -> Optional[Dict[str, Any]]:
    line = (line or "").strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except Exception:
        return {"type": "raw", "text": line[:500]}


def _summarize_stream_event(obj: Dict[str, Any]) -> Dict[str, Any]:
    """解析 Claude Code stream-json，抽出可读活动行（忽略无信息的 stream_event 噪音）。"""
    t = str(obj.get("type") or "")
    text = ""
    tool = ""
    path = ""
    session_id = obj.get("session_id")
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    if not session_id and msg:
        session_id = msg.get("session_id")

    if t == "assistant" and msg:
        parts = msg.get("content") or []
        if isinstance(parts, list):
            bits = []
            for p in parts:
                if not isinstance(p, dict):
                    continue
                pt = str(p.get("type") or "")
                if pt == "text":
                    bits.append(str(p.get("text") or "")[:300])
                elif pt == "tool_use":
                    tool = str(p.get("name") or "tool")
                    inp = p.get("input") if isinstance(p.get("input"), dict) else {}
                    path = str(inp.get("file_path") or inp.get("path") or inp.get("file") or "")[:240]
                    bits.append(tool + ((" → " + path) if path else ""))
            text = " · ".join(b for b in bits if b)[:400]
    elif t == "stream_event":
        ev = obj.get("event") if isinstance(obj.get("event"), dict) else {}
        et = str(ev.get("type") or "")
        delta = ev.get("delta") if isinstance(ev.get("delta"), dict) else {}
        if et == "content_block_delta" and str(delta.get("type") or "") == "text_delta":
            text = str(delta.get("text") or "")[:200]
        elif et == "content_block_start":
            block = ev.get("content_block") if isinstance(ev.get("content_block"), dict) else {}
            if str(block.get("type") or "") == "tool_use":
                tool = str(block.get("name") or "tool")
                text = "调用 " + tool
        # 其它 stream_event 丢弃，避免活动流全是 stream_event
    elif t == "result":
        text = str(obj.get("result") or obj.get("subtype") or "result")[:400]
        if obj.get("is_error"):
            text = "错误：" + text
    elif t == "system":
        sub = str(obj.get("subtype") or "")
        if sub == "init":
            text = "会话已启动"
        else:
            text = ""
    elif t in ("text", "content_block_delta"):
        delta = obj.get("delta") or obj
        if isinstance(delta, dict):
            text = str(delta.get("text") or "")[:400]
        else:
            text = str(delta)[:400]
    elif "tool" in t.lower() or obj.get("name"):
        tool = str(obj.get("name") or obj.get("tool") or t)
        inp = obj.get("input") or obj.get("tool_input") or {}
        if isinstance(inp, dict):
            path = str(inp.get("file_path") or inp.get("path") or inp.get("file") or "")[:240]
        text = tool + ((" → " + path) if path else "")
    else:
        text = str(obj.get("text") or "")[:240]

    return {
        "type": t or "message",
        "text": text,
        "tool": tool,
        "path": path,
        "session_id": session_id,
        "raw_type": t or "message",
    }


def run_task(
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
    resume_session_id: str = "",
    run_id: str = "",
) -> Dict[str, Any]:
    """在策略允许的 cwd 下启动 Claude Code（stream-json）。"""
    cfg = load_config(get_setting, set_setting)
    if not cfg.get("enabled"):
        turn_trace.record_runtime("claude.rejected", {
            "reason": "disabled", "run_id": run_id,
        }, category="claude", severity="warning")
        return {"ok": False, "error": "Claude Code 技能已关闭"}

    mode = (mode or cfg.get("default_mode") or "external").strip().lower()
    ok, err, resolved = path_policy.validate_cwd(cwd or "", mode, get_setting)
    if not ok:
        turn_trace.record_runtime("claude.rejected", {
            "reason": "cwd_policy", "run_id": run_id, "mode": mode,
            "cwd": str(resolved), "error": err,
        }, category="claude", severity="warning")
        return {"ok": False, "error": err, "mode": mode, "cwd": str(resolved)}

    prompt = (task or "").strip()
    if not prompt:
        turn_trace.record_runtime("claude.rejected", {
            "reason": "empty_task", "run_id": run_id, "mode": mode, "cwd": str(resolved),
        }, category="claude", severity="warning")
        return {"ok": False, "error": "空任务", "mode": mode, "cwd": str(resolved)}

    binary = find_claude_binary()
    if not binary:
        turn_trace.record_runtime("claude.rejected", {
            "reason": "binary_missing", "run_id": run_id, "mode": mode, "cwd": str(resolved),
        }, category="claude", severity="warning")
        return {
            "ok": False,
            "error": "未找到 claude CLI，请先安装 Claude Code",
            "mode": mode,
            "cwd": str(resolved),
        }

    # 单活跃 run：若已有*其他*存活任务则拒绝（排队由上层处理）
    existing = get_active_run()
    if existing and existing.get("alive") and existing.get("run_id") != (run_id or ""):
        if not existing.get("starting") or existing.get("proc"):
            turn_trace.record_runtime("claude.rejected", {
                "reason": "busy", "run_id": run_id,
                "active_run_id": existing.get("run_id"), "cwd": str(resolved),
            }, category="claude", severity="warning")
            return {
                "ok": False,
                "error": "已有写码任务进行中",
                "busy": True,
                "active_run_id": existing.get("run_id"),
                "mode": mode,
                "cwd": str(resolved),
            }

    token = ensure_gateway_token(get_setting, set_setting)
    env = gateway_env(base_url, token)

    if mode == "self_extend":
        rules_path = _write_self_extend_rules(resolved)
        prompt = (
            "【强制】遵守写入沙箱：只改 EV 扩展区，禁止改 app.py / voice / camera / control_plane / muse.js / server。\n"
            "规则文件：%s\n\n任务：%s"
        ) % (rules_path, prompt)

    to = int(timeout_s or cfg.get("timeout_s") or 300)
    rid = (run_id or ("run_%s" % int(time.time() * 1000))).strip()
    cmd = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--dangerously-skip-permissions",
        "--permission-mode",
        "bypassPermissions",
    ]
    if resume_session_id:
        cmd.extend(["--resume", str(resume_session_id)])

    started_mtime = time.time()
    before_manifest = _file_manifest(resolved)
    t0 = time.perf_counter()
    seq = 0
    session_id = resume_session_id or ""
    text_bits: List[str] = []
    tool_receipts: List[Dict[str, str]] = []

    def _emit_progress(status: str, detail: str = "", percent: Optional[int] = None, files=None):
        if not on_progress:
            return
        try:
            on_progress(progress_panel(
                status=status,
                detail=detail,
                files=files or [],
                percent=percent,
                cwd=str(resolved),
            ))
        except Exception:
            pass

    def _emit_event(payload: Dict[str, Any]):
        nonlocal seq
        seq += 1
        ev = dict(payload)
        ev["seq"] = seq
        ev["run_id"] = rid
        ev["ts"] = time.time()
        _buf_push(rid, ev)
        turn_trace.record_runtime("claude.stream", {
            "run_id": rid, "stream_seq": seq, "kind": ev.get("kind"),
            "type": ev.get("type"), "tool": ev.get("tool"),
            "path": ev.get("path"), "text": str(ev.get("text") or "")[:500],
            "ok": ev.get("ok"),
        }, category="claude")
        if on_event:
            try:
                on_event(ev)
            except Exception:
                pass

    _emit_progress("启动中", "正在唤起 Claude Code…", percent=5)
    _emit_event({"kind": "status", "text": "启动 Claude Code…"})

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(resolved),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        turn_trace.record_runtime("claude.start_failed", {
            "run_id": rid, "cwd": str(resolved), "mode": mode, "error": str(exc)[:1000],
        }, category="claude", severity="error")
        return {
            "ok": False,
            "error": "启动失败：%s" % exc,
            "cwd": str(resolved),
            "mode": mode,
            "run_id": rid,
        }

    with _RUN_LOCK:
        _ACTIVE_RUNS[rid] = {
            "run_id": rid,
            "pid": proc.pid,
            "proc": proc,
            "alive": True,
            "cwd": str(resolved),
            "started_at": time.time(),
            "cancelled": False,
        }
        _EVENT_BUFFERS.setdefault(rid, [])
    turn_trace.record_runtime("claude.process_started", {
        "run_id": rid, "pid": proc.pid, "cwd": str(resolved), "mode": mode,
        "timeout_s": to, "resumed": bool(resume_session_id),
    }, category="claude")

    stderr_box: Dict[str, str] = {"text": ""}

    def _drain_stderr():
        try:
            if proc.stderr:
                stderr_box["text"] = proc.stderr.read() or ""
        except Exception:
            pass

    err_th = threading.Thread(target=_drain_stderr, daemon=True)
    err_th.start()

    seen_new: List[str] = []
    timed_out = False
    try:
        assert proc.stdout is not None
        while True:
            if time.perf_counter() - t0 > to:
                timed_out = True
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            obj = _parse_stream_line(line)
            if not obj:
                continue
            summary = _summarize_stream_event(obj)
            if summary.get("session_id"):
                session_id = str(summary["session_id"])
            line_text = (summary.get("text") or "").strip()
            if summary.get("tool"):
                receipt = {
                    "tool": str(summary.get("tool") or "")[:120],
                    "path": str(summary.get("path") or "")[:240],
                }
                if receipt not in tool_receipts:
                    tool_receipts.append(receipt)
            if line_text:
                text_bits.append(line_text)
                _emit_event({"kind": "stream", **summary})
            if summary.get("path") and summary["path"] not in seen_new:
                seen_new.append(summary["path"])
            # 文件 mtime 扫描
            touched = [a["path"] for a in list_artifacts(str(resolved), since_mtime=started_mtime, limit=30)]
            for name in touched:
                if name not in seen_new:
                    seen_new.append(name)
            # 无有效文本时不刷进度（避免一堆空 stream_event）
            if not line_text and not summary.get("path"):
                continue
            pct = min(88, 12 + len(seen_new) * 6)
            _emit_progress(
                summary.get("tool") and "工具调用" or "编写中",
                (line_text or "进行中")[:120],
                percent=pct,
                files=seen_new[:12],
            )
        proc.wait(timeout=5)
    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        with _RUN_LOCK:
            if rid in _ACTIVE_RUNS:
                _ACTIVE_RUNS[rid]["alive"] = False
        failed_result = {
            "ok": False,
            "error": "运行异常：%s" % exc,
            "cwd": str(resolved),
            "mode": mode,
            "run_id": rid,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }
        turn_trace.record_runtime("claude.completed", failed_result,
                                  category="claude", severity="error")
        return failed_result

    err_th.join(timeout=2)
    with _RUN_LOCK:
        meta = _ACTIVE_RUNS.get(rid) or {}
        cancelled = bool(meta.get("cancelled"))
        if rid in _ACTIVE_RUNS:
            _ACTIVE_RUNS[rid]["alive"] = False

    stderr = (stderr_box.get("text") or "").strip()
    after_manifest = _file_manifest(resolved)
    changed_paths = sorted(
        rel for rel in set(before_manifest) | set(after_manifest)
        if before_manifest.get(rel) != after_manifest.get(rel)
    )
    observed = {item["path"]: item for item in list_artifacts(str(resolved), limit=200)}
    artifacts = []
    for rel in changed_paths[:40]:
        item = observed.get(rel)
        artifacts.append(item or {
            "path": rel, "bytes": 0, "mtime": time.time(),
            "is_html": rel.lower().endswith((".html", ".htm")), "deleted": rel not in after_manifest,
        })
    preview = pick_preview_path([item for item in artifacts if not item.get("deleted")])
    public_preview = preview
    if preview:
        try:
            public_preview = str((resolved / preview).relative_to(Path(cfg["external_root"]).expanduser().resolve())).replace("\\", "/")
        except Exception:
            public_preview = preview
    preview_url = preview_url_for(public_preview, base_url) if preview else ""
    summary = "\n".join(text_bits).strip() or stderr or "(无输出)"
    if len(summary) > 12000:
        summary = summary[:12000] + "\n…(截断)"

    if timed_out:
        success = False
        error = "Claude Code 超时（%ss）" % to
    elif cancelled:
        success = False
        error = "已取消"
    else:
        code = proc.returncode if proc else -1
        success = code == 0
        error = "" if success else (stderr[:500] or "claude 退出码 %s" % code)

    _emit_event({"kind": "done", "ok": success, "text": error or "完成", "session_id": session_id})
    panel = progress_panel(
        status="完成" if success else "失败",
        detail=("已生成可预览页面" if preview and success else (error or summary[:180])),
        files=[a["path"] for a in artifacts][:16],
        percent=100,
        done=True,
        ok=success,
        preview_path=preview,
        preview_url=preview_url,
        cwd=str(resolved),
        summary=summary,
    )
    if on_progress:
        try:
            on_progress(panel)
        except Exception:
            pass
    result = {
        "ok": success,
        "exit_code": proc.returncode if proc else -1,
        "summary": summary,
        "stderr": stderr[:4000],
        "cwd": str(resolved),
        "mode": mode,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        "error": error,
        "artifacts": artifacts,
        "preview_path": preview,
        "preview_url": preview_url,
        "panel": panel,
        "site_panel": site_panel(preview_url=preview_url, path=preview) if preview_url else None,
        "run_id": rid,
        "session_id": session_id or "",
        "cancelled": cancelled,
        "verified_changes": bool(changed_paths),
        "task_outcome": (
            "failed" if not success else ("completed" if changed_paths else "needs_input")
        ),
        "change_evidence": {"changed_paths": changed_paths[:80], "method": "sha256_before_after"},
        "tool_receipts": tool_receipts[:40],
    }
    turn_trace.record_runtime("claude.completed", {
        "run_id": rid, "ok": success, "exit_code": result["exit_code"],
        "elapsed_ms": result["elapsed_ms"], "cancelled": cancelled,
        "timed_out": timed_out, "verified_changes": result["verified_changes"],
        "task_outcome": result["task_outcome"],
        "changed_paths": changed_paths[:80], "preview_url": preview_url,
        "error": error,
    }, category="claude", severity="info" if success else "error")
    return result


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
    on_done: Optional[Callable[[Dict[str, Any]], None]] = None,
    resume_session_id: str = "",
) -> Dict[str, Any]:
    """异步启动；立即返回 run_id。"""
    rid = "run_%s" % int(time.time() * 1000)
    box: Dict[str, Any] = {"started": True, "run_id": rid}
    with _RUN_LOCK:
        _ACTIVE_RUNS[rid] = {
            "run_id": rid,
            "pid": None,
            "proc": None,
            "alive": True,
            "cwd": cwd or "",
            "started_at": time.time(),
            "cancelled": False,
            "starting": True,
        }
        _EVENT_BUFFERS.setdefault(rid, [])
    turn_trace.record_runtime("claude.submitted", {
        "run_id": rid, "cwd": cwd, "mode": mode,
        "task_preview": str(task or "")[:500], "resumed": bool(resume_session_id),
    }, category="claude")

    def _worker():
        result = run_task(
            task,
            get_setting=get_setting,
            set_setting=set_setting,
            cwd=cwd,
            mode=mode,
            base_url=base_url,
            timeout_s=timeout_s,
            on_progress=on_progress,
            on_event=on_event,
            resume_session_id=resume_session_id,
            run_id=rid,
        )
        box["result"] = result
        with _RUN_LOCK:
            if rid in _ACTIVE_RUNS:
                _ACTIVE_RUNS[rid]["alive"] = False
                _ACTIVE_RUNS[rid]["starting"] = False
        if on_done:
            try:
                on_done(result)
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()
    return box


def tool_definition(*, slim=False) -> dict:
    """Claude Code 工具定义。slim=True 返回精简版（文本模式低频使用）。"""
    if slim:
        return {
            "type": "function",
            "function": {
                "name": "claude_code_run",
                "description": (
                    "调用本机 Claude Code 实际动手（读写文件/改代码/跑命令/搭项目）。"
                    "mode=external 仅 MuseWork；mode=self_extend 限 EV 扩展区。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "交给 Claude Code 的自然语言任务"},
                        "cwd": {"type": "string", "description": "工作目录；external 默认 MuseWork，self_extend 默认 EV 根"},
                        "mode": {
                            "type": "string",
                            "enum": ["external", "self_extend"],
                            "description": "external 外部项目；self_extend EV 扩展区",
                        },
                    },
                    "required": ["task"],
                },
            },
        }
    return {
        "type": "function",
        "function": {
            "name": "claude_code_run",
            "description": (
                "调用本机 Claude Code 实际动手：读写文件、改代码、跑命令、排查报错、搭小项目。"
                "用户要你「帮我做/修/写/改/装/跑」工程类事时必须调用，不要只口头指导。"
                "mode=external：仅 ~/Documents/MuseWork；mode=self_extend：可在 EV 扩展区加功能，禁止改核心运行面。"
                "不要用于闲聊、纯问答、网页搜索、看摄像头。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "要交给 Claude Code 的自然语言任务",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "工作目录绝对路径；external 默认 MuseWork，self_extend 默认 EV 根",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["external", "self_extend"],
                        "description": "external=外部项目；self_extend=给 EV 加功能（扩展区）",
                    },
                },
                "required": ["task"],
            },
        },
    }


def assert_path_write(path: str, mode: str, get_setting) -> Tuple[bool, str]:
    return path_policy.check_write_path(path, mode, get_setting)
