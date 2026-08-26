# -*- coding: utf-8 -*-
"""
强力网络搜索引擎（唯一实现）。

Tavily 能力：Search（含答案/配图）→ Extract（正文+图片）→ 汇总证据。
返回结构支持：总结 / 链接重现 / 配图抽取。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import re
import time
from typing import List, Optional
from urllib.parse import urlparse

import httpx

from tools import web_reader

SETTING_PREFIX = "skill.web_search."
DEFAULTS = {
    "enabled": True,
    "provider": "agentsearch",  # agentsearch | tavily | metaso | both
    # 本地自托管开源检索层（vendor/agent_search + SearXNG），无需任何 API key。
    "agentsearch_url": "http://127.0.0.1:3939",
    "searxng_url": "http://127.0.0.1:8088",
    "tavily_api_key": "",
    "metaso_api_key": "",
    "max_results": 6,
    "fetch_pages": 3,
    "search_depth": "advanced",
    "use_extract": True,
    "include_images": True,
}

_PLACEHOLDER_KEYS = ("mk-xxx", "tvly-xxx", "你的", "请替换", "xxx")
_NEWS_RE = re.compile(
    r"(新闻|热点|热搜|头条|资讯|快讯|突发|today.?s?\s*news|breaking)",
    re.I,
)


def _is_placeholder(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    low = text.lower()
    return any(token in low for token in _PLACEHOLDER_KEYS)


def load_config(get_setting) -> dict:
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        raw = get_setting(SETTING_PREFIX + key, None)
        if raw is None or raw == "":
            continue
        if key in ("enabled", "use_extract", "include_images"):
            cfg[key] = str(raw).lower() in ("1", "true", "yes", "on")
        elif key in ("max_results", "fetch_pages"):
            try:
                cfg[key] = max(0 if key == "fetch_pages" else 1, min(12, int(raw)))
            except Exception:
                pass
        else:
            cfg[key] = str(raw)
    return cfg


def public_config(get_setting) -> dict:
    cfg = load_config(get_setting)
    return {
        "enabled": bool(cfg["enabled"]),
        "provider": cfg["provider"],
        "max_results": int(cfg["max_results"]),
        "fetch_pages": int(cfg["fetch_pages"]),
        "search_depth": cfg.get("search_depth") or "advanced",
        "use_extract": bool(cfg.get("use_extract", True)),
        "include_images": bool(cfg.get("include_images", True)),
        "tavily_api_key_set": not _is_placeholder(cfg.get("tavily_api_key") or ""),
        "metaso_api_key_set": not _is_placeholder(cfg.get("metaso_api_key") or ""),
        "tavily_api_key_masked": _mask(cfg.get("tavily_api_key") or ""),
        "metaso_api_key_masked": _mask(cfg.get("metaso_api_key") or ""),
        "agentsearch_url": cfg.get("agentsearch_url") or "",
        "searxng_url": cfg.get("searxng_url") or "",
        "ready": _providers_ready(cfg),
        "features": ["search", "extract", "summary", "links", "images"],
    }


def _mask(key: str) -> str:
    if _is_placeholder(key):
        return ""
    if len(key) <= 8:
        return "••••"
    return key[:4] + "••••" + key[-4:]


def _providers_ready(cfg: dict) -> List[str]:
    provider = (cfg.get("provider") or "agentsearch").lower()
    ready = []
    # 自托管检索层不需要 key，配了地址就算就绪；服务没起时由调用处报错回退。
    agentsearch_ok = bool((cfg.get("agentsearch_url") or "").strip())
    if provider in ("agentsearch", "both") and agentsearch_ok:
        ready.append("agentsearch")
    tavily_ok = not _is_placeholder(cfg.get("tavily_api_key") or "")
    metaso_ok = not _is_placeholder(cfg.get("metaso_api_key") or "")
    if provider in ("tavily", "both") and tavily_ok:
        ready.append("tavily")
    if provider in ("metaso", "both") and metaso_ok:
        ready.append("metaso")
    if not ready:
        if agentsearch_ok:
            ready.append("agentsearch")
        if tavily_ok:
            ready.append("tavily")
        if metaso_ok:
            ready.append("metaso")
    return ready


def rewrite_queries(query: str) -> List[str]:
    q = (query or "").strip()
    if not q:
        return []
    variants = [q]
    cleaned = re.sub(
        r"^(请|帮我|给我|麻烦|可否|能不能)?"
        r"(搜一下|搜索一下|查一下|查查|看看|检索一下|搜索|检索|搜)\s*",
        "",
        q,
    ).strip(" ：:，,")
    if cleaned and cleaned != q:
        variants.append(cleaned)
    if re.search(r"(最新|今天|今日|现在|刚刚|近期|这周|本周)", q):
        year = time.strftime("%Y")
        base = cleaned or q
        variants.append("%s %s" % (base, year))
        if "最新" not in base:
            variants.append("%s 最新消息" % base)
    seen = set()
    out = []
    for item in variants:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:3]


def _norm_url(url: str) -> str:
    try:
        parsed = urlparse((url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        return "%s://%s%s" % (parsed.scheme, parsed.netloc.lower(), parsed.path or "/")
    except Exception:
        return ""


def _guess_topic(query: str) -> str:
    return "news" if _NEWS_RE.search(query or "") else "general"


def _norm_images(raw) -> List[dict]:
    out = []
    seen = set()
    for item in raw or []:
        if isinstance(item, str):
            url, alt = item.strip(), ""
        elif isinstance(item, dict):
            url = (item.get("url") or item.get("src") or "").strip()
            alt = (item.get("description") or item.get("alt") or "").strip()
        else:
            continue
        if not url or url in seen:
            continue
        if not url.startswith(("http://", "https://")):
            continue
        seen.add(url)
        out.append({"url": url, "alt": alt})
        if len(out) >= 8:
            break
    return out


def _paragraphs_from_text(text: str) -> List[dict]:
    paras = []
    for line in re.split(r"\n+", text or ""):
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) < 8:
            continue
        tag = "h3" if len(line) < 40 and not line.endswith(("。", ".", "！", "？")) else "p"
        paras.append({"tag": tag, "text": line[:1200]})
        if len(paras) >= 40:
            break
    return paras


def _search_agentsearch(
    base_url: str,
    query: str,
    max_results: int,
    *,
    include_images: bool = True,
    searxng_url: str = "",
    timeout: int = 25,
) -> dict:
    """自托管 AgentSearch 检索：多引擎聚合 + 去重 + 跨引擎打分。

    它不产出 Tavily 那种 answer，摘要由 _build_summary 从抽取到的正文生成；
    配图 AgentSearch 也不提供，直接取 SearXNG 的 images 分类补齐。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        raise RuntimeError("agentsearch_url 未配置")
    with httpx.Client(timeout=float(timeout), trust_env=False) as client:
        response = client.get(
            base + "/search",
            params={"q": query, "max_results": max(1, min(20, int(max_results or 6)))},
        )
        response.raise_for_status()
        data = response.json() or {}
    items = []
    for row in data.get("results") or []:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        items.append({
            "title": (row.get("title") or "").strip(),
            "url": url,
            "snippet": (row.get("snippet") or row.get("content") or "").strip()[:600],
            "date": (row.get("published_date") or "").strip(),
            "score": row.get("score"),
            "favicon": "",
            "source": "agentsearch",
        })
    images = []
    if include_images and searxng_url:
        images = _search_images_searxng(searxng_url, query)
    return {
        "provider": "agentsearch",
        "answer": "",
        "items": items,
        "images": images,
        "topic": _guess_topic(query),
    }


