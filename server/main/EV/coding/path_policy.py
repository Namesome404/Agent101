# -*- coding: utf-8 -*-
"""Claude Code / self-extend 三层路径策略：外部可写、EV 扩展可写、核心禁写。"""
from __future__ import annotations

import json
import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common.paths import MAIN_DIR, MUSE_DIR

_POLICY_PATH = Path(__file__).resolve().parent / "path_policy.json"
_POLICY_CACHE: Optional[Dict[str, Any]] = None


def load_policy() -> Dict[str, Any]:
    global _POLICY_CACHE
    if _POLICY_CACHE is not None:
        return _POLICY_CACHE
    try:
        raw = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    _POLICY_CACHE = raw if isinstance(raw, dict) else {}
    return _POLICY_CACHE


def reload_policy() -> Dict[str, Any]:
    global _POLICY_CACHE
    _POLICY_CACHE = None
    return load_policy()


def expand_user_path(p: str) -> Path:
    return Path(os.path.expanduser((p or "").strip())).resolve()


def default_external_root(get_setting=None) -> Path:
    policy = load_policy()
    raw = None
    if get_setting:
        try:
            raw = get_setting("skill.claude_code.external_root")
        except Exception:
            raw = None
    if not raw:
        raw = policy.get("external_root_default") or "~/Documents/MuseWork"
    root = expand_user_path(str(raw))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return root


def muse_root() -> Path:
    return MUSE_DIR.resolve()


def _match_any(rel_posix: str, patterns: List[str]) -> bool:
    rel = (rel_posix or "").lstrip("./")
    for pat in patterns or []:
        p = (pat or "").replace("\\", "/").lstrip("./")
        if not p:
            continue
        if fnmatch(rel, p) or fnmatch(rel, "*/" + p):
            return True
        # also match basename-only patterns against full path segments
        if "/" not in p and fnmatch(Path(rel).name, p):
            return True
    return False


def _match_abs(path: Path, patterns: List[str]) -> bool:
    s = str(path.resolve()).replace("\\", "/")
    for pat in patterns or []:
        p = (pat or "").replace("\\", "/")
        if not p:
            continue
        if fnmatch(s, p) or fnmatch(s, "*/" + p.lstrip("*/")):
            return True
    return False


def is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def validate_cwd(cwd: str, mode: str, get_setting=None) -> Tuple[bool, str, Path]:
    """校验工作目录。返回 (ok, error_or_empty, resolved_cwd)。"""
    mode = (mode or "external").strip().lower()
    if mode not in ("external", "self_extend"):
        return False, "mode 必须是 external 或 self_extend", Path(".")
    try:
        root = expand_user_path(cwd) if cwd else (
            muse_root() if mode == "self_extend" else default_external_root(get_setting)
        )
    except Exception as e:
        return False, "无效路径：%s" % e, Path(".")

    if not root.exists():
        if mode == "external":
            try:
                root.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return False, "无法创建工作区：%s" % e, root
        else:
            return False, "工作目录不存在：%s" % root, root

    if not root.is_dir():
        return False, "工作目录不是文件夹：%s" % root, root

    # reject symlink escape: resolve already done
    if mode == "external":
        ext = default_external_root(get_setting)
        if not is_under(root, ext) and root != ext:
            return False, "external 模式只允许在 %s 下" % ext, root
        return True, "", root

    # self_extend: must be under EV (MUSE_DIR)
    ev = muse_root()
    if not is_under(root, ev) and root != ev:
        return False, "self_extend 模式只允许在 EV 目录下：%s" % ev, root

    policy = load_policy()
    try:
        rel = root.relative_to(ev).as_posix()
    except Exception:
        rel = ""
    if rel and _match_any(rel, list(policy.get("core_deny_cwd_globs") or [])):
        return False, "禁止把 cwd 设在核心目录：%s" % rel, root
    # also deny if cwd itself is a core leaf like control_plane
    if rel in set(policy.get("core_deny_cwd_globs") or []):
        return False, "禁止把 cwd 设在核心目录：%s" % rel, root
    return True, "", root


def check_write_path(path: str, mode: str, get_setting=None) -> Tuple[bool, str]:
    """检查相对/绝对路径是否允许写入。"""
    mode = (mode or "external").strip().lower()
    try:
        target = expand_user_path(path)
    except Exception as e:
        return False, "无效路径：%s" % e

    policy = load_policy()
    if _match_abs(target, list(policy.get("deny_write_abs_globs") or [])):
        return False, "禁止写入核心/系统路径：%s" % target

    # absolute server tree
    server = (MAIN_DIR / "server").resolve()
    if is_under(target, server) or target == server:
        return False, "禁止写入核心 server：%s" % target

    if mode == "external":
        ext = default_external_root(get_setting)
        if is_under(target, ext) or target == ext:
            return True, ""
        return False, "external 模式禁止写到 MuseWork 之外"

    # self_extend
    ev = muse_root()
    if not is_under(target, ev) and target != ev:
        # also allow external while in self_extend? Plan says self_extend cwd is EV;
        # writes should be allow globs under EV only.
        return False, "self_extend 模式只允许写 EV 扩展区"

    try:
        rel = target.relative_to(ev).as_posix()
    except Exception:
        return False, "路径不在 EV 内"

    deny = list(policy.get("deny_write_globs") or [])
    allow = list(policy.get("allow_write_globs") or [])
    if _match_any(rel, deny):
        return False, "禁止写入核心面：%s" % rel
    if _match_any(rel, allow):
        return True, ""
    return False, "不在扩展区白名单：%s" % rel


def public_policy(get_setting=None) -> Dict[str, Any]:
    policy = load_policy()
    return {
        "external_root": str(default_external_root(get_setting)),
        "muse_root": str(muse_root()),
        "allow_write_globs": list(policy.get("allow_write_globs") or []),
        "deny_write_globs": list(policy.get("deny_write_globs") or []),
        "deny_write_abs_globs": list(policy.get("deny_write_abs_globs") or []),
        "core_deny_cwd_globs": list(policy.get("core_deny_cwd_globs") or []),
        "note": "deny 优先于 allow；核心列表不可在 UI 清空",
    }


def self_extend_rules_markdown() -> str:
    policy = load_policy()
    allow = "\n".join("- `%s`" % g for g in (policy.get("allow_write_globs") or []))
    deny = "\n".join("- `%s`" % g for g in (policy.get("deny_write_globs") or []))
    return (
        "# EV self_extend 写入规则（必须遵守）\n\n"
        "你在修改 Muse/EV 仓库。只允许写入扩展区，禁止改核心运行面。\n\n"
        "## 允许写入\n%s\n\n"
        "## 禁止写入（命中即停）\n%s\n\n"
        "以及 `server/main/server/**`、启动脚本、数据库与密钥。\n"
        "不要修改 `app.py`、`devices/voice/**`、`devices/camera/**`、`control_plane/**`、`ui/muse.js`。\n"
        "若任务需要改禁区，停止并说明原因，不要绕过。\n"
    ) % (allow or "- （无）", deny or "- （无）")
