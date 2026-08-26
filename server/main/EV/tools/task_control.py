# -*- coding: utf-8 -*-
"""Compact voice-facing entry for infrequent task capabilities.

The model sees one stable ``task_control`` capability instead of separate
realtime/search/extract/coding tools on every turn. ``kind`` is a typed route;
execution stays in app.py where the receipt-producing adapters already live.
"""
from __future__ import annotations


KINDS = (
    "current_time",
    "date",
    "weather",
    "web_search",
    "web_extract",
    "coding_clarify",
    "coding_plan",
    "coding_status",
    "coding_cancel",
    "coding_revert",
)


def tool_definition():
    return {
        "type": "function",
        "function": {
            "name": "task_control",
            "description": "联网、实时信息和写码任务；已有研究结果的呈现操作归 canvas_control。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "request": {
                        "type": "string",
                        "description": "用户完整原意。",
                    },
                    "location": {
                        "type": "string",
                    },
                    "url": {
                        "type": "string",
                    },
                    "research_depth": {
                        "type": "string",
                        "enum": ["quick", "thorough"],
                        "description": (
                            "仅 web_search：简单事实用 quick；小众对象、真假核实、"
                            "多来源对比或用户明确要求认真找时用 thorough。"
                        ),
                    },
                    "search_queries": {
                        "type": "array",
                        "maxItems": 3,
                        "items": {"type": "string"},
                        "description": (
                            "仅 thorough：按用户目标写 2-3 个互补查询词，"
                            "一次调用内部完成；不要把多轮搜索暴露给用户。"
                        ),
                    },
                    "want": {
                        "type": "string",
                        "enum": ["images", "list", "answer", "compare"],
                        "description": (
                            "先判断用户最终想看到什么再检索，别把他的原话直接当查询词：\n"
                            "images=他想看图（「显示一张故宫的图片」「随便给我张图」「长什么样」）→ 只出图；\n"
                            "list=他要若干个项目/方案/做法 → 先一句结论再逐条列出；\n"
                            "answer=他要一个事实（几点、多高、是什么）→ 一句话答完；\n"
                            "compare=他要多个对象的对比数据 → 出对比。\n"
                            "request 只写真正要检索的主题，不要把「显示一张」这类呈现要求写进去。"
                        ),
                    },
                    "grounding": {
                        "type": "string",
                        "enum": ["required", "helpful"],
                        "description": (
                            "这个回答离不离得开检索到的证据：\n"
                            "required=事实/型号/价格/新闻/链接，你自己不该拍脑袋，"
                            "查不到就只能说没查到；\n"
                            "helpful=原理、做法、能不能这么干这类你本来就会的问题，"
                            "检索只是补充——查不到就按自己的知识正常回答，不要说「不能下结论」。"
                        ),
                    },
                    "include_visuals": {
                        "type": "boolean",
                        "description": "仅图片确实帮助识别、比较或理解时为 true。",
                    },
                    "plan_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "risks": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "speak_while": {
                        "type": "boolean",
                        "description": (
                            "这次会让用户干等就填 true：联网搜索、读网页、写码都算。"
                            "本地即时动作（开关灯、开关窗口、查时间）填 false。"
                            "系统只在实际等待超过 1 秒才播，估高了也不会啰嗦。"
                        ),
                    },
                    "progress_reply": {
                        "type": "string",
                        "description": (
                            "speak_while=true 时必须填：结合用户这次问的内容临场写一句自然短话，"
                            "让他知道你在忙什么；false 时留空。要具体（提到他问的东西），"
                            "不要写‘好的我搜一下’这类通用套话，也不得提前声称已查到或已完成。"
                        ),
                    },
                    "continue_after": {
                        "type": "boolean",
                        "description": (
                            "回执后是否还有独立步骤。web_search 会自动新建/更新并显示研究画布；"
                            "仅要求显示、预览或放到画布时必须为 false。"
                        ),
                    },
                    "post_search_goal": {
                        "type": "string",
                        "description": (
                            "仅 web_search 且用户明确要求搜索后再做具体画布变换时填写，"
                            "例如‘聚焦第一张图片并全屏’。普通显示/预览留空；"
                            "留空时 continue_after=true 也不会继续动作。"
                        ),
                    },
                },
                "required": ["kind", "request", "continue_after"],
            },
        },
    }
