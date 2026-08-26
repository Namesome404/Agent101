# -*- coding: utf-8 -*-
"""
EV 控制面契约：构造核心 server 期望的响应 data。
- server-base：完整基础配置（核心启动时的全局基线）
- agent-models：按设备所属智能体解析出的差异化配置
返回结构对齐 core/connection.py 的合并逻辑与 Java ConfigServiceImpl。
"""
import json

from control_plane import database as db

MODULE_TYPES = db.MODULE_TYPES


def build_server_base():
    """返回完整 config.yaml（去掉 server/manager-api，核心用本地覆盖）。"""
    base = db._to_plain(db.load_yaml(db.BASE_CONFIG))
    base.pop("server", None)
    base.pop("manager-api", None)
    base.pop("voiceprint", None)
    return base


def _resolve_block(catalog, mt, name, overrides):
    blk = dict(catalog.get(mt, {}).get(name, {}) or {})
    if isinstance(overrides, dict):
        blk.update(overrides)
    # mem_local_short 默认挂 ChatGLMLLM；若该 LLM 仍是占位 key，则改用主 LLM，避免记忆/对话被拖垮
    if mt == "Memory" and blk.get("type") == "mem_local_short":
        mem_llm = blk.get("llm")
        if mem_llm:
            llm_blk = (catalog.get("LLM") or {}).get(mem_llm) or {}
            key = str(llm_blk.get("api_key") or "")
            if (not key) or ("你的" in key) or ("请替换" in key) or ("api key" in key.lower()):
                blk = dict(blk)
                blk.pop("llm", None)
    return blk


def _ensure_memory_module(modules):
    """未配置记忆时默认本地短期总结；显式 nomem 保持关闭。"""
    m = dict(modules or {})
    cur = m.get("Memory") or {}
    selected = (cur.get("selected") or "").strip()
    if not selected:
        m["Memory"] = {"selected": "mem_local_short", "overrides": cur.get("overrides") or {}}
    return m


def _agent_plugins_enabled(agent):
    raw = agent.get("plugins")
    if raw is None:
        return None
    if isinstance(raw, dict):
        return list(raw.get("enabled") or [])
    if isinstance(raw, list):
        return list(raw)
    return []


def _agent_plugin_overrides(agent):
    raw = agent.get("plugins")
    if isinstance(raw, dict):
        ov = raw.get("overrides") or {}
        return ov if isinstance(ov, dict) else {}
    return {}


def build_agent_plugins(agent):
    """
    合并 config.yaml 全局插件默认 + 智能体启用列表与覆盖参数。
    返回 (plugins_dict_or_none, enabled_names_or_none)
      plugins_dict: {code: json_string} 供 core json.loads
      enabled_names: 写入 Intent.functions；None 表示不覆盖 server-base
    """
    enabled = _agent_plugins_enabled(agent)
    if enabled is None:
        return None, None
    base_plugins = db._to_plain(db.load_yaml(db.BASE_CONFIG).get("plugins") or {})
    overrides = _agent_plugin_overrides(agent)
    if not enabled:
        return {}, []
    merged = {}
    for code in enabled:
        cfg = dict(base_plugins.get(code) or {})
        extra = overrides.get(code)
        if isinstance(extra, dict):
            cfg.update(extra)
        merged[code] = json.dumps(cfg, ensure_ascii=False)
    return merged, enabled


def _default_agent_id():
    """未绑定设备回退用的默认智能体：优先 settings.default_agent_id，否则取最早创建的智能体。"""
    v = db.get_setting("default_agent_id")
    if v:
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    agents = db.list_agents()
    return agents[0]["id"] if agents else None


