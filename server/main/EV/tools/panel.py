# -*- coding: utf-8 -*-
"""Muse panel tool argument enrichment."""

from __future__ import annotations

from typing import Any, Dict, Optional


def _normalize_news_data(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    if data.get("items"):
        return data
    articles = data.get("articles")
    if isinstance(articles, list) and articles:
        out = dict(data)
        out["items"] = [
            {
                "title": a.get("title") or "无标题",
                "url": a.get("url") or "",
                "source": data.get("source") or a.get("source") or "",
                "snippet": a.get("content") or a.get("snippet") or a.get("desc") or "",
            }
            for a in articles
        ]
        return out
    return data


def _has_temp_digits(val: Any) -> bool:
    return any(ch.isdigit() for ch in str(val or ""))


def _forecast_has_temps(forecast: Any) -> bool:
    if not isinstance(forecast, list) or not forecast:
        return False
    return any(
        isinstance(f, dict)
        and (_has_temp_digits(f.get("high")) or _has_temp_digits(f.get("low")))
        for f in forecast
    )


def _weather_data_complete(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if not (data.get("city") or "").strip():
        return False

    temp_val = data.get("temp") or data.get("current")
    if isinstance(temp_val, dict):
        return False
    has_current_temp = _has_temp_digits(temp_val)
    if not has_current_temp:
        details = data.get("details") or {}
        if isinstance(details, dict):
            for key, val in details.items():
                if "温度" in str(key) or str(key).lower() == "temp":
                    if _has_temp_digits(val):
                        has_current_temp = True
                        break
    if not has_current_temp:
        return False

    forecast = data.get("forecast") or []
    if isinstance(forecast, list) and forecast and not _forecast_has_temps(forecast):
        return False
    return True


def _merge_weather_data(cached: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """保留缓存中的温度数字，仅合并 incoming 的有效字段。"""
    out = dict(cached)
    if not isinstance(incoming, dict):
        return out

    for key, val in incoming.items():
        if val is None or val == "" or val == []:
            continue
        if key == "forecast" and isinstance(val, list):
            base_rows = out.get("forecast") or []
            merged = []
            for i, row in enumerate(val):
                if not isinstance(row, dict):
                    continue
                base = base_rows[i] if i < len(base_rows) and isinstance(base_rows[i], dict) else {}
                item = dict(base)
                for fk, fv in row.items():
                    if fv is None or fv == "":
                        continue
                    if fk in ("high", "low", "max", "min") and not _has_temp_digits(fv):
                        continue
                    item[fk] = fv
                if not _has_temp_digits(item.get("high")) and _has_temp_digits(base.get("high")):
                    item["high"] = base.get("high")
                if not _has_temp_digits(item.get("low")) and _has_temp_digits(base.get("low")):
                    item["low"] = base.get("low")
                merged.append(item)
            if merged:
                out["forecast"] = merged
            continue
        if key in ("temp", "current", "subtitle") and not _has_temp_digits(val):
            if key == "current" and not out.get("condition"):
                text = str(val).strip()
                if text and not _has_temp_digits(text):
                    out["condition"] = text.split("，")[0].split(",")[0].strip()
            continue
        if key == "details" and isinstance(val, dict):
            out["details"] = {**(out.get("details") or {}), **val}
            continue
        out[key] = val
    return out


def _has_panel_body(args: Dict[str, Any]) -> bool:
    data = args.get("data")
    if isinstance(data, dict):
        if data.get("items") or data.get("articles") or data.get("paragraphs"):
            return True
        if data.get("forecast") or data.get("city") or data.get("temp") or data.get("current"):
            return True
    return bool((args.get("content") or "").strip())


def _resolve_url(conn, args: Dict[str, Any]) -> str:
    url = (args.get("url") or "").strip()
    if url and url != "#":
        return url
    link = getattr(conn, "last_newsnow_link", None) or {}
    return (link.get("url") or "").strip()


def _prefetch_article(url: str) -> Optional[Dict[str, Any]]:
    try:
        from tools import web_reader as wr

        if not wr.is_safe_url(url):
            return None
        article = wr.extract_reader(url)
        if article.get("ok") and (article.get("paragraphs") or article.get("full_text")):
            if not article.get("paragraphs") and article.get("full_text"):
                article = dict(article)
                article["paragraphs"] = [
                    {"tag": "p", "text": line.strip()}
                    for line in article["full_text"].split("\n")
                    if line.strip()
                ]
            return article
    except Exception:
        pass
    return None


def _article_plain_text(article: Dict[str, Any], limit: int = 12000) -> str:
    paras = article.get("paragraphs") or []
    lines = [p.get("text", "").strip() for p in paras if isinstance(p, dict) and p.get("text")]
    if lines:
        return "\n\n".join(lines)[:limit]
    return (article.get("full_text") or "")[:limit]


def enrich_muse_panel_args(conn, arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """在 MCP 上屏前补全 panel 参数，避免「等待新闻数据…」空窗。"""
    args = dict(arguments or {})
    panel = args.get("panel") or "custom"

    if isinstance(args.get("data"), dict):
        args["data"] = _normalize_news_data(args["data"])

    if panel == "news" and not _has_panel_body(args):
        items = getattr(conn, "last_newsnow_items", None)
        if items:
            args["data"] = {"source": "热点新闻", "items": items}

    if panel == "weather":
        cached_panel = getattr(conn, "last_weather_panel", None)
        cached_data = (cached_panel or {}).get("data") if isinstance(cached_panel, dict) else None
        incoming = args.get("data") if isinstance(args.get("data"), dict) else {}
        if cached_data:
            if not _weather_data_complete(incoming):
                args["data"] = cached_data
            else:
                args["data"] = _merge_weather_data(cached_data, incoming)
            if cached_panel.get("title") and not (args.get("title") or "").strip():
                args["title"] = cached_panel["title"]
        elif not _has_panel_body(args):
            if cached_data:
                args["data"] = cached_data
                if cached_panel.get("title") and not (args.get("title") or "").strip():
                    args["title"] = cached_panel["title"]

    url = _resolve_url(conn, args)
    if url and url != "#":
        args.setdefault("url", url)

    need_fetch = (
        panel in ("web", "news", "custom")
        and not args.get("data")
        and url
        and url != "#"
    )
    if need_fetch:
        article = _prefetch_article(url)
        if article:
            args["panel"] = "web"
            args["data"] = article
            args["url"] = url
            if not (args.get("title") or "").strip():
                args["title"] = (article.get("title") or "新闻详情")[:40]
            if not (args.get("content") or "").strip():
                args["content"] = _article_plain_text(article)

    data = args.get("data")
    if isinstance(data, dict) and not (args.get("content") or "").strip():
        text = _article_plain_text(data)
        if text:
            args["content"] = text

    return args
