# -*- coding: utf-8 -*-
"""Anthropic Messages API → OpenAI-compatible LLM（供 Claude Code 使用）。"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple


def _as_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text") or "")
                elif block.get("type") == "thinking":
                    # keep thinking out of user-visible OpenAI content; Flash uses reasoning_content
                    continue
        return "".join(parts)
    return str(content)


def anthropic_tools_to_openai(tools: Optional[List[dict]]) -> Optional[List[dict]]:
    if not tools:
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or ""
        if not name:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description") or "",
                "parameters": t.get("input_schema") or t.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return out or None


def anthropic_messages_to_openai(body: dict) -> Tuple[List[dict], Optional[List[dict]], Dict[str, Any]]:
    """Convert Anthropic request → (openai_messages, openai_tools, extras)."""
    messages: List[dict] = []
    system = body.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = _as_text(system)
        if text.strip():
            messages.append({"role": "system", "content": text})

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")

        if role == "user":
            if isinstance(content, list):
                tool_results = []
                texts = []
                for block in content:
                    if not isinstance(block, dict):
                        texts.append(str(block))
                        continue
                    btype = block.get("type")
                    if btype == "tool_result":
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id") or block.get("id") or "",
                            "content": _as_text(block.get("content")),
                        })
                    elif btype == "text":
                        texts.append(block.get("text") or "")
                    else:
                        texts.append(_as_text([block]))
                if texts:
                    messages.append({"role": "user", "content": "".join(texts)})
                messages.extend(tool_results)
            else:
                messages.append({"role": "user", "content": _as_text(content)})
            continue

        if role == "assistant":
            if isinstance(content, list):
                texts = []
                tool_calls = []
                reasoning_parts = []
                for block in content:
                    if not isinstance(block, dict):
                        texts.append(str(block))
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        texts.append(block.get("text") or "")
                    elif btype == "thinking":
                        reasoning_parts.append(block.get("thinking") or block.get("text") or "")
                    elif btype == "tool_use":
                        tid = block.get("id") or ("toolu_%s" % uuid.uuid4().hex[:12])
                        args = block.get("input")
                        if not isinstance(args, (dict, list)):
                            args = {}
                        tool_calls.append({
                            "id": tid,
                            "type": "function",
                            "function": {
                                "name": block.get("name") or "",
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        })
                amsg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(texts) if texts else (None if tool_calls else ""),
                }
                if tool_calls:
                    amsg["tool_calls"] = tool_calls
                if reasoning_parts:
                    amsg["reasoning_content"] = "".join(reasoning_parts)
                messages.append(amsg)
            else:
                messages.append({"role": "assistant", "content": _as_text(content)})
            continue

        # pass-through rare roles
        messages.append({"role": role, "content": _as_text(content)})

    tools = anthropic_tools_to_openai(body.get("tools"))
    extras: Dict[str, Any] = {}
    # map tool_choice
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        ttype = tc.get("type")
        if ttype == "any":
            extras["tool_choice"] = "required"
        elif ttype == "auto":
            extras["tool_choice"] = "auto"
        elif ttype == "none":
            extras["tool_choice"] = "none"
        elif ttype == "tool" and tc.get("name"):
            extras["tool_choice"] = {
                "type": "function",
                "function": {"name": tc["name"]},
            }
    return messages, tools, extras


def openai_message_to_anthropic(message, model: str) -> dict:
    """Convert OpenAI chat completion message → Anthropic message object."""
    content_blocks: List[dict] = []
    text = getattr(message, "content", None)
    if text:
        content_blocks.append({"type": "text", "text": text})

    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning})

    tool_calls = getattr(message, "tool_calls", None) or []
    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", None) if fn else None
        raw_args = getattr(fn, "arguments", None) if fn else "{}"
        try:
            inp = json.loads(raw_args or "{}")
        except Exception:
            inp = {"_raw": raw_args}
        content_blocks.append({
            "type": "tool_use",
            "id": getattr(tc, "id", None) or ("toolu_%s" % uuid.uuid4().hex[:12]),
            "name": name or "",
            "input": inp if isinstance(inp, dict) else {"value": inp},
        })

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    stop_reason = "tool_use" if tool_calls else "end_turn"
    return {
        "id": "msg_%s" % uuid.uuid4().hex[:24],
        "type": "message",
        "role": "assistant",
        "model": model or "ev-gateway",
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _sse(event: str, data: dict) -> str:
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))


def anthropic_sse_from_message(msg: dict) -> Generator[str, None, None]:
    """Emit Anthropic-compatible SSE for a completed message (non-stream upstream)."""
    mid = msg.get("id") or ("msg_%s" % uuid.uuid4().hex[:24])
    model = msg.get("model") or "ev-gateway"
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": mid,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    for idx, block in enumerate(msg.get("content") or []):
        btype = block.get("type")
        if btype == "text":
            yield _sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            text = block.get("text") or ""
            if text:
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": text},
                })
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        elif btype == "thinking":
            yield _sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "thinking", "thinking": ""},
            })
            th = block.get("thinking") or ""
            if th:
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "thinking_delta", "thinking": th},
                })
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        elif btype == "tool_use":
            yield _sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": {},
                },
            })
            partial = json.dumps(block.get("input") or {}, ensure_ascii=False)
            yield _sse("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": partial},
            })
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": msg.get("stop_reason") or "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 0},
    })
    yield _sse("message_stop", {"type": "message_stop"})


def stream_openai_to_anthropic_sse(
    stream,
    model: str,
) -> Generator[str, None, None]:
    """Convert OpenAI chat completion stream → Anthropic SSE (best-effort)."""
    mid = "msg_%s" % uuid.uuid4().hex[:24]
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": mid,
            "type": "message",
            "role": "assistant",
            "model": model or "ev-gateway",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    text_index = 0
    text_started = False
    tool_meta: Dict[int, Dict[str, Any]] = {}
    # tool blocks start after text; assign indices dynamically
    next_index = 0
    stop_reason = "end_turn"

    try:
        for chunk in stream:
            choice = (chunk.choices or [None])[0]
            if not choice:
                continue
            delta = choice.delta
            if delta is None:
                continue
            # text
            piece = getattr(delta, "content", None)
            if piece:
                if not text_started:
                    text_started = True
                    text_index = next_index
                    next_index += 1
                    yield _sse("content_block_start", {
                        "type": "content_block_start",
                        "index": text_index,
                        "content_block": {"type": "text", "text": ""},
                    })
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": text_index,
                    "delta": {"type": "text_delta", "text": piece},
                })
            # tools
            tcs = getattr(delta, "tool_calls", None) or []
            for tc in tcs:
                idx = getattr(tc, "index", 0) or 0
                if idx not in tool_meta:
                    block_index = next_index
                    next_index += 1
                    tid = getattr(tc, "id", None) or ("toolu_%s" % uuid.uuid4().hex[:12])
                    fn = getattr(tc, "function", None)
                    name = getattr(fn, "name", None) if fn else ""
                    tool_meta[idx] = {
                        "block_index": block_index,
                        "id": tid,
                        "name": name or "",
                        "started": False,
                    }
                meta = tool_meta[idx]
                fn = getattr(tc, "function", None)
                if fn and getattr(fn, "name", None) and not meta["name"]:
                    meta["name"] = fn.name
                if getattr(tc, "id", None):
                    meta["id"] = tc.id
                if not meta["started"]:
                    meta["started"] = True
                    stop_reason = "tool_use"
                    yield _sse("content_block_start", {
                        "type": "content_block_start",
                        "index": meta["block_index"],
                        "content_block": {
                            "type": "tool_use",
                            "id": meta["id"],
                            "name": meta["name"],
                            "input": {},
                        },
                    })
                arg_piece = getattr(fn, "arguments", None) if fn else None
                if arg_piece:
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": meta["block_index"],
                        "delta": {"type": "input_json_delta", "partial_json": arg_piece},
                    })
            fr = getattr(choice, "finish_reason", None)
            if fr == "tool_calls":
                stop_reason = "tool_use"
            elif fr in ("stop", "length"):
                if stop_reason != "tool_use":
                    stop_reason = "end_turn" if fr == "stop" else "max_tokens"
    finally:
        if text_started:
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": text_index})
        for meta in tool_meta.values():
            if meta.get("started"):
                yield _sse("content_block_stop", {
                    "type": "content_block_stop",
                    "index": meta["block_index"],
                })
        yield _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 0},
        })
        yield _sse("message_stop", {"type": "message_stop"})


def resolve_agent_llm(db, agent_id: Optional[int]) -> Tuple[Optional[dict], Optional[str]]:
    """Return (llm_block, error)."""
    aid = agent_id
    if not aid:
        agents = db.list_agents() or []
        if not agents:
            return None, "没有可用智能体"
        aid = int(agents[0]["id"])
    agent = db.get_agent(aid)
    if not agent:
        return None, "智能体不存在：%s" % aid
    lm = (agent.get("modules") or {}).get("LLM") or {}
    name = lm.get("selected")
    if not name:
        return None, "智能体未选择 LLM"
    blk = dict(db.provider_catalog().get("LLM", {}).get(name, {}) or {})
    blk.update(lm.get("overrides") or {})
    if blk.get("type") not in (None, "", "openai"):
        return None, "网关仅支持 openai 兼容 LLM，当前：%s" % blk.get("type")
    key, url, model = blk.get("api_key"), blk.get("url"), blk.get("model_name")
    if not key or "你的" in str(key) or "请替换" in str(key):
        return None, "LLM 未配置 api_key"
    if not url or not model:
        return None, "LLM 缺少 url/model_name"
    blk["_agent_id"] = aid
    blk["_provider_name"] = name
    return blk, None


def deepseek_agent_extras(url: str, model: str, enable_thinking: bool = True) -> dict:
    """Flash-0731 agent 友好参数。"""
    u = str(url or "").lower()
    m = str(model or "").lower()
    if "deepseek" in u or m.startswith("deepseek"):
        if enable_thinking:
            return {
                "extra_body": {
                    "thinking": {"type": "enabled"},
                }
            }
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}