def build_agent_models(mac, client_id, selected_module_from_client):
    """
    返回 (status, payload)
      status='bind' → payload=绑定码(str)
      status='ok'   → payload=配置 dict
    """
    device = db.touch_or_create_device(mac, client_id)
    agent_id = device["agent_id"] if device else None
    if agent_id is None:
        # 未绑定设备回退到默认智能体，实现「打开即用」；仍保留绑定系统——
        # 一旦设备被绑到某个智能体，下面就用该智能体覆盖此默认。
        agent_id = _default_agent_id()

    agent = db.get_agent(agent_id) if agent_id else None
    if not agent:
        # 连默认智能体都不存在（空库）才要求绑定
        return ("bind", (device["bind_code"] if device else "------") or "------")

    catalog = db.provider_catalog()
    modules = _ensure_memory_module(agent.get("modules", {}))

    result = {}
    selected = {}
    for mt in MODULE_TYPES:
        m = modules.get(mt)
        if not m or not m.get("selected"):
            continue
        name = m["selected"]
        selected[mt] = name
        result[mt] = {name: _resolve_block(catalog, mt, name, m.get("overrides"))}
    result["selected_module"] = selected

    if agent.get("prompt"):
        result["prompt"] = agent["prompt"]

    # 运行时档案 + 旧事实条，合并下发给核心记忆通道
    from control_plane import dossier as dossier_lib
    dossier = db.get_agent_dossier(agent["id"]) if agent.get("id") else None
    dossier_text = dossier_lib.dossier_to_prompt(dossier or {})
    items = db._raw_to_items(agent.get("summary_memory") or "")
    bullets = db.memory_items_to_prompt(items)
    chunks = [c for c in (dossier_text, bullets) if c]
    result["summaryMemory"] = "\n\n".join(chunks)

    # Muse 终端设备：注入桌面面板 MCP 工具使用说明
    if str(mac or "").startswith("muse:"):
        hint = (
            "\n\n【EV·技能与窗口·必遵守】"
            "1) 用户问天气时调用 get_weather；问新闻/热点/时效事实/搜索时必须调用 web_search，"
            "调用后屏幕会自动弹出结果窗口。不要使用已移除的国内新闻插件。"
            "2) 用户说「打开窗口/面板/窗户」时，必须调用 muse_ui_open_panel（屏幕浮窗，不是智能家居）。"
            "3) 用户说「预览/打开链接/看看这篇/第N条」时，必须调用 muse_ui_open_panel，panel=web，并传入 url（搜索结果里的链接）。"
            "4) 用户说「总结/讲讲/解读/详细内容」时，用 web_search 检索相关内容，或用 muse_ui_open_panel(panel=web, url=…) 打开具体链接；不要打开空 news 面板。"
            "5) 用户说「打开新闻/看看新闻」时，必须先 web_search，不要 muse_ui_open_panel 空 news 面板。"
            "6) 用户问天气时只调用 get_weather，禁止再 muse_ui_open_panel 空 weather 面板（系统会自动上屏）；禁止编造无数字的温度数据。"
            "7) 口头只答一句简短口语，禁止写「调用工具」「（调用xxx）」等旁白，禁止用括号描述动作。"
            "8) 事实铁律：只汇报本轮确实调用成功的工具结果；未调用/失败则如实说没查到或没打开，"
            "禁止假装已改代码、已开窗、已搜到。进度以真实回执为准，不说「应该好了」。"
        )
        result["prompt"] = (result.get("prompt") or "") + hint

    mcp = (agent.get("mcp_endpoint") or "").strip()
    if mcp.startswith("ws"):
        result["mcp_endpoint"] = mcp.replace("/mcp/", "/call/")

    plugins_payload, _enabled_plugins = build_agent_plugins(agent)
    if plugins_payload is not None:
        result["plugins"] = plugins_payload
        intent_name = (result.get("selected_module") or {}).get("Intent") or "function_call"
        intent_blk = (result.get("Intent") or {}).get(intent_name)
        if isinstance(intent_blk, dict):
            intent_blk = dict(intent_blk)
            intent_blk["functions"] = list(plugins_payload.keys())
            result["Intent"] = dict(result.get("Intent") or {})
            result["Intent"][intent_name] = intent_blk

    return ("ok", result)
