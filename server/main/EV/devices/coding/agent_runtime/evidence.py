# -*- coding: utf-8 -*-
"""Filesystem receipts independent of what a coding provider claims it changed."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List


_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".next", ".turbo",
})
_MAX_FILE_BYTES = 20 * 1024 * 1024


def file_manifest(root: Path) -> Dict[str, str]:
    root = Path(root).expanduser().resolve()
    out: Dict[str, str] = {}
    if not root.is_dir():
        return out
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in _SKIP_DIRS]
        base = Path(current)
        for name in files:
            path = base / name
            try:
                stat = path.stat()
                if not path.is_file() or stat.st_size > _MAX_FILE_BYTES:
                    continue
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                out[str(path.relative_to(root)).replace("\\", "/")] = digest.hexdigest()
            except (OSError, ValueError):
                continue
    return out


def changed_paths(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    return sorted(
        path for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def artifacts(root: Path, paths: Iterable[str], after: Dict[str, str]) -> List[Dict[str, Any]]:
    root = Path(root).expanduser().resolve()
    items: List[Dict[str, Any]] = []
    for rel in list(paths)[:80]:
        path = root / rel
        if rel not in after:
            items.append({"path": rel, "bytes": 0, "mtime": time.time(), "deleted": True})
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({
            "path": rel,
            "bytes": int(stat.st_size),
            "mtime": float(stat.st_mtime),
            "is_html": rel.lower().endswith((".html", ".htm")),
            "deleted": False,
        })
    return items


def pick_preview(items: Iterable[Dict[str, Any]]) -> str:
    paths = [str(item.get("path") or "") for item in items if item.get("is_html") and not item.get("deleted")]
    for preferred in ("index.html", "index.htm"):
        if preferred in paths:
            return preferred
    return paths[0] if paths else ""
