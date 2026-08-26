# -*- coding: utf-8 -*-
"""Codex App Server stdio adapter.

Only provider-neutral, user-visible activity leaves this module. Raw reasoning is
never forwarded to EV or the HUD.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from devices.coding.agent_runtime import evidence
from devices.coding.agent_runtime.protocol import event


EventCb = Optional[Callable[[Dict[str, Any]], None]]
_DESTRUCTIVE = (
    "rm -rf", "sudo ", "diskutil ", "shutdown", "reboot", "mkfs", "dd if=",
    "git reset --hard", "git clean -f", ":(){:|:&};:",
)


def find_binary() -> str:
    for candidate in (
        os.environ.get("EV_CODEX_BIN", ""),
        os.environ.get("CODEX_BIN", ""),
        shutil.which("codex") or "",
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    ):
        path = str(candidate or "").strip()
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return path
    return ""


def available() -> bool:
    return bool(find_binary())


def _text_input(text: str):
    return [{"type": "text", "text": str(text or ""), "text_elements": []}]


def _preview_url(preview: str, base_url: str) -> str:
    if not preview:
        return ""
    return "%s/api/agent-runtime/preview/%s" % (
        base_url.rstrip("/"), urllib.parse.quote(preview.lstrip("/"), safe="/"),
    )


class CodexAppServerRun:
    provider = "codex"

    def __init__(self, *, run_id: str, cwd: Path, base_url: str, on_event: EventCb = None):
        self.run_id = run_id
        self.cwd = Path(cwd).expanduser().resolve()
        self.base_url = base_url
        self.on_event = on_event
        self.proc: Optional[subprocess.Popen] = None
        self.thread_id = ""
        self.turn_id = ""
        self.cancelled = False
        self.started_at = time.time()
        self._seq = 0
        self._write_lock = threading.RLock()
        self._inbox: queue.Queue = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._stderr: list[str] = []
        self._assistant_text: list[str] = []
        self._last_error = ""

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def _emit(self, item: Dict[str, Any]) -> None:
        self._seq += 1
        message = {**item, "seq": self._seq, "run_id": self.run_id}
        if self.on_event:
            try:
                self.on_event(message)
            except Exception:
                pass

    def _send(self, payload: Dict[str, Any]) -> None:
        with self._write_lock:
            if not self.proc or not self.proc.stdin:
                raise RuntimeError("Agent 连接尚未建立")
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def _request(self, request_id: int, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 20
        deferred = []
        while time.monotonic() < deadline:
            try:
                message = self._inbox.get(timeout=0.25)
            except queue.Empty:
                if self.proc and self.proc.poll() is not None:
                    break
                continue
            if message.get("id") == request_id and ("result" in message or "error" in message):
                for item in deferred:
                    self._inbox.put(item)
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                return dict(message.get("result") or {})
            deferred.append(message)
        for item in deferred:
            self._inbox.put(item)
        raise RuntimeError("Agent App Server 响应超时：%s" % method)

    def _read_stdout(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        for raw in self.proc.stdout:
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(message, dict):
                self._inbox.put(message)

    def _read_stderr(self) -> None:
        if not self.proc or not self.proc.stderr:
            return
        for raw in self.proc.stderr:
            if raw.strip():
                self._stderr.append(raw.strip())
                del self._stderr[:-40]

    def _start_process(self) -> None:
        binary = find_binary()
        if not binary:
            raise RuntimeError("未找到 Codex App Server")
        self.proc = subprocess.Popen(
            [binary, "app-server", "--stdio"],
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    @staticmethod
    def _thread_from(result: Dict[str, Any]) -> str:
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else result
        return str(thread.get("id") or thread.get("threadId") or "")

    @staticmethod
    def _turn_from(result: Dict[str, Any]) -> str:
        turn = result.get("turn") if isinstance(result.get("turn"), dict) else result
        return str(turn.get("id") or turn.get("turnId") or "")

    def _approval(self, message: Dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        command = str(params.get("command") or "")
        dangerous = any(token in command.lower() for token in _DESTRUCTIVE)
        decision = "decline" if dangerous else "acceptForSession"
        self._emit(event(
            "approval.resolved", phase="checking",
            detail="已拒绝高风险命令" if dangerous else "已按工作区策略继续",
            command=command[:240], ok=not dangerous,
        ))
        self._send({"id": message.get("id"), "result": {"decision": decision}})

    def _handle_item(self, method: str, params: Dict[str, Any]) -> None:
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        completed = method.endswith("completed")
        if item_type == "commandExecution":
            command = str(item.get("command") or item.get("aggregatedOutput") or "")
            self._emit(event(
                "check.completed" if completed else "check.started",
                phase="checking", detail=("检查完成" if completed else "运行检查"),
                command=command[:240], ok=item.get("status") == "completed" if completed else None,
            ))
        elif item_type == "fileChange":
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            files = [str(change.get("path") or "") for change in changes if isinstance(change, dict)]
            self._emit(event(
                "file.changed", phase="editing", detail="修改 %d 个文件" % max(1, len(files)),
                path=files[0] if files else "", files=files[:12],
            ))
        elif item_type in {"mcpToolCall", "webSearch", "imageView"}:
            self._emit(event("tool.completed" if completed else "tool.started", phase="reading", detail="获取所需资料"))
        elif item_type == "agentMessage" and completed:
            text = str(item.get("text") or "").strip()
            if text:
                self._assistant_text.append(text)

    def _handle_notification(self, message: Dict[str, Any]) -> Optional[str]:
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            self._approval(message)
        elif method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            self.turn_id = str(turn.get("id") or self.turn_id)
            self._emit(event("turn.started", phase="working", detail="开始处理"))
        elif method == "turn/plan/updated":
            self._emit(event("plan.updated", phase="planning", detail="整理执行步骤"))
        elif method in {"item/started", "item/completed"}:
            self._handle_item(method, params)
        elif method == "turn/diff/updated":
            diff = str(params.get("diff") or "")
            self._emit(event("diff.updated", phase="editing", detail="正在整理改动", added=diff.count("\n+"), removed=diff.count("\n-")))
        elif method == "item/agentMessage/delta":
            delta = str(params.get("delta") or "")
            if delta:
                self._assistant_text.append(delta)
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            return str(turn.get("status") or "completed")
        elif method == "error":
            raw_error = params.get("error")
            if isinstance(raw_error, dict):
                detail = str(raw_error.get("message") or raw_error.get("code") or raw_error)
            else:
                detail = str(params.get("message") or raw_error or "Agent 运行错误")
            retrying = bool(params.get("willRetry")) or "reconnecting" in detail.lower()
            self._emit(event(
                "runtime.retrying" if retrying else "runtime.error",
                phase="working" if retrying else "failed",
                detail="连接暂时不稳定，正在恢复" if retrying else detail,
                ok=None if retrying else False,
            ))
            if not retrying:
                self._last_error = detail
                return "failed"
        return None

    def steer(self, text: str) -> bool:
        if not self.thread_id or not self.turn_id or not str(text or "").strip():
            return False
        try:
            self._send({
                "id": int(time.time() * 1000), "method": "turn/steer",
                "params": {
                    "threadId": self.thread_id, "expectedTurnId": self.turn_id,
                    "input": _text_input(text),
                },
            })
            self._emit(event("turn.steered", phase="working", detail="已接收新的要求"))
            return True
        except Exception:
            return False

    def cancel(self) -> bool:
        self.cancelled = True
        try:
            if self.thread_id and self.turn_id:
                self._send({
                    "id": int(time.time() * 1000), "method": "turn/interrupt",
                    "params": {"threadId": self.thread_id, "turnId": self.turn_id},
                })
                return True
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            return True
        return False

    def run(self, task: str, *, resume_session_id: str = "", timeout_s: int = 900) -> Dict[str, Any]:
        before = evidence.file_manifest(self.cwd)
        started = time.perf_counter()
        terminal_status = "failed"
        error = ""
        try:
            self._start_process()
            self._request(1, "initialize", {
                "clientInfo": {"name": "ev", "title": "EV Work Agent", "version": "1.0"},
            })
            self._send({"method": "initialized"})
            if resume_session_id:
                thread_result = self._request(2, "thread/resume", {
                    "threadId": resume_session_id, "cwd": str(self.cwd),
                    "approvalPolicy": "never", "sandbox": "workspace-write",
                })
            else:
                thread_result = self._request(2, "thread/start", {
                    "cwd": str(self.cwd), "approvalPolicy": "never", "sandbox": "workspace-write",
                    "serviceName": "ev_work_agent",
                    "developerInstructions": (
                        "Work only on the confirmed task. Keep changes scoped. Run relevant checks. "
                        "Finish with a concise factual summary; do not claim unverified work."
                    ),
                })
            self.thread_id = self._thread_from(thread_result)
            if not self.thread_id:
                raise RuntimeError("Agent 没有返回会话 ID")
            turn_result = self._request(3, "turn/start", {
                "threadId": self.thread_id, "cwd": str(self.cwd),
                "approvalPolicy": "never",
                "sandboxPolicy": {
                    "type": "workspaceWrite", "writableRoots": [str(self.cwd)], "networkAccess": True,
                },
                "input": _text_input(task),
            })
            self.turn_id = self._turn_from(turn_result)
            deadline = time.monotonic() + max(30, int(timeout_s or 900))
            while time.monotonic() < deadline:
                if self.cancelled and (not self.proc or self.proc.poll() is not None):
                    terminal_status = "cancelled"
                    break
                try:
                    message = self._inbox.get(timeout=0.25)
                except queue.Empty:
                    if self.proc and self.proc.poll() is not None:
                        error = "Agent App Server 意外退出"
                        break
                    continue
                if "method" not in message:
                    continue
                status = self._handle_notification(message)
                if status:
                    terminal_status = status
                    break
            else:
                error = "工作 Agent 超时"
                self.cancel()
        except Exception as exc:
            error = str(exc)
        finally:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

        after = evidence.file_manifest(self.cwd)
        changed = evidence.changed_paths(before, after)
        artifact_items = evidence.artifacts(self.cwd, changed, after)
        preview = evidence.pick_preview(artifact_items)
        if self.cancelled:
            outcome = "cancelled"
        elif terminal_status in {"failed", "error"} or error:
            outcome = "failed"
        elif changed:
            outcome = "completed"
        else:
            outcome = "needs_input"
        summary = "".join(self._assistant_text).strip()
        if len(summary) > 12000:
            summary = summary[:12000] + "\n…"
        return {
            "ok": outcome in {"completed", "needs_input"},
            "run_id": self.run_id,
            "provider": self.provider,
            "session_id": self.thread_id,
            "turn_id": self.turn_id,
            "cwd": str(self.cwd),
            "summary": summary or ("工作已完成" if changed else "Agent 未产生文件改动"),
            "error": error or self._last_error or ("" if outcome != "failed" else "工作 Agent 未完成任务"),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "verified_changes": bool(changed),
            "task_outcome": outcome,
            "artifacts": artifact_items,
            "preview_path": preview,
            "preview_url": _preview_url(preview, self.base_url),
            "change_evidence": {"changed_paths": changed, "method": "sha256_before_after"},
            "stderr": "\n".join(self._stderr)[-4000:],
        }