# 图库/图标站：出的是摆拍和 logo，技术图（接线图/引脚图/原理图）永远不在这里
_STOCK_IMAGE_ENGINES = (
    "devicons", "unsplash", "pexels", "pixabay", "freepik", "flickr",
    "shutterstock", "istock", "gettyimages",
)


def _image_query_tokens(query: str) -> List[str]:
    """从查询里取可用于匹配标题的词：拉丁词 + 中文双字以上片段。"""
    text = str(query or "").lower()
    tokens = re.findall(r"[a-z0-9]{2,}", text)
    tokens += re.findall(r"[\u4e00-\u9fff]{2,}", text)
    return tokens


def _rank_images(rows: List[dict], query: str) -> List[dict]:
    """给图片结果排序，让主图真的对题。

    SearXNG 的原始顺序不可用：搜「Arduino 接线图」时第一条是 devicons 的
    Arduino 图标、第二条是 unsplash 摆拍，真正的 Pinout 图排在后面。而 figure
    布局取第一张当主图，于是用户看到一个 logo。
    """
    tokens = _image_query_tokens(query)

    def rank(row: dict) -> float:
        score = float(row.get("score") or 0)
        engine = str(row.get("engine") or "").lower()
        if any(name in engine for name in _STOCK_IMAGE_ENGINES):
            score -= 8.0
        title = str(row.get("title") or "").lower()
        score += sum(2.0 for token in tokens if token in title)
        return -score

    return sorted(rows, key=rank)


def _search_images_searxng(searxng_url: str, query: str, limit: int = 6) -> List[dict]:
    """配图走 SearXNG 的 images 分类（AgentSearch 本身不返回图片）。失败不影响主流程。"""
    try:
        with httpx.Client(timeout=12.0, trust_env=False) as client:
            response = client.get(
                (searxng_url or "").rstrip("/") + "/search",
                params={"q": query, "format": "json", "categories": "images"},
            )
            response.raise_for_status()
            rows = (response.json() or {}).get("results") or []
    except Exception:
        return []
    ranked = _rank_images(
        [r for r in rows if r.get("img_src") or r.get("url")], query,
    )
    return _norm_images([
        {"url": r.get("img_src") or r.get("url"), "description": r.get("title") or ""}
        for r in ranked[:limit]
    ])


def _extract_agentsearch(
    base_url: str,
    urls: List[str],
    *,
    timeout: int = 45,
) -> dict:
    """用 AgentSearch 的 /read/batch 抽正文（多级降级：直取→readability→换UA→
    浏览器渲染→Wayback→Google缓存）。返回 {url: page_dict}，与 Tavily 版同构。"""
    if not urls:
        return {}
    base = (base_url or "").rstrip("/")
    by_url: dict = {}
    try:
        with httpx.Client(timeout=float(timeout), trust_env=False) as client:
            response = client.post(
                base + "/read/batch",
                json={"urls": list(urls)[:10]},
            )
            response.raise_for_status()
            rows = (response.json() or {}).get("results") or []
    except Exception as exc:
        raise RuntimeError("agentsearch extract failed: %s" % str(exc)[:160])
    for row in rows:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        raw = (row.get("content") or "").strip()
        paragraphs = _paragraphs_from_text(raw)
        summary = ""
        for para in paragraphs:
            if para.get("tag") == "p" and len(para.get("text") or "") > 40:
                summary = para["text"][:400]
                break
        if not summary:
            summary = raw[:400]
        site = urlparse(url).hostname or ""
        by_url[_norm_url(url) or url] = {
            "ok": bool(raw),
            "url": url,
            "title": (row.get("title") or "").strip() or site or url,
            "site": site,
            "lead": summary,
            "text": raw[:8000],
            "summary": summary,
            "images": [],
            "paragraphs": paragraphs[:40],
            "error": "" if raw else (row.get("error") or "未能提取正文"),
            "extractor": "agentsearch",
        }
    return by_url


