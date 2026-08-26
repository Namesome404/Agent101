import httpx
from config.logger import setup_logging
from plugins_func.register import (
    register_function,
    ToolType,
    ActionResponse,
    Action,
)
from plugins_func.muse_panel import skill_panel
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

_DEFAULT_DESCRIPTION = (
    "强力联网搜索：检索网页并阅读原文。"
    "用户问时效信息、新闻、真假核实、最新进展或需要出处的事实时必须调用。"
)

WEB_SEARCH_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": _DEFAULT_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题",
                }
            },
            "required": ["query"],
        },
    },
}


def _muse_base_url(conn: "ConnectionHandler") -> str:
    # Prefer manager-api (EV) if configured; else local Muse default.
    try:
        mgr = (conn.config or {}).get("manager-api") or {}
        url = (mgr.get("url") or "").rstrip("/")
        if url:
            # http://127.0.0.1:8002/xiaozhi → http://127.0.0.1:8002
            if url.endswith("/xiaozhi"):
                return url[: -len("/xiaozhi")]
            return url
    except Exception:
        pass
    return "http://127.0.0.1:8002"


async def _run_via_muse(conn: "ConnectionHandler", query: str) -> dict:
    base = _muse_base_url(conn)
    url = base + "/api/skills/web-search/run"
    timeout = httpx.Timeout(45.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json={"query": query})
        response.raise_for_status()
        return response.json()


async def _run_local_deep_search(query: str) -> dict:
    """Same-process fallback when Muse HTTP is unavailable."""
    import sys
    from pathlib import Path

    ev_dir = Path(__file__).resolve().parents[3] / "EV"
    if str(ev_dir) not in sys.path:
        sys.path.insert(0, str(ev_dir))
    from tools import deep_search  # noqa: WPS433
    from control_plane import database as db  # noqa: WPS433

    return deep_search.search(query, get_setting=db.get_setting)


@register_function("web_search", WEB_SEARCH_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def web_search(conn: "ConnectionHandler", query: str = None):
    logger.bind(tag=TAG).info(f"web_search 被调用 | query={query}")
    if not query:
        return ActionResponse(Action.REQLLM, "请提供搜索关键词。", None)

    # Allow plugin yaml description override
    web_search_config = conn.config.get("plugins", {}).get("web_search", {})
    desc = (web_search_config.get("description") or "").strip()
    if desc:
        WEB_SEARCH_FUNCTION_DESC["function"]["description"] = desc

    result = {}
    try:
        result = await _run_via_muse(conn, query)
    except Exception as http_error:
        logger.bind(tag=TAG).warning(
            f"Muse deep_search HTTP 失败，尝试本机导入: {http_error}"
        )
        try:
            result = await _run_local_deep_search(query)
        except Exception as local_error:
            logger.bind(tag=TAG).error(f"联网搜索异常: {local_error}")
            return ActionResponse(
                Action.REQLLM,
                "联网搜索出现异常，请稍后重试。",
                None,
            )

    result_text = (
        result.get("answer_context")
        or result.get("error")
        or "未找到相关搜索结果。"
    )
    items = result.get("items") or []
    # Prefer panel payload from engine; fall back to items
    panel = None
    engine_panel = result.get("panel") or {}
    panel_data = engine_panel.get("data") if isinstance(engine_panel, dict) else None
    if panel_data and panel_data.get("items"):
        panel = skill_panel(
            "search",
            engine_panel.get("title") or f"搜索：{query}",
            data=panel_data,
            width=int(engine_panel.get("width") or 480),
            height=int(engine_panel.get("height") or 440),
        )
    elif items:
        max_results = int(web_search_config.get("max_results", 5) or 5)
        panel = skill_panel(
            "search",
            f"搜索：{query}",
            data={
                "query": query,
                "items": [
                    {
                        "title": it.get("title"),
                        "snippet": it.get("snippet"),
                        "date": it.get("date") or "",
                        "url": it.get("url"),
                    }
                    for it in items[:max_results]
                ],
                "pages": result.get("pages") or [],
            },
            width=480,
            height=440,
        )

    logger.bind(tag=TAG).info(
        f"deep_search 完成 | ok={result.get('ok')} sources={result.get('sources')} items={len(items)}"
    )
    return ActionResponse(Action.REQLLM, result_text, None, panel=panel)
