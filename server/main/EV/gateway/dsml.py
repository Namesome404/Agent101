# -*- coding: utf-8 -*-
"""DeepSeek V4 DSML tool-call 解析：API 偶发把工具调用留在 content 文本里。"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Tuple

# 官方形如 <｜DSML｜tool_calls>；线上偶见双竖线变体
_DSML = r"(?:｜){1,2}DSML(?:｜){1,2}"
_BLOCK_RE = re.compile(
    rf"<{_DSML}tool_calls>(.*?)</{_DSML}tool_calls>",
    re.DOTALL | re.IGNORECASE,
)
_INVOKE_RE = re.compile(
    rf"<{_DSML}invoke\s+name=\"([^\"]+)\">(.*?)</{_DSML}invoke>",
    re.DOTALL | re.IGNORECASE,
)
_PARAM_RE = re.compile(
    rf"<{_DSML}parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?\s*>"
    rf"(.*?)</{_DSML}parameter>",
    re.DOTALL | re.IGNORECASE,
)


def strip_dsml(text: str) -> str:
    if not text or "DSML" not in text:
        return text or ""
    cleaned = _BLOCK_RE.sub("", text)
    return cleaned.strip()


def parse_dsml_tool_calls(text: str) -> Tuple[List[Dict[str, Any]], str]:
    """从助手文本解析 DSML 工具调用。返回 (openai风格 tool_calls, 去掉 DSML 后的文本)。"""
    if not text or "DSML" not in text:
        return [], text or ""
    calls: List[Dict[str, Any]] = []
    for block in _BLOCK_RE.findall(text):
        for name, body in _INVOKE_RE.findall(block):
            args: Dict[str, Any] = {}
            for pname, is_string, raw in _PARAM_RE.findall(body):
                val = (raw or "").strip()
                if (is_string or "true").lower() == "true":
                    args[pname] = val
                else:
                    try:
                        args[pname] = json.loads(val)
                    except Exception:
                        args[pname] = val
            calls.append({
                "id": "dsml_" + uuid.uuid4().hex[:12],
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            })
    return calls, strip_dsml(text)


def extract_tool_calls(message) -> Tuple[List[Dict[str, Any]], str]:
    """优先用原生 tool_calls；否则尝试从 content 解 DSML。返回 (calls, visible_content)。"""
    content = getattr(message, "content", None) or ""
    native = getattr(message, "tool_calls", None) or []
    if native:
        out = []
        for tc in native:
            if hasattr(tc, "model_dump"):
                d = tc.model_dump()
            elif isinstance(tc, dict):
                d = tc
            else:
                d = {
                    "id": getattr(tc, "id", None) or ("call_" + uuid.uuid4().hex[:12]),
                    "type": "function",
                    "function": {
                        "name": getattr(getattr(tc, "function", None), "name", "") or "",
                        "arguments": getattr(getattr(tc, "function", None), "arguments", None) or "{}",
                    },
                }
            out.append(d)
        return out, strip_dsml(content)
    return parse_dsml_tool_calls(content)