def _search_tavily(
    api_key: str,
    query: str,
    max_results: int,
    depth: str = "advanced",
    include_images: bool = True,
    include_answer: str | bool = "advanced",
) -> dict:
    topic = _guess_topic(query)
    if depth not in ("basic", "advanced", "fast", "ultra-fast"):
        depth = "advanced"
    # voice/fast：用 basic 答案，避免 advanced 综合再拖几秒
    if include_answer is True:
        answer_mode = True if depth in ("basic", "fast", "ultra-fast") else "basic"
    elif include_answer is False:
        answer_mode = False
    else:
        answer_mode = include_answer
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": depth,
        "include_answer": answer_mode,
        "include_raw_content": False,
        "include_images": bool(include_images),
        "include_image_descriptions": bool(include_images),
        "include_favicon": True,
        "topic": topic,
    }
    # 中文查询略偏向中国结果
    if re.search(r"[\u4e00-\u9fff]", query) and topic == "general":
        payload["country"] = "china"
    with httpx.Client(timeout=httpx.Timeout(22.0, connect=4.0)) as client:
        response = client.post(
            "https://api.tavily.com/search",
            json=payload,
            headers={
                "Authorization": "Bearer %s" % api_key,
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()
    items = []
    for row in data.get("results") or []:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        items.append({
            "title": (row.get("title") or "无标题").strip(),
            "url": url,
            "snippet": (row.get("content") or "").strip()[:600],
            "date": (row.get("published_date") or "").strip(),
            "score": row.get("score"),
            "favicon": (row.get("favicon") or "").strip(),
            "source": "tavily",
        })
    images = _norm_images(data.get("images") or [])
    return {
        "provider": "tavily",
        "answer": (data.get("answer") or "").strip(),
        "items": items,
        "images": images,
        "topic": topic,
    }


def _search_metaso(api_key: str, query: str, max_results: int) -> dict:
    payload = {
        "q": query,
        "size": max_results,
        "stream": False,
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": False,
        "conciseSnippet": False,
    }
    with httpx.Client(timeout=httpx.Timeout(18.0, connect=4.0)) as client:
        response = client.post(
            "https://metaso.cn/api/v1/search",
            json=payload,
            headers={
                "Authorization": "Bearer %s" % api_key,
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()
    items = []
    for row in data.get("webpages") or []:
        url = (row.get("link") or row.get("url") or "").strip()
        if not url:
            continue
        items.append({
            "title": (row.get("title") or "无标题").strip(),
            "url": url,
            "snippet": (row.get("summary") or "").strip()[:600],
            "date": (row.get("date") or "").strip(),
            "score": None,
            "favicon": "",
            "source": "metaso",
        })
    return {"provider": "metaso", "answer": "", "items": items, "images": [], "topic": ""}


def _merge_items(batches: List[dict], limit: int) -> List[dict]:
    merged = []
    seen = set()
    for batch in batches:
        for item in batch.get("items") or []:
            key = _norm_url(item.get("url") or "") or item.get("url")
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= limit:
                return merged
    return merged


_QUERY_FILLER_TERMS = {
    "请", "帮我", "给我", "麻烦", "搜索", "搜一下", "查找", "看看",
    "有没有", "有没", "什么", "怎么", "如何", "一个", "这个", "那个",
    "一下", "相关", "资料", "信息",
}


def _query_terms(query: str) -> List[str]:
    """Extract lightweight lexical anchors without a domain-specific router."""
    text = str(query or "").lower()
    terms: List[str] = re.findall(r"[a-z0-9][a-z0-9_.+-]{1,}", text)
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if chunk not in _QUERY_FILLER_TERMS and len(chunk) <= 10:
            terms.append(chunk)
        if len(chunk) > 2:
            terms.extend(chunk[index:index + 2] for index in range(len(chunk) - 1))
    seen = set()
    return [
        term for term in terms
        if term not in _QUERY_FILLER_TERMS
        and not (term in seen or seen.add(term))
    ][:32]


def _rank_items_for_query(items: List[dict], query: str) -> tuple[List[dict], str]:
    """Drop obvious search noise and expose evidence strength to the answerer.

    Provider scores describe search-engine rank, not whether a page proves the
    user's claim. Lexical coverage is deliberately conservative: weak pages may
    remain as leads, but are explicitly marked weak and can never justify
    “找到了” or a market-wide negative conclusion.
    """
    terms = _query_terms(query)
    if not terms:
        return list(items or []), "weak"
    ranked = []
    for position, raw in enumerate(items or []):
        item = dict(raw or {})
        title = re.sub(r"\s+", "", str(item.get("title") or "").lower())
        body = title + re.sub(r"\s+", "", str(item.get("snippet") or "").lower())
        weighted = 0.0
        total = 0.0
        for term in terms:
            compact = re.sub(r"\s+", "", term)
            weight = min(4.0, max(1.0, len(compact) / 2.0))
            total += weight * 2.0
            if compact and compact in title:
                weighted += weight * 2.0
            elif compact and compact in body:
                weighted += weight
        lexical = weighted / total if total else 0.0
        try:
            provider_score = max(0.0, min(1.0, float(item.get("score") or 0)))
        except (TypeError, ValueError):
            provider_score = 0.0
        relevance = lexical * 0.9 + provider_score * 0.1
        item["relevance"] = round(relevance, 3)
        item["_position"] = position
        ranked.append(item)
    ranked.sort(key=lambda row: (-float(row.get("relevance") or 0), row.get("_position", 0)))
    useful = [row for row in ranked if float(row.get("relevance") or 0) >= 0.16]
    for row in useful:
        row.pop("_position", None)
    top = float(useful[0].get("relevance") or 0) if useful else 0.0
    quality = "strong" if top >= 0.62 else "medium" if top >= 0.34 else "weak"
    return useful, quality


def _extract_tavily(
    api_key: str,
    urls: List[str],
    *,
    query: str = "",
    include_images: bool = True,
) -> dict:
    """Batch extract via Tavily. Returns {url: page_dict}."""
    if not urls:
        return {}
    payload = {
        "urls": urls[:20],
        "include_images": bool(include_images),
        "extract_depth": "advanced",
        "format": "markdown",
    }
    if query:
        payload["query"] = query
        payload["chunks_per_source"] = 5
    with httpx.Client(timeout=httpx.Timeout(45.0, connect=5.0)) as client:
        response = client.post(
            "https://api.tavily.com/extract",
            json=payload,
            headers={
                "Authorization": "Bearer %s" % api_key,
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()
    by_url = {}
    for row in data.get("results") or []:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        raw = (row.get("raw_content") or "").strip()
        paragraphs = _paragraphs_from_text(raw)
        title = ""
        if paragraphs:
            first = paragraphs[0]["text"]
            if len(first) < 80:
                title = first
        site = urlparse(url).hostname or ""
        summary = ""
        for p in paragraphs:
            if p.get("tag") == "p" and len(p.get("text") or "") > 40:
                summary = p["text"][:400]
                break
        if not summary:
            summary = (raw[:400] if raw else "")
        by_url[_norm_url(url) or url] = {
            "ok": bool(raw),
            "url": url,
            "title": title or site or url,
            "site": site,
            "lead": summary,
            "text": raw[:8000],
            "summary": summary,
            "images": _norm_images(row.get("images") or []),
            "paragraphs": paragraphs[:40],
            "error": "" if raw else "未能提取正文",
            "extractor": "tavily",
        }
    for row in data.get("failed_results") or []:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        by_url[_norm_url(url) or url] = {
            "ok": False,
            "url": url,
            "title": "",
            "site": urlparse(url).hostname or "",
            "lead": "",
            "text": "",
            "summary": "",
            "images": [],
            "paragraphs": [],
            "error": row.get("error") or "提取失败",
            "extractor": "tavily",
        }
    return by_url


def _fetch_page_local(url: str, timeout: int = 12) -> dict:
    result = web_reader.extract_reader(url, timeout=timeout, include_images=True)
    if not result.get("ok"):
        return {
            "ok": False,
            "url": url,
            "error": result.get("error") or "读取失败",
            "title": result.get("title") or "",
            "site": result.get("site") or "",
            "lead": "",
            "text": "",
            "summary": "",
            "images": [],
            "paragraphs": [],
            "extractor": "local",
        }
    full = (result.get("full_text") or "").strip()
    lead = (result.get("lead") or "").strip()
    paragraphs = result.get("paragraphs") or []
    summary = lead or (paragraphs[0]["text"] if paragraphs else full[:280])
    return {
        "ok": True,
        "url": result.get("url") or url,
        "title": result.get("title") or "",
        "site": result.get("site") or "",
        "lead": lead,
        "text": full[:8000],
        "summary": (summary or "")[:400],
        "images": _norm_images(result.get("images") or []),
        "paragraphs": paragraphs[:40],
        "error": "",
        "extractor": "local",
    }


def fetch_page_contents(
    urls: List[str],
    *,
    cfg: dict,
    query: str = "",
    include_images: bool = True,
) -> List[dict]:
    """Prefer Tavily Extract, fall back to local reader per URL."""
    urls = [u for u in urls if u]
    if not urls:
        return []
    by_url = {}
    use_extract = bool(cfg.get("use_extract", True))
    agentsearch_url = (cfg.get("agentsearch_url") or "").strip()
    provider = (cfg.get("provider") or "").lower()
    # 自托管抽取优先：无 key、正文降级策略比 Tavily Extract 更完整
    if use_extract and agentsearch_url and provider in ("agentsearch", "both"):
        try:
            by_url = _extract_agentsearch(agentsearch_url, urls)
        except Exception:
            by_url = {}
    tavily_key = cfg.get("tavily_api_key") or ""
    if use_extract and not by_url and not _is_placeholder(tavily_key):
        try:
            by_url = _extract_tavily(
                tavily_key, urls, query=query, include_images=include_images
            )
        except Exception:
            by_url = {}

    pages = []
    for url in urls:
        key = _norm_url(url) or url
        page = by_url.get(key)
        if page and page.get("ok"):
            if not include_images:
                page = dict(page)
                page["images"] = []
            pages.append(page)
            continue
        # fallback local
        local = _fetch_page_local(url)
        if not include_images:
            local["images"] = []
        if page and not local.get("ok"):
            pages.append(page)
        else:
            pages.append(local)
    return pages


def extract(
    url: str,
    *,
    cfg: Optional[dict] = None,
    get_setting=None,
    query: str = "",
    include_images: bool = True,
) -> dict:
    """Extract a single URL (Tavily Extract → local fallback)."""
    if cfg is None:
        if get_setting is None:
            raise ValueError("cfg or get_setting required")
        cfg = load_config(get_setting)
    target = (url or "").strip()
    if not web_reader.is_safe_url(target):
        return {
            "ok": False,
            "url": target,
            "error": "无效的网页地址",
            "title": "",
            "text": "",
            "summary": "",
            "images": [],
            "paragraphs": [],
        }
    pages = fetch_page_contents(
        [target], cfg=cfg, query=query, include_images=include_images
    )
    return pages[0] if pages else {
        "ok": False,
        "url": target,
        "error": "提取失败",
        "title": "",
        "text": "",
        "summary": "",
        "images": [],
        "paragraphs": [],
    }


def _build_summary(
    query: str,
    provider_answers: List[str],
    items: List[dict],
    pages: List[dict],
) -> str:
    for answer in provider_answers:
        if answer and len(answer.strip()) > 20:
            return answer.strip()[:900]
    bits = []
    for page in pages:
        if page.get("ok") and page.get("summary"):
            bits.append(page["summary"][:180])
        if len(bits) >= 3:
            break
    if bits:
        return "关于「%s」：%s" % (query, "；".join(bits))[:900]
    if items:
        titles = "、".join((it.get("title") or "")[:40] for it in items[:4])
        return "检索到相关来源：%s。" % titles
    return ""


def _build_answer_context(
    query: str,
    items: List[dict],
    pages: List[dict],
    provider_answers: List[str],
    summary: str,
    images: List[dict],
    evidence_quality: str = "weak",
    max_chars: int = 7500,
    grounding: str = "",
) -> str:
    compact = max_chars <= 2800
    # Weak candidates remain in internal result metadata for diagnostics, but
    # are intentionally withheld from the answering model. Otherwise a model
    # can turn a nearby tutorial or similarly named product into a plausible
    # recommendation even after reading the warning above it.
    answer_items = [] if evidence_quality == "weak" else items
    answer_pages = [] if evidence_quality == "weak" else pages
    answer_images = [] if evidence_quality == "weak" else images
    lines = ["【联网搜索结果】", "查询：%s" % query]
    if evidence_quality == "weak" and grounding == "helpful":
        # 原理/做法这类问题本来就不靠检索定论。查不到时让模型照常用自己的知识
        # 回答，只是别把没查到的东西说成查到了——而不是憋出一句「不能下结论」。
        lines.append(
            "证据状态：本轮没检索到有用的资料。这个问题不依赖公开证据，"
            "直接用你自己的知识正常回答，别说「不能下结论」「没有明确确认」这类话，"
            "也不要提检索失败。只是不能凭空说出某个具体来源、链接或数字。"
        )
    elif evidence_quality == "weak":
        lines.append(
            "证据状态：本轮只得到弱相关线索，没有找到明确匹配。"
            "只能说『本轮没找到』，不能说目标不存在，也不能把相近产品/教程说成『找到了』。"
            "但不要就此收口：把下面的线索挑 2~3 条按标题讲给用户，"
            "说清它们只是可能的方向、没有完全对上，再问他要不要打开看看。"
            "只回一句『没有明确确认』而不给任何线索，属于没完成任务。"
        )
        # 弱证据不等于什么都不能说。此前这里把线索整个清空，用户听到的永远是
        # 一句「本轮没有明确确认」，既没内容也没下一步——问得越具体越是这样，
        # 因为长查询几乎不可能被单页完整覆盖。
        # 现在把线索的标题与来源交给模型（URL 仍然不给，防止把相近页说成找到了，
        # 也不给它编链接的机会）；要打开就用 result.N 引用，真实链接在服务端。
        if items:
            lines.append("")
            lines.append(
                "弱相关线索（只能当作可能方向介绍，必须说明没有完全对上；"
                "用户想看某一条时用 object_control invoke target=result.N command=open）："
            )
            for index, item in enumerate(items[:5], 1):
                site = ""
                try:
                    site = urlparse(str(item.get("url") or "")).hostname or ""
                except ValueError:
                    site = ""
                lines.append(
                    "result.%d %s%s"
                    % (
                        index,
                        str(item.get("title") or "无标题")[:80],
                        ("（来源 %s）" % site.replace("www.", "")) if site else "",
                    )
                )
                snippet = str(item.get("snippet") or "").strip()
                if snippet:
                    lines.append("   线索摘要：%s" % snippet[:120])
    elif evidence_quality == "medium":
        lines.append(
            "证据状态：有相关候选，但关键条件未完全被来源明确确认。"
            "回答必须指出缺少哪项确认，不能把候选冒充完全匹配。"
        )
    else:
        lines.append("证据状态：来源与查询条件高度匹配，仍只陈述来源明确支持的事实。")
    if summary:
        lines.append("综合摘要：%s" % summary[:(500 if compact else 900)])
    lines.append("")
    lines.append("来源链接（回答时请引用）：")
    for index, item in enumerate(answer_items, 1):
        lines.append("%d. %s" % (index, item.get("title") or "无标题"))
        if item.get("url"):
            lines.append("   链接：%s" % item["url"])
        if item.get("date") and not compact:
            lines.append("   日期：%s" % item["date"])
        if item.get("snippet"):
            lines.append(
                "   摘要：%s" % item["snippet"][:(160 if compact else 320)]
            )
    if answer_pages:
        lines.append("")
        lines.append("原文摘录：")
        for index, page in enumerate(answer_pages, 1):
            if not page.get("ok"):
                continue
            lines.append("%d. %s" % (index, page.get("title") or page.get("url")))
            if page.get("url"):
                lines.append("   链接：%s" % page["url"])
            excerpt = page.get("summary") or (page.get("text") or "")[:500]
            if excerpt:
                lines.append("   正文：%s" % excerpt[:500])
            if page.get("images"):
                lines.append(
                    "   配图：%s"
                    % "；".join(img["url"] for img in page["images"][:3])
                )
    if answer_images:
        lines.append("")
        lines.append(
            "相关图片：%s" % "；".join(img["url"] for img in answer_images[:6])
        )
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n…(已截断)"
    return text


def _panel_pages(pages: List[dict]) -> List[dict]:
    out = []
    for p in pages:
        out.append({
            "url": p.get("url"),
            "title": p.get("title"),
            "site": p.get("site") or "",
            "summary": p.get("summary") or "",
            "images": p.get("images") or [],
            "paragraphs": p.get("paragraphs") or [],
            "full_text": (p.get("text") or "")[:6000],
            "ok": bool(p.get("ok")),
            "extractor": p.get("extractor") or "",
            "error": p.get("error") or "",
        })
    return out


def search_images(query: str, *, cfg: Optional[dict] = None, get_setting=None,
                  limit: int = 9) -> dict:
    """用户想看图时直接查图片索引，而不是拿网页结果凑合。

    以前所有请求都走网页检索，「显示一张故宫的图片」被当成网页查询，
    自然搜不到可展示的图——图片只是网页结果的副产品。意图既然是看图，
    检索就该落在图片索引上。
    """
    started = time.perf_counter()
    if cfg is None:
        cfg = load_config(get_setting) if get_setting else dict(DEFAULTS)
    topic = str(query or "").strip()
    images = _search_images_searxng(cfg.get("searxng_url") or "", topic, limit=limit)
    return {
        "ok": bool(images),
        "query": topic,
        "want": "images",
        "summary": "",
        "items": [],
        "images": images,
        "pages": [],
        "evidence_quality": "strong" if images else "weak",
        "answerable": bool(images),
        "answer_context": (
            "【图片检索结果】查询：%s\n已取回 %d 张图片，直接展示即可；"
            "不要罗列网页链接，也不要说找不到。" % (topic, len(images))
            if images else
            "【图片检索结果】查询：%s\n这次没取到可展示的图片。" % topic
        ),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


# 上文里的型号会被顺手粘进查询词：用户只问「洞洞板下走飞线可不可以」，
# 查询却变成「ESP32-C3 Super Mini 洞洞板 飞线接线 注意事项」——这种长查询
# 几乎不可能被任何单页完整覆盖，必然扑空。扑空时退回主题词再搜一次，
# 而不是把「没查到」直接甩给用户。
_GENERIC_TAIL = (
    "注意事项", "注意", "教程", "方法", "步骤", "建议", "技巧", "指南",
    "可不可以", "行不行", "怎么样", "怎么办", "是否可行",
    "tips", "guide", "notes", "howto", "tutorial",
)


# 「今天」对搜索引擎没有意义：实测「今天的重要新闻」0 条，把它换成
# 具体日期「2026年8月25日 新闻」立刻是 strong/4 条。这不是新闻专用——
# 任何带相对时间的问题（今天发布会、今天股市）都吃这一套。
_TODAY_WORDS = ("今天", "今日", "本日", "现在", "最新", "近期", "这两天")


def _dateify_query(query: str) -> str:
    """把相对时间换成具体日期；没有相对时间词则返回空串。"""
    import datetime

    text = str(query or "").strip()
    if not text or not any(word in text for word in _TODAY_WORDS):
        return ""
    today = datetime.date.today()
    stamp = "%d年%d月%d日" % (today.year, today.month, today.day)
    out = text
    for word in _TODAY_WORDS:
        out = out.replace(word, stamp)
    # 「的」这类连接词粘在日期后面会伤检索：2026年8月25日的重要新闻 → 加个空格
    out = re.sub(r"(\d日)的?", r"\1 ", out).strip()
    out = re.sub(r"\s+", " ", out)
    return out if out != text else ""


def _broaden_query(query: str) -> str:
    """把被上文撑长的查询收回到主题词；无法收窄时返回空串。"""
    tokens = [token for token in str(query or "").split() if token]
    if len(tokens) < 2:
        return ""
    cjk = [token for token in tokens if any(ord(char) > 0x2E80 for char in token)]
    core = cjk if cjk else list(tokens)
    # 末尾的泛化词（注意事项/教程…）对检索没有区分度，先摘掉
    while len(core) > 1 and core[-1].lower() in _GENERIC_TAIL:
        core.pop()
    if len(core) > 3:
        core = core[-3:]
    broadened = " ".join(core).strip()
    return broadened if broadened and broadened != str(query or "").strip() else ""


def search(
    query: str,
    *,
    cfg: Optional[dict] = None,
    get_setting=None,
    max_results: Optional[int] = None,
    fetch_pages: Optional[int] = None,
    include_images: Optional[bool] = None,
    query_variants: Optional[List[str]] = None,
    profile: str = "full",
    grounding: str = "",
) -> dict:
    """profile=voice：语音快路径（basic/单查询/不抽正文）；full：完整检索。"""
    started = time.perf_counter()
    if cfg is None:
        if get_setting is None:
            raise ValueError("cfg or get_setting required")
        cfg = load_config(get_setting)

    voice_fast = str(profile or "full").lower() in ("voice", "fast", "realtime")
    q = (query or "").strip()
    empty = {
        "ok": False,
        "query": q,
        "queries": [q] if q else [],
        "summary": "",
        "answer_context": "",
        "items": [],
        "pages": [],
        "images": [],
        "links": [],
        "sources": [],
        "provider_answers": [],
        "evidence_quality": "weak",
        "answerable": False,
        "elapsed_ms": 0,
        "error": "",
        "panel": None,
        "profile": "voice" if voice_fast else "full",
    }
    if not q:
        empty["answer_context"] = "请提供搜索关键词。"
        empty["error"] = "empty_query"
        return empty

    if not cfg.get("enabled", True):
        empty["answer_context"] = "网页搜索技能已关闭。"
        empty["error"] = "disabled"
        empty["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return empty

    providers = _providers_ready(cfg)
    if not providers:
        empty["answer_context"] = (
            "联网搜索未配置可用 API Key。"
            "请在设置 → 技能 → 网页搜索中填写 Tavily 或 Metaso 密钥。"
        )
        empty["error"] = "no_provider"
        empty["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return empty

    if voice_fast:
        # 语音目标：先开口；摘要+snippet 够用，跳过抽正文/多查询/配图
        limit = int(max_results or min(4, int(cfg.get("max_results") or 4)))
        page_n = int(fetch_pages if fetch_pages is not None else 0)
        want_images = bool(include_images) if include_images is not None else False
        queries = rewrite_queries(q)[:1]
        depth = "basic"
        answer_mode = True
        context_max = 2200
    else:
        limit = int(max_results or cfg.get("max_results") or 6)
        page_n = int(fetch_pages if fetch_pages is not None else cfg.get("fetch_pages") or 3)
        want_images = (
            bool(cfg.get("include_images", True))
            if include_images is None
            else bool(include_images)
        )
        planned = [q, *(query_variants or []), *rewrite_queries(q)]
        queries = []
        seen_queries = set()
        for candidate in planned:
            value = re.sub(r"\s+", " ", str(candidate or "")).strip()
            key = value.lower()
            if not value or key in seen_queries:
                continue
            seen_queries.add(key)
            queries.append(value)
            if len(queries) >= 3:
                break
        depth = cfg.get("search_depth") or "advanced"
        answer_mode = "advanced"
        context_max = 7500

    batches = []
    errors = []
    def run_provider(name: str, use_query: str):
        try:
            if name == "agentsearch":
                return _search_agentsearch(
                    cfg.get("agentsearch_url") or "",
                    use_query,
                    limit,
                    include_images=want_images,
                    searxng_url=cfg.get("searxng_url") or "",
                )
            if name == "tavily":
                return _search_tavily(
                    cfg["tavily_api_key"],
                    use_query,
                    limit,
                    depth=depth,
                    include_images=want_images,
                    include_answer=answer_mode,
                )
            if name == "metaso":
                return _search_metaso(cfg["metaso_api_key"], use_query, limit)
        except Exception as exc:
            errors.append("%s: %s" % (name, str(exc)[:160]))
        return None

    # quick voice：主 provider + 单查询；thorough/full：每个 provider 同时执行
    # 最多三个互补查询。它仍是一次工具事务，不把重搜循环暴露给模型或用户。
    run_list = providers[:1] if voice_fast else providers
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for name in run_list:
            for use_q in (queries[:1] if voice_fast else queries[:3]):
                futures.append(pool.submit(run_provider, name, use_q))
        for fut in concurrent.futures.as_completed(futures):
            batch = fut.result()
            if batch:
                batches.append(batch)

    raw_items = _merge_items(batches, max(limit * 3, limit))
    items, evidence_quality = _rank_items_for_query(raw_items, q)
    if evidence_quality == "weak":
        broadened = _dateify_query(q) or _broaden_query(q)
        if broadened:
            retry_batches = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                for fut in concurrent.futures.as_completed(
                    [pool.submit(run_provider, name, broadened) for name in run_list]
                ):
                    batch = fut.result()
                    if batch:
                        retry_batches.append(batch)
            if retry_batches:
                retry_items, retry_quality = _rank_items_for_query(
                    _merge_items(retry_batches, max(limit * 3, limit)), broadened,
                )
                if retry_quality != "weak":
                    batches.extend(retry_batches)
                    items, evidence_quality, q = retry_items, retry_quality, broadened
                    queries.append(broadened)
    # Weak matches are only leads, not an answer. Keep at most two so neither
    # the answer context nor the collapsed source area turns into a search dump.
    # Fetching their full pages adds latency without improving confidence.
    items = items[: (min(limit, 2) if evidence_quality == "weak" else limit)]
    if evidence_quality == "weak":
        page_n = 0
    provider_answers = [b.get("answer") or "" for b in batches if b.get("answer")]
    sources = sorted({b.get("provider") for b in batches if b.get("provider")})
    images = []
    seen_img = set()
    for batch in batches:
        for img in batch.get("images") or []:
            u = img.get("url")
            if u and u not in seen_img:
                seen_img.add(u)
                images.append(img)

    pages: List[dict] = []
    if page_n > 0 and items:
        urls = [it["url"] for it in items[:page_n] if it.get("url")]
        pages = fetch_page_contents(
            urls, cfg=cfg, query=q, include_images=want_images
        )
        for page in pages:
            for img in page.get("images") or []:
                u = img.get("url")
                if u and u not in seen_img:
                    seen_img.add(u)
                    images.append(img)

    # Image providers often return a generic object that merely shares one
    # keyword. Showing that beside an unanswerable result makes the candidate
    # look verified, so weak searches deliberately reveal no image.
    images = [] if evidence_quality == "weak" else images[: (4 if voice_fast else 12)]
    summary = _build_summary(q, provider_answers, items, pages)
    if evidence_quality == "weak":
        summary = "没找到对得上的资料。"
    answer_context = _build_answer_context(
        q,
        items,
        pages,
        provider_answers,
        summary,
        images,
        evidence_quality=evidence_quality,
        max_chars=context_max,
        grounding=grounding,
    )
    if not items:
        answer_context = (
            "未找到相关搜索结果。"
            + ((" 错误：" + "；".join(errors)) if errors else "")
        )

    links = [
        {
            "title": it.get("title"),
            "url": it.get("url"),
            "snippet": it.get("snippet") or "",
            "favicon": it.get("favicon") or "",
        }
        for it in items
        if it.get("url")
    ]

    panel_pages = _panel_pages(pages) if not voice_fast else []
    panel = {
        "kind": "search",
        "title": "搜索：%s" % q,
        "data": {
            "query": q,
            "summary": summary,
            "items": [
                {
                    "title": it.get("title"),
                    "snippet": it.get("snippet"),
                    "date": it.get("date") or "",
                    "url": it.get("url"),
                    "favicon": it.get("favicon") or "",
                }
                for it in items
            ],
            "links": links,
            "images": images,
            "pages": panel_pages,
        },
        "width": 520,
        "height": 520,
    }

    return {
        "ok": bool(items),
        "query": q,
        "queries": queries,
        "summary": summary,
        "answer_context": answer_context,
        "items": items,
        "pages": pages,
        "images": images,
        "links": links,
        "sources": sources,
        "evidence_quality": evidence_quality,
        "answerable": evidence_quality in {"medium", "strong"},
        "provider_answers": provider_answers,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "error": ("; ".join(errors) if errors and not items else ""),
        "panel": panel,
        "profile": "voice" if voice_fast else "full",
        "fingerprint": hashlib.sha1(
            (q + "|" + "|".join(it.get("url") or "" for it in items[:5])).encode("utf-8")
        ).hexdigest()[:12],
    }


def tool_definition(*, slim=False) -> dict:
    if slim:
        return {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "联网搜索：检索网页+摘要+配图，回答带来源链接，"
                    "不要凭记忆编造近期事件。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词或完整问题"},
                    },
                    "required": ["query"],
                },
            },
        }
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "强力联网搜索（Tavily）：检索网页、生成综合摘要、抽取原文与配图。"
                "不要凭训练记忆编造近期事件。回答时带上来源链接。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或完整问题（中文优先）",
                    }
                },
                "required": ["query"],
            },
        },
    }


