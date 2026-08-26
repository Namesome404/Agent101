# -*- coding: utf-8 -*-
"""技能路由：通用工作 Agent、兼容执行器、搜索、阅读与工程状态。

从 app.py 拆出的 APIRouter。`_claude_code_base_url` 在 app_shared（与 chat_stream 工具轮共享）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, FileResponse

from common.paths import SERVER_DIR
from app_shared import _claude_code_base_url
from coding import path_policy as coding_path_policy
from control_plane import database as db
from devices.coding import agent_runtime
from devices.coding import claude_code as claude_code_skill
from devices.coding import project_fsm as coding_fsm
from devices.desk import hub as desk_hub
from tools import deep_search
from tools import web_reader

router = APIRouter()


@router.get("/api/agent-runtime")
def api_agent_runtime_get():
    return {"ok": True, **agent_runtime.public_config(db.get_setting, db.set_setting)}


@router.put("/api/agent-runtime")
def api_agent_runtime_put(payload: dict = Body(...)):
    try:
        config = agent_runtime.apply_config_update(db.get_setting, db.set_setting, payload or {})
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    return {"ok": True, **config}


@router.post("/api/agent-runtime/run")
def api_agent_runtime_run(payload: dict = Body(...), request: Request = None):
    body = dict(payload or {})
    result = agent_runtime.start_task_background(
        str(body.get("task") or ""),
        get_setting=db.get_setting,
        set_setting=db.set_setting,
        cwd=str(body.get("cwd") or ""),
        mode=str(body.get("mode") or "external"),
        base_url=_claude_code_base_url(request),
        timeout_s=body.get("timeout_s"),
        resume_session_id=str(body.get("session_id") or ""),
    )
    return {"ok": bool(result.get("started")), **result}


@router.get("/api/agent-runtime/runs/{run_id}/events")
def api_agent_runtime_events(run_id: str, after: int = 0):
    return {
        "ok": True,
        "run_id": run_id,
        "events": agent_runtime.get_events(run_id, after=after),
        "result": agent_runtime.get_result(run_id),
    }


@router.post("/api/agent-runtime/runs/{run_id}/control")
def api_agent_runtime_control(run_id: str, payload: dict = Body(...)):
    body = dict(payload or {})
    action = str(body.get("action") or "").strip().lower()
    if action == "cancel":
        ok = agent_runtime.cancel_run(run_id)
    elif action == "steer":
        ok = agent_runtime.steer_run(str(body.get("text") or ""), run_id)
    else:
        return JSONResponse({"ok": False, "error": "action 只能是 cancel 或 steer"}, status_code=422)
    return {"ok": bool(ok), "run_id": run_id, "action": action}


def _bootstrap_web_search_keys_from_core():
    """One-shot: copy placeholder-free key from core config.yaml into skill settings."""
    cfg = deep_search.load_config(db.get_setting)
    if deep_search._providers_ready(cfg):
        return
    try:
        from ruamel.yaml import YAML
        yaml_path = SERVER_DIR / "config.yaml"
        if not yaml_path.exists():
            return
        data = YAML(typ="safe").load(yaml_path.read_text(encoding="utf-8")) or {}
        block = ((data.get("plugins") or {}).get("web_search") or {})
        provider = str(block.get("provider") or "metaso").lower()
        api_key = str(block.get("api_key") or "").strip()
        if deep_search._is_placeholder(api_key):
            return
        if provider == "tavily" or api_key.startswith("tvly-"):
            if not cfg.get("tavily_api_key"):
                db.set_setting("skill.web_search.tavily_api_key", api_key)
                db.set_setting("skill.web_search.provider", "tavily")
        else:
            if not cfg.get("metaso_api_key"):
                db.set_setting("skill.web_search.metaso_api_key", api_key)
                db.set_setting("skill.web_search.provider", "metaso")
        max_results = block.get("max_results")
        if max_results:
            db.set_setting("skill.web_search.max_results", str(int(max_results)))
    except Exception as exc:
        print("[muse] bootstrap web_search keys skipped: %s" % exc, flush=True)


@router.get("/api/skills/claude-code")
def api_skills_claude_code_get(reveal: int = 0):
    cfg = claude_code_skill.public_config(db.get_setting, db.set_setting)
    if reveal:
        cfg["gateway_token"] = claude_code_skill.ensure_gateway_token(db.get_setting, db.set_setting)
    return {"ok": True, **cfg}


@router.put("/api/skills/claude-code")
def api_skills_claude_code_put(payload: dict = Body(...)):
    cfg = claude_code_skill.apply_config_update(db.get_setting, db.set_setting, payload or {})
    return {"ok": True, **cfg}


@router.post("/api/skills/claude-code/run")
def api_skills_claude_code_run(payload: dict = Body(...), request: Request = None):
    body = payload or {}
    result = claude_code_skill.run_task(
        body.get("task") or "",
        get_setting=db.get_setting,
        set_setting=db.set_setting,
        cwd=body.get("cwd") or "",
        mode=body.get("mode") or "",
        base_url=_claude_code_base_url(request),
        timeout_s=body.get("timeout_s"),
    )
    return {"ok": bool(result.get("ok")), **result}


@router.post("/api/skills/claude-code/check-path")
def api_skills_claude_code_check_path(payload: dict = Body(...)):
    body = payload or {}
    ok, err = coding_path_policy.check_write_path(
        body.get("path") or "",
        body.get("mode") or "self_extend",
        db.get_setting,
    )
    return {"ok": ok, "error": err or ""}


@router.get("/api/agent-runtime/preview/{rel_path:path}")
@router.get("/api/skills/claude-code/preview/{rel_path:path}")
def api_skills_claude_code_preview(rel_path: str):
    """安全托管 MuseWork（external_root）下的静态文件，供网站预览。"""
    root = Path(coding_path_policy.default_external_root(db.get_setting)).expanduser().resolve()
    target = (root / (rel_path or "")).resolve()
    try:
        target.relative_to(root)
    except Exception:
        return JSONResponse({"ok": False, "error": "路径越界"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
    # 简单 MIME
    suffix = target.suffix.lower()
    media = {
        ".html": "text/html; charset=utf-8",
        ".htm": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".txt": "text/plain; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
    }.get(suffix)
    return FileResponse(
        str(target),
        media_type=media,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/coding/fsm/{agent_id}")
def api_coding_fsm(agent_id: int):
    return {"ok": True, "state": coding_fsm.load(agent_id)}


@router.post("/api/coding/devserver/start")
def api_coding_devserver_start(payload: dict = Body(...)):
    """三期：尝试在项目 cwd 启动 npm run dev，并把预览指到本地端口（失败则说明）。"""
    body = payload or {}
    cwd = Path(body.get("cwd") or coding_path_policy.default_external_root(db.get_setting)).expanduser()
    if not cwd.is_dir():
        return {"ok": False, "error": "cwd 无效"}
    pkg = cwd / "package.json"
    if not pkg.exists():
        return {"ok": False, "error": "无 package.json，请用静态预览"}
    # 探测是否已有 vite 配置
    has_vite = any((cwd / n).exists() for n in ("vite.config.js", "vite.config.ts", "vite.config.mjs"))
    if not has_vite and "vite" not in pkg.read_text(encoding="utf-8", errors="replace"):
        return {"ok": False, "error": "未检测到 Vite；一期请用静态 preview"}
    port = int(body.get("port") or 5173)
    try:
        proc = subprocess.Popen(
            ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    url = "http://127.0.0.1:%d" % port
    desk_hub.set_preview("site-preview", url=url, locked=False)
    return {"ok": True, "pid": proc.pid, "preview_url": url, "note": "远程客户端请在电脑上查看 Desk"}


@router.get("/api/skills/web-search")
def api_skills_web_search_get():
    _bootstrap_web_search_keys_from_core()
    return {"ok": True, **deep_search.public_config(db.get_setting)}


@router.put("/api/skills/web-search")
def api_skills_web_search_put(payload: dict = Body(...)):
    cfg = deep_search.apply_config_update(db.get_setting, db.set_setting, payload or {})
    return {"ok": True, **cfg}


@router.post("/api/skills/web-search/run")
def api_skills_web_search_run(payload: dict = Body(...)):
    _bootstrap_web_search_keys_from_core()
    query = (payload or {}).get("query") or ""
    result = deep_search.search(
        query,
        get_setting=db.get_setting,
        max_results=(payload or {}).get("max_results"),
        fetch_pages=(payload or {}).get("fetch_pages"),
    )
    return {
        "ok": bool(result.get("ok")),
        "query": result.get("query"),
        "queries": result.get("queries"),
        "summary": result.get("summary") or "",
        "answer_context": result.get("answer_context"),
        "items": result.get("items") or [],
        "pages": result.get("pages") or [],
        "images": result.get("images") or [],
        "links": result.get("links") or [],
        "sources": result.get("sources") or [],
        "provider_answers": result.get("provider_answers") or [],
        "elapsed_ms": result.get("elapsed_ms"),
        "error": result.get("error") or "",
        "panel": result.get("panel"),
    }


@router.post("/api/skills/web-search/extract")
def api_skills_web_search_extract(payload: dict = Body(...)):
    _bootstrap_web_search_keys_from_core()
    url = ((payload or {}).get("url") or "").strip()
    question = ((payload or {}).get("question") or "").strip()
    page = deep_search.extract(
        url,
        get_setting=db.get_setting,
        query=question,
        include_images=True,
    )
    status = 200 if page.get("ok") else 422
    return JSONResponse({"ok": bool(page.get("ok")), **page}, status_code=status)


@router.get("/api/web/reader")
def web_reader_api(url: str = "", q: str = ""):
    """抓取网页并提取正文（优先 Tavily Extract），供 Muse 预览窗口使用。"""
    target = (url or "").strip()
    if not web_reader.is_safe_url(target):
        return JSONResponse({"ok": False, "error": "无效的网页地址"}, status_code=400)
    # Prefer deep extract when skill key ready; else local reader
    try:
        if deep_search._providers_ready(deep_search.load_config(db.get_setting)):
            page = deep_search.extract(
                target, get_setting=db.get_setting, query=q or "", include_images=True
            )
            if page.get("ok"):
                return JSONResponse({
                    "ok": True,
                    "title": page.get("title") or "",
                    "lead": page.get("summary") or "",
                    "site": page.get("site") or "",
                    "url": page.get("url") or target,
                    "image": (page.get("images") or [{}])[0].get("url") if page.get("images") else "",
                    "images": page.get("images") or [],
                    "paragraphs": page.get("paragraphs") or [],
                    "full_text": page.get("text") or "",
                    "extractor": page.get("extractor") or "",
                })
    except Exception as exc:
        print("[muse] web reader extract fallback: %s" % exc, flush=True)
    result = web_reader.extract_reader(target, include_images=True)
    status = 200 if result.get("ok") else 422
    return JSONResponse(result, status_code=status)
