# -*- coding: utf-8 -*-
"""开写前快照 + 回滚。"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.paths import TMP_DIR

_SKIP_DIRS = {".git", ".ev", "__pycache__", "node_modules", ".venv", "venv"}
_MAX_KEEP = 5


def _snap_root(cwd: Path) -> Path:
    root = Path(cwd).expanduser().resolve() / ".ev" / "snapshots"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _is_git(cwd: Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=8,
        )
        return r.returncode == 0 and "true" in (r.stdout or "").lower()
    except Exception:
        return False


def create_checkpoint(cwd: str, *, label: str = "") -> Dict[str, Any]:
    root = Path(cwd or "").expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "error": "cwd 无效"}
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    meta: Dict[str, Any] = {
        "id": run_id,
        "cwd": str(root),
        "label": (label or "")[:120],
        "at": time.time(),
        "method": "",
        "ok": False,
    }
    if _is_git(root):
        try:
            subprocess.run(["git", "add", "-A"], cwd=str(root), capture_output=True, timeout=60)
            msg = "ev-checkpoint %s %s" % (run_id, label or "")
            r = subprocess.run(
                ["git", "commit", "-m", msg, "--allow-empty"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=60,
            )
            # 即使 nothing to commit 也记 head
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=8,
            )
            meta["method"] = "git"
            meta["git_head"] = (head.stdout or "").strip()
            meta["ok"] = True
            meta["commit_note"] = ((r.stdout or "") + (r.stderr or ""))[:300]
            _prune(root)
            return meta
        except Exception as exc:
            meta["error"] = str(exc)

    # 文件拷贝兜底
    dest = _snap_root(root) / run_id
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel_parts = p.relative_to(root).parts
            if any(part in _SKIP_DIRS for part in rel_parts):
                continue
            if rel_parts and rel_parts[0] == ".ev":
                continue
            rel = p.relative_to(root)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                if p.stat().st_size > 2_000_000:
                    continue
                shutil.copy2(p, out)
                copied += 1
            except Exception:
                continue
        meta["method"] = "copy"
        meta["path"] = str(dest)
        meta["files"] = copied
        meta["ok"] = True
        _prune(root)
        return meta
    except Exception as exc:
        meta["error"] = str(exc)
        return meta


def revert_checkpoint(meta: Dict[str, Any]) -> Dict[str, Any]:
    if not meta or not meta.get("ok"):
        return {"ok": False, "error": "无有效 checkpoint"}
    cwd = Path(str(meta.get("cwd") or "")).expanduser()
    if not cwd.is_dir():
        return {"ok": False, "error": "cwd 无效"}
    method = meta.get("method")
    if method == "git" and meta.get("git_head"):
        try:
            r = subprocess.run(
                ["git", "reset", "--hard", str(meta["git_head"])],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if r.returncode != 0:
                return {"ok": False, "error": (r.stderr or r.stdout or "git reset 失败")[:400]}
            return {"ok": True, "method": "git", "head": meta["git_head"]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    path = Path(str(meta.get("path") or ""))
    if method == "copy" and path.is_dir():
        try:
            for p in path.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(path)
                out = cwd / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, out)
            return {"ok": True, "method": "copy", "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "无法回滚：缺少快照数据"}


def _prune(cwd: Path) -> None:
    root = _snap_root(cwd)
    dirs = sorted([d for d in root.iterdir() if d.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dirs[_MAX_KEEP:]:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
    # 索引
    try:
        idx = [{"id": d.name, "mtime": d.stat().st_mtime} for d in dirs[:_MAX_KEEP]]
        (root / "index.json").write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