def extract_tool_definition(*, slim=False) -> dict:
    if slim:
        return {
            "type": "function",
            "function": {
                "name": "web_extract",
                "description": (
                    "打开并提取指定网页正文/摘要/配图。"
                    "用户已给 URL 且要「打开/看看/总结这个网页」时用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "http(s) 网页地址"},
                        "question": {"type": "string", "description": "想从该页了解什么（可选）"},
                    },
                    "required": ["url"],
                },
            },
        }
    return {
        "type": "function",
        "function": {
            "name": "web_extract",
            "description": (
                "打开并提取指定网页：正文、摘要、配图。"
                "用户说「打开这个链接/看看这篇/第N条详情/总结这个网页」且已有 URL 时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "http(s) 网页地址",
                    },
                    "question": {
                        "type": "string",
                        "description": "想从该页了解什么（可选，用于聚焦摘录）",
                    },
                },
                "required": ["url"],
            },
        },
    }


def format_tool_result(result: dict) -> str:
    if not result:
        return "搜索失败：空结果"
    text = result.get("answer_context") or ""
    if result.get("error") and not result.get("ok"):
        return text or ("搜索失败：%s" % result["error"])
    return text


def format_extract_result(page: dict) -> str:
    if not page:
        return "网页提取失败：空结果"
    if not page.get("ok"):
        return "网页提取失败：%s" % (page.get("error") or "未知错误")
    lines = [
        "【网页提取】",
        "标题：%s" % (page.get("title") or "无标题"),
        "链接：%s" % (page.get("url") or ""),
    ]
    if page.get("summary"):
        lines.append("摘要：%s" % page["summary"][:500])
    body = (page.get("text") or "")[:3500]
    if body:
        lines.append("正文：\n%s" % body)
    if page.get("images"):
        lines.append(
            "配图：%s" % "；".join(img["url"] for img in page["images"][:5])
        )
    return "\n".join(lines)


