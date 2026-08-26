# -*- coding: utf-8 -*-
"""管理 REST /api/*：bootstrap、provider 检查、状态、智能体 CRUD / 记忆 / 档案。

从 app.py 拆出的 APIRouter。纯管理面，不涉及 chat/流式/live 主战场。
"""
from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app_shared import (
    PLUGINS_DIR,
    _avatar_catalog,
    _provider_config_state,
    _provider_status_catalog,
    _tcp_open,
    EDGE_VOICES,
    MINIMAX_VOICES,
)
from control_plane import database as db
from control_plane import dossier as dossier_lib

router = APIRouter()


@router.get("/api/bootstrap")
def bootstrap():
    catalog = db.provider_catalog()
    provider_configs = db.seed_provider_configs_from_agents()
    funcs = []
    if PLUGINS_DIR.exists():
        for p in sorted(PLUGINS_DIR.glob("*.py")):
            if not p.stem.startswith("__") and p.stem != "hass_init":
                funcs.append(p.stem)
    avatars = _avatar_catalog()
    return {
        "catalog": catalog,
        "provider_configs": provider_configs,
        "provider_status": _provider_status_catalog(catalog, provider_configs),
        "module_types": db.MODULE_TYPES,
        "functions": funcs,
        "avatars": avatars,
        "edge_voices": [{"value": v, "label": l} for v, l in EDGE_VOICES],
        "minimax_voices": [{"value": v, "label": l} for v, l in MINIMAX_VOICES],
        "secret": db.get_setting("server.secret"),
    }


@router.post("/api/providers/check")
def api_provider_check(payload: dict = Body(...)):
    module_type = str(payload.get("module_type") or "")
    provider = str(payload.get("provider") or "")
    if provider not in (db.provider_catalog().get(module_type) or {}):
        return JSONResponse({"error": "未知供应商"}, status_code=404)
    return _provider_config_state(
        module_type,
        provider,
        overrides=payload.get("overrides") or {},
    )


@router.get("/api/status")
def api_status():
    return {"core_up": _tcp_open("127.0.0.1", 8000),
            "agents": len(db.list_agents()),
            "devices": len(db.list_devices())}


@router.get("/api/agents")
def api_agents():
    return {"agents": db.list_agents()}


@router.get("/api/agents/{aid}")
def api_agent_get(aid: int):
    a = db.get_agent(aid)
    return a or JSONResponse({"error": "not found"}, status_code=404)


@router.post("/api/agents")
def api_agent_create(data: dict = Body(...)):
    return {"id": db.create_agent(data)}


@router.put("/api/agents/{aid}")
def api_agent_update(aid: int, data: dict = Body(...)):
    modules = data.get("modules")
    pending_profiles = []
    current_agent = db.get_agent(aid) or {}
    current_modules = current_agent.get("modules") or {}
    if isinstance(modules, dict):
        for module_type, node in modules.items():
            if not isinstance(node, dict):
                continue
            provider = str(node.get("selected") or "")
            overrides = node.get("overrides") or {}
            if not provider:
                continue
            state = _provider_config_state(module_type, provider, overrides=overrides)
            if not state["configured"]:
                current_provider = str(
                    ((current_modules.get(module_type) or {}).get("selected")) or ""
                )
                if current_provider == provider:
                    continue
                return JSONResponse(
                    {
                        "error": "%s / %s 未配置完成：%s"
                        % (module_type, provider, "、".join(state["missing"])),
                    },
                    status_code=400,
                )
            pending_profiles.append((module_type, provider, overrides))
    for module_type, provider, overrides in pending_profiles:
        db.set_provider_config(module_type, provider, overrides)
    updated = db.update_agent(aid, data)
    return {
        "ok": updated,
        "apply": {
            "camera_asr": "within_2_seconds",
            "camera_llm_tts": "next_turn",
            "ev_terminal": "next_connection",
        },
        "agent": db.get_agent(aid) if updated else None,
    }


@router.delete("/api/agents/{aid}")
def api_agent_delete(aid: int):
    db.delete_agent(aid)
    return {"ok": True}


