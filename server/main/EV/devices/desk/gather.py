# -*- coding: utf-8 -*-
"""安全只读 gather：为 desk_compose 提供真数据。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from coding import path_policy

_SECRET_NAMES = {".env", ".ev-claude-env", "credentials.json", "id_rsa", "id_ed25519"}
_TEXT_EXT = {".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".html", ".css", ".js", ".ts", ".py", ".svg"}


def _safe_root(cwd: str, get_setting) -> Path:
    root = Path(cwd or path_policy.default_external_root(get_setting)).expanduser().resolve()
    return root


def _under(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def gather_files(cwd: str, get_setting, *, glob_pat: str = "**/*.{json,yml,yaml,toml,md}", limit: int = 40) -> Dict[str, Any]:
    root = _safe_root(cwd, get_setting)
    items = []
    # simple scan without fancy glob
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name in _SECRET_NAMES:
            continue
        if any(part in {".git", "node_modules", ".venv", "venv", "__pycache__", ".ev"} for part in p.parts):
            continue
        if p.suffix.lower() not in _TEXT_EXT and p.name not in ("Dockerfile", "Makefile"):
            continue
        try:
            if p.stat().st_size > 200_000:
                continue
            rel = str(p.relative_to(root)).replace("\\", "/")
            text = p.read_text(encoding="utf-8", errors="replace")
            preview = text[:4000]
            entry: Dict[str, Any] = {"path": rel, "bytes": p.stat().st_size, "preview": preview}
            if p.suffix.lower() == ".json":
                try:
                    entry["json"] = json.loads(text)
                except Exception:
                    pass
            items.append(entry)
            if len(items) >= limit:
                break
        except Exception:
            continue
    return {"ok": True, "cwd": str(root), "files": items, "count": len(items)}


def gather_tree(cwd: str, get_setting, *, depth: int = 3, limit: int = 120) -> Dict[str, Any]:
    root = _safe_root(cwd, get_setting)
    rows = []

    def walk(d: Path, level: int):
        if level > depth or len(rows) >= limit:
            return
        try:
            children = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except Exception:
            return
        for c in children:
            if c.name.startswith(".") or c.name in {"node_modules", "__pycache__", ".venv"}:
                continue
            rel = str(c.relative_to(root)).replace("\\", "/")
            rows.append({"path": rel, "type": "dir" if c.is_dir() else "file"})
            if c.is_dir():
                walk(c, level + 1)
            if len(rows) >= limit:
                return

    walk(root, 0)
    return {"ok": True, "cwd": str(root), "tree": rows, "count": len(rows)}


def gather_which(tools: Optional[List[str]] = None) -> Dict[str, Any]:
    tools = tools or ["node", "npm", "python3", "python", "git", "uv", "pip", "claude"]
    rows = []
    for name in tools:
        path = shutil.which(name) or ""
        ver = ""
        if path:
            try:
                r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
                ver = ((r.stdout or r.stderr or "").strip().splitlines() or [""])[0][:120]
            except Exception:
                ver = ""
        rows.append({"name": name, "path": path or "（未找到）", "version": ver, "found": bool(path)})
    return {"ok": True, "tools": rows}


def gather_venvs(home: Optional[str] = None) -> Dict[str, Any]:
    home_p = Path(home or Path.home())
    candidates: List[Path] = []
    for base in [
        home_p / ".virtualenvs",
        home_p / "venvs",
        home_p / ".pyenv" / "versions",
        home_p / "Documents" / "MuseWork",
    ]:
        if base.is_dir():
            candidates.append(base)
    rows = []
    seen = set()
    # conda
    try:
        r = subprocess.run(["conda", "env", "list", "--json"], capture_output=True, text=True, timeout=12)
        if r.returncode == 0:
            data = json.loads(r.stdout or "{}")
            for ep in data.get("envs") or []:
                p = Path(ep)
                if str(p) in seen:
                    continue
                seen.add(str(p))
                rows.append(_venv_row(p, source="conda"))
    except Exception:
        pass
    for base in candidates:
        if base.name in {"MuseWork"} or "MuseWork" in str(base):
            for v in base.rglob(".venv"):
                if v.is_dir() and str(v) not in seen:
                    seen.add(str(v))
                    rows.append(_venv_row(v, source="project"))
            continue
        try:
            for child in base.iterdir():
                if child.is_dir() and str(child) not in seen:
                    # pyvenv.cfg or bin/python
                    if (child / "pyvenv.cfg").exists() or (child / "bin" / "python").exists():
                        seen.add(str(child))
                        rows.append(_venv_row(child, source=base.name))
        except Exception:
            continue
    return {"ok": True, "venvs": rows, "count": len(rows)}


def _venv_row(path: Path, source: str = "") -> Dict[str, Any]:
    cfg = {}
    pyvenv = path / "pyvenv.cfg"
    if pyvenv.exists():
        try:
            for line in pyvenv.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
        except Exception:
            pass
    py = path / "bin" / "python"
    if not py.exists():
        py = path / "Scripts" / "python.exe"
    version = cfg.get("version") or cfg.get("version_info") or ""
    if not version and py.exists():
        try:
            r = subprocess.run([str(py), "--version"], capture_output=True, text=True, timeout=5)
            version = (r.stdout or r.stderr or "").strip()
        except Exception:
            pass
    return {
        "name": path.name,
        "path": str(path),
        "python": version,
        "source": source,
        "active": False,
        "home": cfg.get("home") or "",
    }


def gather_json_file(cwd: str, get_setting, rel: str) -> Dict[str, Any]:
    root = _safe_root(cwd, get_setting)
    path = (root / rel).resolve()
    if not _under(root, path) or not path.is_file():
        return {"ok": False, "error": "文件不可读"}
    if path.name in _SECRET_NAMES:
        return {"ok": False, "error": "敏感文件已跳过"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"ok": True, "path": rel, "json": data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_plan(plan: List[Dict[str, Any]], cwd: str, get_setting) -> Dict[str, Any]:
    """执行 gather 计划，合并 facts。"""
    facts: Dict[str, Any] = {"ok": True, "parts": []}
    for step in (plan or [])[:12]:
        if not isinstance(step, dict):
            continue
        kind = str(step.get("kind") or step.get("provider") or "").strip()
        part: Dict[str, Any]
        if kind in ("gather.files", "files"):
            part = gather_files(cwd, get_setting, limit=int(step.get("limit") or 40))
        elif kind in ("gather.tree", "tree"):
            part = gather_tree(cwd, get_setting, depth=int(step.get("depth") or 3))
        elif kind in ("gather.which", "which"):
            part = gather_which(step.get("tools"))
        elif kind in ("gather.venvs", "local.venvs", "venvs"):
            part = gather_venvs()
        elif kind in ("gather.json", "json"):
            part = gather_json_file(cwd, get_setting, str(step.get("path") or "package.json"))
        else:
            part = {"ok": False, "error": "未知 gather: %s" % kind}
        part["kind"] = kind
        facts["parts"].append(part)
    # flat helpers
    for part in facts["parts"]:
        if part.get("venvs"):
            facts["venvs"] = part["venvs"]
        if part.get("files"):
            facts.setdefault("files", []).extend(part["files"])
        if part.get("tools"):
            facts["tools"] = part["tools"]
        if part.get("tree"):
            facts["tree"] = part["tree"]
        if "json" in part and part.get("path"):
            facts.setdefault("json_files", {})[part["path"]] = part.get("json")
    facts["count"] = sum(
        len(part.get("venvs") or part.get("files") or part.get("tools") or part.get("tree") or [])
        for part in facts["parts"]
    )
    return facts


def facts_to_markdown(facts: Dict[str, Any]) -> str:
    lines = ["# Gather facts", ""]
    if facts.get("venvs"):
        lines.append("## Virtual envs")
        for v in facts["venvs"][:40]:
            lines.append("- **%s** `%s` — %s" % (v.get("name"), v.get("path"), v.get("python")))
        lines.append("")
    if facts.get("tools"):
        lines.append("## Tools")
        for t in facts["tools"]:
            lines.append("- %s: %s %s" % (t.get("name"), t.get("path"), t.get("version") or ""))
        lines.append("")
    if facts.get("files"):
        lines.append("## Files")
        for f in facts["files"][:40]:
            lines.append("### `%s`" % f.get("path"))
            if f.get("json") and isinstance(f["json"], dict) and "dependencies" in f["json"]:
                deps = f["json"].get("dependencies") or {}
                lines.append("dependencies: " + ", ".join("%s@%s" % (k, deps[k]) for k in list(deps)[:30]))
            else:
                lines.append("```\n%s\n```" % str(f.get("preview") or "")[:1500])
            lines.append("")
    if facts.get("tree"):
        lines.append("## Tree")
        for row in facts["tree"][:80]:
            lines.append("- %s (%s)" % (row.get("path"), row.get("type")))
    if len(lines) <= 2:
        lines.append("（未采集到数据）")
    return "\n".join(lines)
