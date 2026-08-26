# -*- coding: utf-8 -*-
"""最近一次搜索结果的引用表：让模型按稳定 ID 打开结果，而不是自己写 URL。

存在的理由是一次真实事故：搜索判定为弱证据时，系统会（有意地、并有测试锁定）
把所有 URL 从模型上下文里扣掉，防止它把相近教程说成「找到了」。但用户接着说
「把那个链接打开」，模型手里一个真 URL 都没有，于是凭记忆编了一个——编出了
B 站那个著名的 rickroll BV 号。

扣 URL 是对的，让模型写 URL 是错的。这里保留真实链接在服务端，模型只引用
result.N；URL 不进它的上下文，它因此在物理上无法编造链接。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

_LOCK = threading.RLock()
_STATE: Dict[str, Any] = {"query": "", "items": [], "ts": 0.0}

MAX_ITEMS = 8


def _host(url: str) -> str:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def remember(query: str, items: Any) -> int:
    """记下本轮搜索结果。只保留能打开的（有 http(s) 链接的）条目。"""
    kept: List[Dict[str, str]] = []
    for raw in list(items or [])[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        kept.append({
            "title": str(raw.get("title") or "").strip()[:140] or _host(url),
            "url": url[:900],
            "snippet": str(raw.get("snippet") or raw.get("summary") or "").strip()[:200],
            "site": _host(url),
        })
    with _LOCK:
        _STATE["query"] = str(query or "")[:140]
        _STATE["items"] = kept
        _STATE["ts"] = time.time()
    return len(kept)


def snapshot() -> Dict[str, Any]:
    with _LOCK:
        return {
            "query": _STATE["query"],
            "items": [dict(item) for item in _STATE["items"]],
            "ts": _STATE["ts"],
        }


def get(index: int) -> Optional[Dict[str, str]]:
    """按 1 起的序号取结果；越界返回 None。"""
    try:
        position = int(index)
    except (TypeError, ValueError):
        return None
    with _LOCK:
        if 1 <= position <= len(_STATE["items"]):
            return dict(_STATE["items"][position - 1])
    return None


def count() -> int:
    with _LOCK:
        return len(_STATE["items"])


def clear() -> None:
    with _LOCK:
        _STATE["query"] = ""
        _STATE["items"] = []
        _STATE["ts"] = 0.0