def apply_config_update(get_setting, set_setting, payload: dict) -> dict:
    current = load_config(get_setting)
    data = dict(payload or {})

    for key in ("enabled", "use_extract", "include_images"):
        if key in data:
            current[key] = bool(data[key])
            set_setting(SETTING_PREFIX + key, "1" if current[key] else "0")

    if "provider" in data:
        provider = str(data["provider"] or "agentsearch").lower().strip()
        if provider not in ("agentsearch", "tavily", "metaso", "both"):
            provider = "agentsearch"
        current["provider"] = provider
        set_setting(SETTING_PREFIX + "provider", provider)

    for key in ("agentsearch_url", "searxng_url"):
        if key in data:
            value = str(data[key] or "").strip()
            current[key] = value
            set_setting(SETTING_PREFIX + key, value)

    for key in ("max_results", "fetch_pages"):
        if key in data:
            try:
                lo = 0 if key == "fetch_pages" else 1
                current[key] = max(lo, min(12, int(data[key])))
            except Exception:
                pass
            set_setting(SETTING_PREFIX + key, str(current[key]))

    if "search_depth" in data:
        depth = str(data["search_depth"] or "advanced").lower()
        if depth not in ("basic", "advanced", "fast", "ultra-fast"):
            depth = "advanced"
        current["search_depth"] = depth
        set_setting(SETTING_PREFIX + "search_depth", depth)

    for key in ("tavily_api_key", "metaso_api_key"):
        if key not in data:
            continue
        text = str(data[key] or "").strip()
        if not text or "••••" in text or text == "(unchanged)":
            continue
        current[key] = text
        set_setting(SETTING_PREFIX + key, text)

    return public_config(get_setting)