@router.get("/api/agents/{aid}/memory")
def api_agent_memory_get(aid: int):
    a = db.get_agent(aid)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    mem = (a.get("modules") or {}).get("Memory") or {}
    items = db.get_agent_memory_items(aid) or []
    dossier = db.get_agent_dossier(aid) or dossier_lib.empty_dossier()
    return {
        "provider": mem.get("selected") or "nomem",
        "items": items,
        "summary_memory": db.memory_items_to_prompt(items),
        "dossier": dossier,
        "dossier_prompt": dossier_lib.dossier_to_prompt(dossier),
    }


@router.get("/api/agents/{aid}/dossier")
def api_agent_dossier_get(aid: int):
    if not db.get_agent(aid):
        return JSONResponse({"error": "not found"}, status_code=404)
    dossier = db.get_agent_dossier(aid) or dossier_lib.empty_dossier()
    return {
        "ok": True,
        "dossier": dossier,
        "prompt_preview": dossier_lib.dossier_to_prompt(dossier),
    }


@router.put("/api/agents/{aid}/dossier")
def api_agent_dossier_put(aid: int, data: dict = Body(...)):
    if not db.get_agent(aid):
        return JSONResponse({"error": "not found"}, status_code=404)
    if "dossier" in (data or {}):
        ok = db.set_agent_dossier(aid, data.get("dossier"))
    elif "patch" in (data or {}):
        ok = db.patch_agent_dossier(aid, data.get("patch")) is not None
    else:
        ok = db.set_agent_dossier(aid, data or {})
    if not ok:
        return JSONResponse({"ok": False, "error": "save failed"}, status_code=400)
    dossier = db.get_agent_dossier(aid) or dossier_lib.empty_dossier()
    return {
        "ok": True,
        "dossier": dossier,
        "prompt_preview": dossier_lib.dossier_to_prompt(dossier),
    }


@router.put("/api/agents/{aid}/memory")
def api_agent_memory_put(aid: int, data: dict = Body(...)):
    if not db.get_agent(aid):
        return JSONResponse({"error": "not found"}, status_code=404)
    if "items" in data and isinstance(data.get("items"), list):
        ok = db.set_agent_memory_items(aid, data["items"])
        return {"ok": ok, "items": db.get_agent_memory_items(aid) or []}
    # 兼容：整段文本按行拆成条目
    text = data.get("summary_memory")
    if text is None:
        text = data.get("summaryMemory", "")
    items = db._raw_to_items(text if text is not None else "")
    ok = db.set_agent_memory_items(aid, items)
    return {"ok": ok, "items": db.get_agent_memory_items(aid) or []}


@router.post("/api/agents/{aid}/memory/items")
def api_agent_memory_add(aid: int, data: dict = Body(...)):
    if not db.get_agent(aid):
        return JSONResponse({"error": "not found"}, status_code=404)
    text = (data.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "空内容"}, status_code=400)
    items = db.add_agent_memory_item(aid, text, data.get("source") or "manual")
    return {"ok": True, "items": items or []}


@router.put("/api/agents/{aid}/memory/items/{item_id}")
def api_agent_memory_update(aid: int, item_id: str, data: dict = Body(...)):
    if not db.get_agent(aid):
        return JSONResponse({"error": "not found"}, status_code=404)
    r = db.update_agent_memory_item(aid, item_id, data.get("text") or "")
    if r is False:
        return JSONResponse({"error": "条目不存在"}, status_code=404)
    return {"ok": True, "items": db.get_agent_memory_items(aid) or []}


@router.delete("/api/agents/{aid}/memory/items/{item_id}")
def api_agent_memory_delete_item(aid: int, item_id: str):
    if not db.get_agent(aid):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_agent_memory_item(aid, item_id)
    return {"ok": True, "items": db.get_agent_memory_items(aid) or []}


@router.delete("/api/agents/{aid}/memory")
def api_agent_memory_clear(aid: int):
    if not db.get_agent(aid):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": db.set_agent_memory_items(aid, [])}



