# -*- coding: utf-8 -*-
"""
Muse 后端：核心契约层(server-base/agent-models/correct-words) + 管理 REST + TTS 统一逻辑 + 托管 UI。
端口 8002，核心契约挂在 /xiaozhi/*，鉴权 Authorization: Bearer <server.secret>。
"""
import asyncio
import contextlib
import copy
import os
import re
import json
import datetime
import hashlib
import secrets
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Body, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, Response, StreamingResponse
import uvicorn

from common.paths import (
    MUSE_DIR,
    UI_DIR,
)
from common.runtime import require_project_venv

require_project_venv()


def _load_muse_dotenv():
    """加载 EV/.env（不覆盖已有环境变量），便于 MUSE_LLM_TTFT_MS 等生效。"""
    path = MUSE_DIR / ".env"
    if not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip("\"'")
            if name and name not in os.environ:
                os.environ[name] = value
    except Exception:
        pass


_load_muse_dotenv()

from control_plane import database as db
from control_plane import dossier as dossier_lib
from control_plane import live_hub
from infrastructure.core_proxy import router as core_proxy_router
from tools import canvas_control
from tools import deep_search
from tools import device_control
from tools import object_control
from tools import surface_apps
from tools import surface_control
from tools import task_control
from coding import path_policy as coding_path_policy
from devices.coding import agent_runtime
from devices.coding import claude_code as claude_code_skill
from devices.coding import native_ui as coding_native_ui
from devices.coding import project_fsm as coding_fsm
from devices.coding import orchestrator as coding_orch
from devices.coding import diagrams as coding_diagrams
from devices.coding import run_memory as _coding_run_memory
from devices.coding import surface_tools
from control_plane import world_snapshot
from devices.coding import surfaces as surface_skill
from devices.coding import turn_trace as coding_turn_trace
from devices.coding.action_registry import ActionRegistry as _ActionRegistry
from devices.coding.scene_store import PROTOCOL_VERSION as SCENE_PROTOCOL_VERSION
from devices.coding.scene_store import scene_store
from devices.coding import surface_layout
from devices.desk import hub as desk_hub
from devices.desk import actions as desk_actions
from devices.desk import compose as desk_compose
from gateway import anthropic_messages as anthropic_gw
from gateway import dsml as dsml_gw

from app_shared import (
    AVATAR_VISUALIZER,
    DH_DIR,
    ESP_CLAW_FLASH_DIR,
    _SPEAKERS,
    _claude_code_base_url,
    _esp_claw_runtime_config,
    _external_base_url,
    _openai_client,
    _tcp_open,
    _resolve_avatar_model,
)
from routes_admin import router as admin_router
from routes_core import router as core_router
from routes_tts import router as tts_router
from routes_skills import router as skills_router
from routes_devices import router as devices_router
from routes_devices import _prewarm_agent


def _llm_ttft_budget_s(voice_mode: bool) -> float:
    """语音首包预算（秒）。超时则断流并切备用模型，避免卡在上游排队十几秒。"""
    raw = os.environ.get("MUSE_LLM_TTFT_MS")
    if raw is None or str(raw).strip() == "":
        # 文本聊天保持宽松；语音默认 2s（DeepSeek 排队时先让 failover 接，
        # 不要在主链上傻等 4s+）
        return 2.0 if voice_mode else 0.0
    try:
        ms = float(raw)
    except Exception:
        ms = 4000.0 if voice_mode else 0.0
    if not voice_mode and ms <= 0:
        return 0.0
    return max(0.0, ms / 1000.0)


def _llm_create_timeout_s(voice_mode: bool) -> float:
    """语音建连/拿流超时。DeepSeek 偶发卡在 create 本身，不能只等首包。"""
    if not voice_mode:
        return 90.0
    raw = os.environ.get("MUSE_LLM_CREATE_TIMEOUT_MS")
    if raw is None or str(raw).strip() == "":
        # 正常建连只有 100-400ms；给排队留 2.5s 余量即可，超了就切备用模型，
        # 不要在 DeepSeek 排队上干等 4s+。
        return max(2.5, _llm_ttft_budget_s(True) + 0.5)
    try:
        return max(1.0, float(raw) / 1000.0)
    except Exception:
        return 5.0


def _llm_stream_read_timeout_s() -> float:
    """流读取超时（SDK timeout，秒）。

    与 create 阶段预算解耦：create 建连/排队用 _llm_create_timeout_s 的短预算
    （线程池硬超时 + 竞争兜底），而 SDK timeout 同时作用于『流中途每个 token 的
    读取』。把它设成短值会误杀长回复——DeepSeek 生成几百字清单时 token 间隔偶尔
    超过 2.5s，触发 ReadTimeout 导致整轮失败（trace 里的 llm_error 根因）。
    这里给大值（默认 60s），只兜底真正卡死的流，不掐正常生成节奏。
    """
    raw = os.environ.get("MUSE_LLM_STREAM_READ_TIMEOUT_MS")
    try:
        return max(5.0, float(raw)) / 1000.0 if raw else 60.0
    except Exception:
        return 60.0


def _llm_block_for_provider(provider_name: str) -> dict:
    name = (provider_name or "").strip()
    if not name:
        return {}
    blk = dict(db.provider_catalog().get("LLM", {}).get(name, {}) or {})
    stored = (db.get_provider_configs().get("LLM") or {}).get(name) or {}
    if isinstance(stored, dict):
        blk.update(stored)
    return blk


# 备用模型黑名单：请求返回 403/401（key 失效/模型未开通）等明确不可用时记入，
# 后续回合直接跳过该 failover，避免每次排队都白等主链超时再撞一次错误。
_FAILOVER_BLACKLIST = set()
_FAILOVER_BLACKLIST_LOCK = threading.Lock()
_FAILOVER_BLACKLIST_TTL = 1800.0  # 30 分钟自动解禁，允许换 key 后恢复
_FAILOVER_BLACKLIST_AT = {}


def _blacklist_failover(name: str, reason: str):
    """把不可用的备用 provider 记入黑名单（带 TTL），打印一次原因。"""
    with _FAILOVER_BLACKLIST_LOCK:
        if name not in _FAILOVER_BLACKLIST:
            print(
                "[muse] 备用模型 %s 暂不可用，%.0fs 内跳过: %s"
                % (name, _FAILOVER_BLACKLIST_TTL, reason),
                flush=True,
            )
        _FAILOVER_BLACKLIST.add(name)
        _FAILOVER_BLACKLIST_AT[name] = time.time()


def _failover_blacklisted(name: str) -> bool:
    with _FAILOVER_BLACKLIST_LOCK:
        if name not in _FAILOVER_BLACKLIST:
            return False
        if time.time() - _FAILOVER_BLACKLIST_AT.get(name, 0) > _FAILOVER_BLACKLIST_TTL:
            _FAILOVER_BLACKLIST.discard(name)
            _FAILOVER_BLACKLIST_AT.pop(name, None)
            return False
        return True


def _clear_failover_blacklist():
    with _FAILOVER_BLACKLIST_LOCK:
        _FAILOVER_BLACKLIST.clear()
        _FAILOVER_BLACKLIST_AT.clear()


def _voice_llm_backups(primary_name: str, configured_backups=None):
    """语音动作流的备用 LLM 列表（串行兜底：主模型首包超时才切换）。

    configured_backups：设置界面配置的备用 provider 名字列表（可空）。
    为空时回退到环境变量 MUSE_VOICE_LLM_FAILOVER，再为空则无备用。
    注意：这里只做「超时后串行兜底」，绝不并行竞争。并行竞争已在
    2026-08-12 按用户要求停用（多模型同时发会带来不一致/机械切换）。
    串行兜底与竞争不同：主模型 2.5s 没出首包 → 切换备用 → 备用出结果，
    不会出现两条链同时生成、选谁播的不一致。
    """
    if not primary_name:
        return []
    names = []
    if configured_backups:
        names = [str(n).strip() for n in configured_backups if str(n).strip()]
    if not names:
        env_failover = (
            os.environ.get("MUSE_VOICE_LLM_FAILOVER", "") or ""
        ).strip()
        if env_failover and env_failover.lower() not in ("0", "off", "false", "no", "none"):
            names = [env_failover]
    backups = []
    seen = set()
    for failover in names:
        if failover == primary_name or failover in seen:
            continue
        seen.add(failover)
        if _failover_blacklisted(failover):
            continue
        blk = _llm_block_for_provider(failover)
        key = blk.get("api_key")
        url = blk.get("url")
        model = blk.get("model_name")
        if not key or "你的" in str(key) or "请替换" in str(key):
            continue
        if not url or not model:
            continue
        # catalog 缺 type 时按 openai 兼容处理
        if blk.get("type") not in (None, "", "openai"):
            continue
        backups.append((failover, blk))
    return backups


def _llm_request_overrides(voice_mode: bool, url: str, model: str) -> dict:
    if not voice_mode:
        return {}
    if (
        "api.deepseek.com" in str(url or "").lower()
        and str(model or "").lower().startswith("deepseek-v4")
    ):
        # include_usage：流式最后一帧带回 usage，其中有 prompt_cache_hit_tokens /
        # prompt_cache_miss_tokens。没有它，前缀缓存到底命中没有完全不可观测——
        # 「把静态段连成一片」这类改动就只能靠推断，没法验证。
        return {
            "extra_body": {"thinking": {"type": "disabled"}},
            "stream_options": {"include_usage": True},
        }
    if str(model or "").lower().startswith("qwen"):
        return {"extra_body": {"enable_thinking": False}}
    return {}


def _close_llm_stream(response) -> None:
    for attr in ("close", "http_response"):
        try:
            obj = getattr(response, attr, None)
            if callable(obj):
                obj()
            elif obj is not None and hasattr(obj, "close"):
                obj.close()
        except Exception:
            pass


def _llm_create_with_budget(active_client, budget_s, **kwargs):
    """create() 硬超时保护。

    DeepSeek 排队时 create() 可能阻塞十几秒（排队在服务端、响应头迟迟不来），
    而 SDK 的 timeout 在 stream 模式下对『建连后等响应头』阶段不总是生效。
    这里用线程池给 create() 本身套一个预算：超过即抛 TimeoutError 走 failover，
    不再傻等。超时后线程池 shutdown(wait=False)，后台线程继续等（最终连接
    由 SDK/httpx 自己超时回收），主流程立即切换备用模型。
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    if budget_s <= 0:
        return active_client.chat.completions.create(**kwargs)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(active_client.chat.completions.create, **kwargs)
    try:
        return future.result(timeout=budget_s)
    except FuturesTimeout:
        pool.shutdown(wait=False)
        raise TimeoutError("llm_ttft_timeout after %.0fms" % (budget_s * 1000))
    except Exception:
        pool.shutdown(wait=False)
        raise


_AUTO_MEMORY_LOCK = threading.Lock()

_MEMORY_CANDIDATE_RE = re.compile(
    r"(记住|别忘了|要记得|叫我|我的名字|"
    r"我是(?!觉得|想|说|问|在想)|"
    r"我喜欢|我不喜欢|我讨厌|我习惯|我通常|"
    r"我住在|我来自|我的(?:工作|职业|生日|家人|孩子|宠物|目标|计划)|"
    r"长期|以后都|每周|每天|经常|总是)"
)
_EXPLICIT_MEMORY_RE = (
    (re.compile(r"^记住[：:，,\s]*(.+)$"), lambda value: value),
    (re.compile(r"^别忘了[：:，,\s]*(.+)$"), lambda value: value),
    (re.compile(r"^要记得[：:，,\s]*(.+)$"), lambda value: value),
    (re.compile(r"^叫我[：:，,\s]*(.+)$"), lambda value: "用户叫" + value),
    (
        re.compile(r"^我的名字[是叫]?[：:，,\s]*(.+)$"),
        lambda value: "用户叫" + value,
    ),
)

_AUTO_MEMORY_PROMPT = """
你是长期记忆筛选器。根据历史记忆和本轮对话，输出更新后的长期记忆。
只保留未来多次对话仍有用的信息：稳定身份、明确且持续的偏好或习惯、
重要关系与宠物、长期项目与目标、未来确实需要继续跟进的承诺。
不要保存问候闲聊、一次性问题、新闻天气时间、临时情绪和状态、故事内容、
助手说的话、推测结论，以及密码、密钥、证件、银行卡和精确住址。
历史信息仍有效就保留，冲突时只保留较新的事实。
每条必须以“用户”开头，不超过60个汉字，最多8条。
没有值得保存的信息就保留原有有效记忆或输出空数组。
只输出合法JSON：
{"version":1,"items":[{"text":"用户喜欢简洁直接的回答","source":"auto"}]}
"""

_VOICE_PERSONA_SYSTEM = (
    "这是实时语音。严格服从最前面的智能体身份设定，不得把它稀释成熟人闲聊或网络陪伴助手。"
    "自然说话，不靠低沉声线、正式腔、书面腔或科幻台词制造人设。智能管家感来自具体行为："
    "回答问题先给结论；执行任务先做再按真实回执报告；发现歧义、风险或更省事的路径时，"
    "只主动指出最关键的一处；不知道就明确说不知道，不装懂。"
    "跟随用户当前使用的中文或英文以及自然程度，默认省略称呼，不强制使用『您』『先生』。"
    "句子完整、简洁但不生硬；说完即止，不主动推销能力，不用邀约或待命句收尾。"
    "禁止甜嗲、卖萌、过度热情、过度亲密和哄人语气；避免「我在呢」「当然可以」「没问题」"
    "「很高兴」等网络助手套话。禁止用任何邀约继续下指令的句子收尾，包括「有什么需要」"
    "「有什么直接说」「需要的话」「说一声」「告诉我」「交给我就行」「随时叫我」；"
    "内容答完就停。自我介绍只说明身份和工作原则，不罗列功能，也不邀请用户下指令。"
    "冷幽默只能在合适时顺手带一句：短、淡、一本正经，不解释笑点，不连续使用；"
    "安全、医疗、法律、坏消息或用户焦虑时完全不用。不要模仿或声称自己是任何影视角色。"
)


def _normalize_explicit_memory(text):
    value = (text or "").strip().strip("。．.!！?？ ")
    if not value or value.startswith("用户"):
        return value
    rules = (
        (re.compile(r"^我喜欢(.+)$"), "用户喜欢"),
        (re.compile(r"^我不喜欢(.+)$"), "用户不喜欢"),
        (re.compile(r"^我讨厌(.+)$"), "用户讨厌"),
        (re.compile(r"^我是(.+)$"), "用户是"),
        (re.compile(r"^我住在(.+)$"), "用户住在"),
        (re.compile(r"^我来自(.+)$"), "用户来自"),
        (re.compile(r"^我的(.+)$"), "用户的"),
        (re.compile(r"^我(.+)$"), "用户"),
    )
    for pattern, prefix in rules:
        match = pattern.match(value)
        if match:
            return prefix + (match.group(1) or "").strip()
    return value


def _explicit_memory_from_text(text):
    raw = (text or "").strip()
    for pattern, formatter in _EXPLICIT_MEMORY_RE:
        match = pattern.match(raw)
        if match:
            return _normalize_explicit_memory(
                formatter((match.group(1) or "").strip())
            )
    return ""


def _parse_memory_response(content):
    raw = (content or "").strip()
    if raw.startswith("```"):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        raw = match.group(0) if match else ""
    if not raw:
        return []
    return db._raw_to_items(json.loads(raw))


def _consider_agent_memory(aid, llm_block, user_text, assistant_text):
    """Legacy bullet memory + structured dossier updater."""
    with _AUTO_MEMORY_LOCK:
        explicit = _explicit_memory_from_text(user_text)
        if explicit:
            db.add_agent_memory_item(aid, explicit, source="explicit")
            print("[muse] 已保存明确记忆:", explicit, flush=True)

        dossier = db.get_agent_dossier(aid) or dossier_lib.empty_dossier()
        need_dossier = dossier_lib.should_update_dossier_with_state(
            user_text, assistant_text, dossier
        )
        need_legacy = bool(_MEMORY_CANDIDATE_RE.search(user_text or ""))
        if not need_dossier and not need_legacy:
            return
        try:
            client = _openai_client(
                llm_block.get("url"),
                llm_block.get("api_key"),
            )
            if need_dossier:
                response = client.chat.completions.create(
                    model=llm_block.get("model_name"),
                    messages=[
                        {"role": "system", "content": dossier_lib.UPDATER_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                "当前时间：%s\n\n当前档案：\n%s\n\n本轮用户：%s\n本轮助手：%s"
                                % (
                                    datetime.datetime.now().strftime(
                                        "%Y-%m-%d %H:%M %A"
                                    ),
                                    json.dumps(dossier, ensure_ascii=False),
                                    user_text,
                                    assistant_text,
                                )
                            ),
                        },
                    ],
                    temperature=0,
                    max_tokens=700,
                    timeout=45,
                )
                patch = dossier_lib.parse_updater_response(
                    response.choices[0].message.content
                )
                if patch:
                    merged = db.patch_agent_dossier(aid, patch)
                    if merged is not None:
                        print("[muse] 档案已更新", flush=True)

            if need_legacy and not explicit:
                existing = db.get_agent_memory_items(aid) or []
                response = client.chat.completions.create(
                    model=llm_block.get("model_name"),
                    messages=[
                        {"role": "system", "content": _AUTO_MEMORY_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                "历史记忆：\n%s\n\n本轮用户：%s\n本轮助手：%s"
                                % (
                                    db.memory_items_to_prompt(existing) or "（无）",
                                    user_text,
                                    assistant_text,
                                )
                            ),
                        },
                    ],
                    temperature=0,
                    max_tokens=400,
                    timeout=45,
                )
                incoming = _parse_memory_response(
                    response.choices[0].message.content
                )
                pinned = [
                    item for item in existing
                    if item.get("source") in ("manual", "explicit")
                ]
                for item in incoming:
                    item["source"] = "auto"
                db.set_agent_memory_items(aid, pinned + incoming)
                print(
                    "[muse] 自动记忆筛选完成: %d 条" % len(incoming),
                    flush=True,
                )
        except Exception as error:
            print("[muse] 档案/记忆更新失败:", error, flush=True)


def _inject_agent_context_messages(
    messages,
    aid,
    voice_mode=False,
):
    """Append dossier (+ legacy bullets) system messages after persona."""
    dossier = db.get_agent_dossier(aid) or dossier_lib.empty_dossier()
    dossier_text = dossier_lib.dossier_to_prompt(
        dossier,
        voice_mode=voice_mode,
    )
    if dossier_text:
        messages.append({"role": "system", "content": dossier_text})
    items = db.get_agent_memory_items(aid) or []
    if voice_mode:
        items = items[-4:]
    summary_memory = db.memory_items_to_prompt(items)
    if summary_memory:
        if voice_mode:
            summary_memory = summary_memory[-500:]
        messages.append({
            "role": "system",
            "content": "补充长期事实条（与上方档案重复时以档案为准）：\n"
            + summary_memory,
        })


_WMO_WEATHER = {
    0: "晴", 1: "大致晴朗", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇", 51: "小毛毛雨", 53: "毛毛雨",
    55: "较强毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨",
    81: "较强阵雨", 82: "强阵雨", 95: "雷雨",
}
_REALTIME_CACHE = {}
_REALTIME_CACHE_LOCK = threading.Lock()
_DEFAULT_WEATHER_LOCATION = "沈阳市铁西区"
_DEFAULT_WEATHER_PLACE = {
    "name": _DEFAULT_WEATHER_LOCATION,
    "latitude": 41.7989084,
    "longitude": 123.3502179,
}
_DEFAULT_WEATHER_ALIASES = {
    "沈阳市铁西区", "沈阳铁西区", "沈阳铁西", "铁西区", "铁西",
}


def _realtime_cache_get(key, max_age):
    with _REALTIME_CACHE_LOCK:
        cached = _REALTIME_CACHE.get(key)
    if not cached or time.monotonic() - cached["stored_at"] > max_age:
        return None
    return cached["value"]


def _realtime_cache_put(key, value):
    with _REALTIME_CACHE_LOCK:
        _REALTIME_CACHE[key] = {
            "stored_at": time.monotonic(),
            "value": value,
        }


def _get_json(url, params=None, timeout=8):
    response = httpx.get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=httpx.Timeout(timeout, connect=min(4.0, timeout)),
    )
    response.raise_for_status()
    return response.json()


def _extract_weather_location(text):
    match = re.search(
        r"(?:查(?:一下)?|看看|播报)?([\u4e00-\u9fff]{2,8}?)(?:今天|明天|现在)?"
        r"(?:的)?(?:天气|气温|温度)",
        text,
    )
    if not match:
        return _DEFAULT_WEATHER_LOCATION
    location = match.group(1).strip()
    location = re.sub(
        r"^(?:EV|E\s*V|伊维|衣维|依维|一位|请|帮我|给我)",
        "",
        location,
    ).strip()
    location = re.sub(r"^(今天|明天|现在)|(?:今天|明天|现在)$", "", location).strip()
    return location or _DEFAULT_WEATHER_LOCATION


# 兼容旧引用；真实缓存见 devices.coding.run_memory
_LAST_CODING_RUN = _coding_run_memory._LAST




def _remember_coding_run(aid, result: dict, task: str = "") -> None:
    _coding_run_memory.remember(aid, result, task=task)


def _voice_truth_facts(aid) -> str:
    """把系统已知事实写成短清单，供模型对照，禁止超纲宣称。"""
    lines = []
    try:
        st = coding_fsm.load(int(aid) or 0)
    except Exception:
        st = {}
    phase = (st or {}).get("phase") or "idle"
    brief = (st or {}).get("brief") or {}
    active = (st or {}).get("active_run") or agent_runtime.get_active_run()
    last_fsm = (st or {}).get("last_run") or {}
    last_mem = _coding_run_memory.get(int(aid) or 0)
    lines.append("工程相位=%s" % phase)
    if brief.get("goal"):
        lines.append("目标=%s" % str(brief.get("goal"))[:120])
    if active:
        lines.append("写码任务=进行中（未完成，不得说写好了/改好了）")
    else:
        lines.append("写码任务=未在跑")
    if (st or {}).get("pending_patch"):
        lines.append("有排队修改=是")
    preview = brief.get("preview_url") or last_mem.get("preview_url") or ""
    if preview:
        lines.append("已知预览URL=%s" % preview[:160])
    else:
        lines.append("已知预览URL=无")
    # 最近一次真实跑完
    src = last_mem if last_mem.get("at") else last_fsm
    if src:
        age = time.time() - float(src.get("at") or 0)
        if age < 900:
            ok = src.get("ok")
            lines.append(
                "最近一次写码结果=%s（约%d秒前）"
                % (("成功" if ok else "失败"), int(age))
            )
            if src.get("error"):
                lines.append("错误=%s" % str(src.get("error"))[:120])
            files = src.get("files") or []
            if files:
                lines.append("改动文件=%s" % ", ".join(str(f) for f in files[:8]))
            if src.get("summary"):
                lines.append("摘要=%s" % str(src.get("summary"))[:180])
    else:
        lines.append("最近一次写码结果=无记录")
    return "；".join(lines)


def _voice_coding_honesty_system(aid, tool_just_ran: bool = False) -> str:
    facts = _voice_truth_facts(aid)
    last = _coding_run_memory.get(int(aid) or 0)
    age = time.time() - float(last.get("at") or 0) if last else 1e9
    head = (
        "【事实铁律·写码/窗口/进度】你必须百分百如实交代「当下确实做了什么 / 确实没做什么」。"
        "只能依据系统事实清单与本轮工具回执说话；清单外的完成态一律不许说。"
        "禁止：写好了、改好了、已经换成X、已经打开窗口、正在重写中、预览在某某端口——"
        "除非事实清单或工具回执明确支持。"
        "不知道就说不知道/还没做/还在跑；不要用「应该已经」「大概好了」搪塞。"
        "系统事实：" + facts + "。"
    )
    if tool_just_ran:
        return (
            head
            + "本轮工作 Agent 已真实执行：只根据工具返回汇报改了哪些文件、成/败；"
            "不要编造端口或不存在的改动。可说还想改颜色/文案直接说。"
        )
    if last and age < 120 and last.get("ok"):
        return (
            head
            + "最近一次真实改动已完成（约 %d 秒前）。用户要继续改必须等系统再次调用工作 Agent，"
            "不能空口说又改好了。"
        ) % int(age)
    try:
        phase = coding_fsm.get_phase(int(aid) or 0)
    except Exception:
        phase = "idle"
    if phase == "writing" or agent_runtime.get_active_run():
        return head + "当前仍在写：只能说还在写/还没写完，禁止说已经写好。"
    if phase in ("clarifying", "planning", "awaiting_confirm"):
        return head + "当前还没开写：只能说在澄清/计划/等确认，禁止说正在写或写好了。"
    return head + "本轮若无工作 Agent 回执：禁止宣称任何文件已被改写。"


def _latest_preview_url() -> str:
    try:
        root = str(coding_path_policy.default_external_root(db.get_setting))
        arts = claude_code_skill.list_artifacts(root, since_mtime=0)
        preview = claude_code_skill.pick_preview_path(arts)
        if preview:
            return claude_code_skill.preview_url_for(preview, "http://127.0.0.1:8002")
    except Exception:
        pass
    return ""


def _skill_routing_card() -> str:
    """Constant routing protocol; capabilities live in the object registry."""
    return (
        "【固定动作协议】\n"
        "判断要不要调工具、调哪个，【以用户最新这一句的意图为主】；历史对话和下面的"
        "「最近真实回执」只是背景，用来接上『这个/那个/刚才』、做指代消歧、避免臆测，"
        "【不是让你重复上一轮的动作】——上一轮做过什么，不构成这一轮就要再做一次。"
        "最新这句没有新的动作意图（评价/感叹/答应/寒暄/口头语），就 conversation_reply。\n"
        "唯一例外：当前 project.active 明确处于 awaiting_confirm 时，用户说『可以/开始/就这样做』"
        "是在确认刚才的工程工作单，必须 invoke project.active command=confirm；不能当成普通寒暄。\n"
        "调变更类命令时，顺手用 say 写好做成后要说的那句话（你自己的措辞，别写具体数值）；"
        "没写也行，那就等回执回来你再说——但别让我替你说。"
        "没有动作意图时直接把话说出来就行，不用调任何工具；"
        "要动手就调真实工具并等 ok:true——没调=没做，也就无从声称完成。"
        "conversation_reply 仍可用，但它只是回答协议，不会执行动作。页面、灯、技能只是对象，不等于动作；"
        "问看法、原因、建议时不得改变任何对象。\n"
        "页面、助手界面、设备、画布、内置应用、文件工件和安装技能统一走 object_control："
        "不知道稳定 target 或能力时用 inspect（可用 selector.kind/owner/query 缩小范围）；"
        "修改对象属性用 apply(target,patch,base_rev)；执行对象命令用 invoke(target,command,args)。"
        "能力来自运行时对象描述符，禁止自行发明 target、patch 字段或 command。\n"
        "目标按对象身份和所有者判断，不按动作词猜：『你/你自己/给自己』指 owner=assistant 的"
        "agent.ui.status；实体灯只有明确指向 iot.desk-light 或上下文唯一承接该设备时才操作。"
        "回执必须带 verified_target=true，最终话术必须使用回执中的 target_name，"
        "不能用模糊的『给你换好了』掩盖实际目标。\n"
        "新增 Skill 也是运行时对象（kind=skill）；先按用户原意 inspect selector.query，"
        "再 invoke 返回的 skill target。不得要求新增常驻工具或在这里增加路由行。\n"
        "同时有多个动作 → 变更动作优先于查询；不同设备/页面可同轮并行调用。\n"
        "意图、对象、范围或必要参数不完整 → conversation_reply(mode=clarify) 只问一个关键问题；"
        "只要 reply 是追问信息，mode 就必须是 clarify；"
        "不得靠猜测调用 inspect 之外的动作。\n"
        "【短工作流】每轮只提交当前可执行的一组。互不依赖的动作同轮并行；"
        "用户要求阶段顺序、动作间说一句、或拿回执后再决定时，当前工具设 continue_after=true，"
        "等回执后再说/再调用下一步。普通单次 web_search/web_extract，无论实际网络偶尔慢不慢，"
        "都设 speak_while=false、progress_reply留空，搜完直接给结论；只有任务本身需要多对象对比、"
        "多来源核对、连续深挖，或用户明确要求认真查找时才设 speak_while=true。此时 progress_reply"
        "由你结合当前问题临场写一句自然、具体的短话，说明为什么要多花一点时间；不要套用"
        "『好的，我搜一下』『我去查查』之类通用句式，也不得提前声称查到或完成。"
        "speak_while 只决定是否允许先说一句，不代表允许多搜一轮；一次 web_search 已会聚合多来源，"
        "continue_after=false 后必须直接回答，不得换关键词再次搜索。"
        "写码等长任务也可设 speak_while=true；程序会边播安全开始语边执行，"
        "开始语绝不算完成回执。\n"
        "工程、PCB、CAD 等长任务都以运行时对象承载，不新增专用工具：当前工程固定 target="
        "project.active。先 invoke plan 形成版本化工作单并用自然语言告诉用户计划；用户确认后"
        "invoke confirm。执行中补充要求 invoke update，进度/停止/回退分别用 status/cancel/revert。"
        "后台结果必须经过文件回执后才能说完成。\n"
        "【语言铁律】无论中间工具轮还是最终答复，一律用中文（用户用其他语言才跟随）。"
        "禁止冒出英文叙述（如「I have enough data」）——你是在和中文用户语音对话。"
        "工具调用前的过渡语只允许一两个中文词（好/行/稍等/马上），不要用英文。\n"
        "实时信息与联网检索走 task_control："
        "现在几点用 current_time；计时/倒计时绝不能用 current_time。"
        "『几点吃饭/几点开始/记录几点钟』不是问当前时间，应回答或澄清；request 填完整原意。\n"
        "查新闻、时效信息、核实事实用 task_control kind=web_search；"
        "简单明确的问题 research_depth=quick；小众对象、真假核实、多来源对比，或用户明确"
        "要求认真找时 research_depth=thorough，并给 2-3 个互补 search_queries，一次调用内部完成，"
        "不要连续改关键词重搜。只有图片能帮助识别、比较或理解时 include_visuals=true。"
        "仅当用户有查询/核实意图才用；只报一个名称/名词(纯陈述)不搜。"
        "要查具体资料(接线图/引脚/参数/报错)却没说清是哪个型号、哪个引脚/信号时，"
        "先 conversation_reply(clarify) 问一句再搜——"
        "『Arduino 接线图』要先问是 UNO 还是 Nano、接什么模块。"
        "但只问一次、只问最关键那一项：型号和对象都给了就直接搜，"
        "别再追问针数、版本这类细枝末节——那些从搜索结果里看就行。"
        "搜索只负责取回证据，结果会自动进入带稳定节点ID的研究画布；不要传呈现枚举。"
        "只要求‘显示/预览/放到研究画布’时搜索本身已经完成，continue_after=false，"
        "不得再 inspect/apply。只有用户还明确要求聚焦、全屏、隐藏或重排等具体变换，"
        "才设 continue_after=true 并把该变换原样写入 post_search_goal；"
        "3D 预览必须来自真实 GLB/glTF 资源；禁止用依赖外部脚本的临时 HTML 冒充已显示，"
        "也不能把模型目录网页说成可交互模型。\n"
        "读取已知链接用 web_extract；打开网站则对 surface.new invoke create。"
    )


def _scene_open_reply(surface_id, rev, opened_text):
    """窗口打开后的诚实回执：只有等桌面壳回执 ok 才算打开。"""
    if scene_store.wait_surface_ready(surface_id, min_rev=rev, timeout=0.9):
        return opened_text
    if scene_store.shell_count():
        return "已经发给桌面壳了，但还没收到窗口打开回执。"
    return "状态已经记下，但 Tauri 桌面壳现在没连上，所以不能说窗口已经打开。"


def _route_open_preview(*, aid: int, msg: str, base: str, phase: str) -> str:
    del msg, phase
    url = _latest_preview_url() or (coding_fsm.load(aid).get("brief") or {}).get("preview_url") or ""
    if not url:
        return "还没有可预览的页面。"
    coding_fsm.set_preview(aid, url=url, locked=False)
    coding_orch.ensure_preview_window(aid, url=url, open_native=False, base_url=base)
    result = scene_store.request_show("site-preview")
    return _scene_open_reply("site-preview", int(result.get("rev") or 0), "好，网站预览窗已打开并复用。")


def _route_open_studio(*, aid: int, msg: str, base: str, phase: str) -> str:
    del msg, base
    if phase == "writing" or agent_runtime.get_active_run():
        coding_orch.ensure_terminal_window(aid, open_native=False)
        result = scene_store.request_show("work-hud", focus=False)
        return _scene_open_reply("work-hud", int(result.get("rev") or 0), "工作状态已经显示。")
    return "现在没有执行中的工程任务；工作状态会在开始后自动出现。"


def _route_open_blank(*, aid: int, msg: str, base: str, phase: str) -> str:
    del aid, msg, base, phase
    _text, meta = surface_control.execute({
        "action": "show",
        "surface_id": "blank-board",
        "title": "空白窗口",
        "summary": "",
        "sections": [],
    })
    if meta.get("ok"):
        return "好，空白窗口已打开并复用。"
    return "空白窗口没有打开：%s" % (meta.get("error") or "未收到桌面回执")


def _route_coding_status(*, aid: int, **_context) -> str:
    return coding_fsm.status_speech(aid)


def _route_window_status(**_context) -> str:
    visible = [
        label for surface_id, label in (
            ("work-hud", "工作状态"),
            ("site-preview", "网站预览"),
            ("blank-board", "空白窗口"),
        )
        if scene_store.wait_surface_ready(surface_id, timeout=0)
    ]
    if visible:
        return "桌面壳已回执：%s。" % "、".join(visible)
    if scene_store.shell_count():
        return "桌面壳在线，但目前没有窗口打开回执。"
    return "Tauri 桌面壳没连上，目前不能确认有窗口打开。"


def _route_coding_cancel(*, aid: int, **_context) -> str:
    active = bool(agent_runtime.get_active_run()) or coding_fsm.get_phase(aid) == "writing"
    if not active:
        return "现在没有在写。"
    agent_runtime.cancel_run()
    coding_fsm.transition(aid, "idle", reason="user_cancel")
    coding_orch.push_studio(aid, status="已停止", detail="这轮工作已取消", phase="cancelled", done=True, ok=False)
    return "好，这轮编写已停止。"


def _route_coding_revert(*, aid: int, **_context) -> str:
    result = coding_orch.handle_revert(aid)
    return "已撤回到改之前。" if result.get("ok") else ("撤销失败：" + (result.get("error") or "未知错误"))


def _route_open_browser(*, aid: int, **_context) -> str:
    url = _latest_preview_url() or (coding_fsm.load(aid).get("brief") or {}).get("preview_url") or ""
    ok = coding_native_ui.open_url(url) if url else False
    return "好，已用系统浏览器打开。" if ok else "还没有可打开的预览地址。"


def _route_coding_clarify(*, aid: int, msg: str, **_context) -> str:
    coding_fsm.transition(aid, "clarifying", reason="voice_clarify")
    # A new vague project starts a new brief.  Keeping a prior plan, risk or
    # preview here made unrelated requests look as if the user had supplied
    # requirements that actually came from an older run.
    coding_fsm.update_brief(aid, {
        "goal": (msg or "")[:160],
        "constraints": [],
        "open_questions": [],
        "plan_steps": [],
        "risks": [],
        "diagrams": [],
        "preview_path": "",
        "preview_url": "",
        "preview_mode": "static",
    })
    return "可以做。请再说清楚你最终想得到什么、要改哪个现有内容，以及必须遵守的限制。"


def _route_coding_plan(*, aid: int, msg: str, plan_steps=None, risks=None, **_context) -> str:
    coding_fsm.transition(aid, "planning", reason="voice_plan")
    goal = (msg or "").strip()[:160]
    plan_steps = [str(item).strip() for item in (plan_steps or []) if str(item).strip()][:20]
    if not plan_steps:
        plan_steps = ["确认目标和现有项目范围", "实现用户明确要求的改动", "运行与交付物相匹配的验证"]
    risks = [str(item).strip() for item in (risks or []) if str(item).strip()][:12]
    coding_fsm.update_brief(aid, {"goal": (msg or "")[:160], "plan_steps": plan_steps, "risks": risks})
    coding_fsm.prepare_work_order(aid, goal=goal, plan_steps=plan_steps)
    coding_fsm.transition(aid, "awaiting_confirm", reason="plan_ready")
    return "我会先%s；然后%s。你确认后我就开始。" % (plan_steps[0], "；再".join(plan_steps[1:]))


def _route_coding_diagram(*, aid: int, **_context) -> str:
    coding_fsm.transition(aid, "planning", reason="diagram")
    mermaid = (
        "stateDiagram-v2\n  [*] --> idle\n  idle --> clarifying: 需求不清\n"
        "  clarifying --> planning: 需求足够\n  planning --> awaiting_confirm: 计划完成\n"
        "  awaiting_confirm --> writing: 确认开写\n  writing --> idle: done\n"
    )
    diag = coding_diagrams.make_diagram("工程会话状态机", mermaid)
    if diag:
        coding_fsm.update_brief(aid, {"diagrams": [diag]})
    coding_orch.push_studio(
        aid,
        status="状态机",
        detail="idle → clarifying → planning → awaiting_confirm → writing → idle",
        phase="planning",
        plan_steps=["idle：空闲", "clarifying：澄清", "planning / awaiting_confirm：计划待确认", "writing：编写中"],
    )
    return "状态机已经生成：澄清、计划、确认，然后才开写。"


_VOICE_DIRECT_HANDLERS = {
    "open_preview": _route_open_preview,
    "open_studio": _route_open_studio,
    "open_blank": _route_open_blank,
    "coding_status": _route_coding_status,
    "window_status": _route_window_status,
    "coding_cancel": _route_coding_cancel,
    "coding_revert": _route_coding_revert,
    "open_browser": _route_open_browser,
    "coding_clarify": _route_coding_clarify,
    "coding_plan": _route_coding_plan,
    "coding_diagram": _route_coding_diagram,
}


def _voice_realtime_tool(text, *, forced_kind=""):
    normalized = re.sub(r"\s+", "", text or "")
    started_at = time.perf_counter()
    now = datetime.datetime.now()

    if forced_kind == "weather":
        location = _extract_weather_location(normalized)
        cache_key = "weather:" + location
        context = _realtime_cache_get(cache_key, 300)
        if context is None:
            try:
                if location in _DEFAULT_WEATHER_ALIASES:
                    place = dict(_DEFAULT_WEATHER_PLACE)
                else:
                    geo = _get_json(
                        "https://geocoding-api.open-meteo.com/v1/search",
                        {
                            "name": location,
                            "count": 1,
                            "language": "zh",
                            "format": "json",
                        },
                        timeout=6,
                    )
                    places = geo.get("results") or []
                    if not places:
                        raise RuntimeError("未找到城市")
                    place = places[0]
                weather = _get_json(
                    "https://api.open-meteo.com/v1/forecast",
                    {
                        "latitude": place["latitude"],
                        "longitude": place["longitude"],
                        "current": (
                            "temperature_2m,apparent_temperature,"
                            "relative_humidity_2m,weather_code,wind_speed_10m"
                        ),
                        "daily": (
                            "weather_code,temperature_2m_max,"
                            "temperature_2m_min,precipitation_probability_max"
                        ),
                        "timezone": "Asia/Shanghai",
                        "forecast_days": 2,
                    },
                    timeout=8,
                )
                current = weather.get("current") or {}
                daily = weather.get("daily") or {}
                code = int(current.get("weather_code") or 0)
                city = place.get("name") or location
                context = (
                    "实时天气：%s现在%s，%.1f°C，体感%.1f°C，湿度%s%%，"
                    "风速%s公里/小时；今天最高%s°C，最低%s°C，最高降水概率%s%%。"
                    % (
                        city,
                        _WMO_WEATHER.get(code, "天气状况未知"),
                        float(current.get("temperature_2m") or 0),
                        float(current.get("apparent_temperature") or 0),
                        current.get("relative_humidity_2m", "未知"),
                        current.get("wind_speed_10m", "未知"),
                        (daily.get("temperature_2m_max") or ["未知"])[0],
                        (daily.get("temperature_2m_min") or ["未知"])[0],
                        (daily.get("precipitation_probability_max") or ["未知"])[0],
                    )
                )
                _realtime_cache_put(cache_key, context)
            except Exception as error:
                context = "实时天气获取失败（地点：%s）：%s" % (location, error)
        return {
            "name": "weather",
            "context": context,
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
        }

    if forced_kind == "time":
        return {
            "name": "time",
            "direct_reply": "现在是%s。" % now.strftime("%H点%M分"),
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
        }

    if forced_kind == "date":
        weekdays = "一二三四五六日"
        return {
            "name": "date",
            "direct_reply": "今天是%s年%s月%s日，星期%s。" % (
                now.year,
                now.month,
                now.day,
                weekdays[now.weekday()],
            ),
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
        }

    if forced_kind == "web_search":
        if _web_search_enabled():
            # 语音走快路径：basic + 不抽正文，先开口再说细节
            result = _run_web_search_tool(text, profile="voice")
            return {
                "name": "web_search",
                "context": deep_search.format_tool_result(result),
                "panel": result.get("panel"),
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
            }

    return None


def _realtime_info_tool_definition():
    return {
        "type": "function",
        "function": {
            "name": "realtime_info",
            "description": (
                "查询实时时间/日期/天气（系统直取，快）。"
                "用户问现在几点/几月几号/星期几/某地天气时调用；"
                "kind 必填：time/date/weather，weather 可带 location。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["time", "date", "weather"],
                        "description": "time=现在几点；date=今天几月几号星期几；weather=某地实时天气",
                    },
                    "location": {
                        "type": "string",
                        "description": "weather 专用：城市/地名，如杭州；缺省用默认城市",
                    },
                },
                "required": ["kind"],
            },
        },
    }


def _split_direct_reply_chunks(text):
    """把直答文本拆成增量小段，让 TTS 提前出声（首 PCM 从 ~800ms 降到 ~200ms）。

    实测火山流式 TTS：整句 10 字首 PCM 782ms，3-8 字首段仅 ~200ms。
    turn.py 按 FIRST_SEGMENT_CHARS=8 阈值切分——首段凑满 8 字符（或遇
    句号）就提交给 TTS，TTS 边合成边等后续文本。
    """
    text = str(text or "").strip()
    if not text:
        return []
    # 首段直接给满 FIRST_SEGMENT_CHARS，让 turn.py 立即切出送 TTS；
    # 后续按自然标点分块，末尾不足 3 字的并入前段。
    first_chars = 8
    first = text[:first_chars]
    rest = text[first_chars:]
    if not rest:
        return [text]
    pieces = [first]
    buf = ""
    for ch in rest:
        buf += ch
        if ch in "，。！？；、,:.":
            pieces.append(buf)
            buf = ""
    if buf:
        if pieces and len(buf) < 3:
            pieces[-1] += buf
        else:
            pieces.append(buf)
    return pieces


def _realtime_info_execute(arguments):
    args = arguments if isinstance(arguments, dict) else {}
    kind = str(args.get("kind") or "").strip()
    location = str(args.get("location") or "").strip()
    if kind == "weather":
        text = (location + "天气") if location else ""
    else:
        text = ""
    result = _voice_realtime_tool(text, forced_kind=kind)
    if not result:
        return "realtime_info 无法获取%s信息。" % kind, {
            "ok": False, "kind": kind,
        }
    payload = result.get("context") or result.get("direct_reply") or ""
    meta = {
        "ok": True,
        "kind": kind,
        "elapsed_ms": result.get("elapsed_ms", 0),
    }
    # 时间/日期等已生成完整自然语言答复：动作流可据此跳过第二轮 LLM，
    # 直接播报，省一次 prefill（DeepSeek 无缓存，第二轮全量重算 ~2s）。
    if result.get("direct_reply"):
        meta["direct_reply"] = result["direct_reply"]
    return payload, meta


def _web_search_cfg():
    return deep_search.load_config(db.get_setting)


def _web_search_enabled():
    cfg = _web_search_cfg()
    return bool(cfg.get("enabled")) and bool(deep_search._providers_ready(cfg))



def _claude_code_enabled():
    try:
        cfg = agent_runtime.load_config(db.get_setting, db.set_setting)
        return bool(cfg.get("enabled") and cfg.get("available"))
    except Exception:
        return False


def _panel_source_for(name, items):
    """把条目名对上服务端手里的真实链接；对不上就不给链接，绝不猜。"""
    target = "".join(str(name or "").lower().split())
    if not target:
        return "", ""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        haystack = "".join(
            ("%s %s" % (item.get("title") or "", item.get("snippet") or "")).lower().split()
        )
        if target not in haystack:
            continue
        url = str(item.get("url") or "")
        host = re.match(r"^https?://([^/?#]+)", url)
        return url, re.sub(r"^www\.", "", host.group(1)) if host else ""
    return "", ""


def _panel_entries_from_answer(answer, result):
    """把回答里讲到的对象提成面板条目。

    面板此前列的是搜索命中的文章页（「XX 大盘点」「有哪些开源机械臂项目」），
    而用户要的是回答里真正讲到的那几个东西各带一行说明。模型口播时本来就把
    对象名用 **粗体** 标出来，直接用这个既有约定，不额外占提示预算、也不多一次
    模型来回。链接仍由服务端按名字对回真实结果，模型没有写 URL 的机会。
    """
    text = str(answer or "")
    marks = [
        match for match in re.finditer(r"\*\*([^*\n]{1,40})\*\*", text)
        if match.group(1).strip()
    ]
    if len(marks) < 2:
        # 只讲了一个对象时，面板摆一条清单没有意义，走原来的证据展示
        return []
    items = (result or {}).get("items") or []
    entries = []
    seen = set()
    for index, match in enumerate(marks):
        name = match.group(1).strip(" ，,：:、")[:60]
        key = "".join(name.lower().split())
        if not name or key in seen:
            continue
        seen.add(key)
        tail = text[match.end(): marks[index + 1].start() if index + 1 < len(marks) else len(text)]
        note = re.sub(r"\s+", " ", tail.replace("**", ""))
        # 截到最后一个句号：句号之后的是引出下一个对象的话（「再往上是」「还有」），
        # 属于口播的连接词，不是这一条的内容。
        cut = max(note.rfind("。"), note.rfind("！"), note.rfind("？"))
        if cut > 0:
            note = note[:cut]
        note = note.strip(" ，,：:。、；;-—")[:180]
        url, site = _panel_source_for(name, items)
        entries.append({"name": name, "note": note, "url": url, "site": site})
    return entries[:8]


def _conversation_reply_tool_definition():
    """语音协议中的无动作回答/澄清出口。"""
    return {
        "type": "function",
        "function": {
            "name": "conversation_reply",
            "description": (
                "无动作出口：answer 回答，clarify 追问。执行或改变状态必须用工具；"
                "reply 禁止声称设备/页面已打开、关闭、修改或完成。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["answer", "clarify"],
                        "description": "answer=直接作答；clarify=reply 本身在向用户追问。",
                    },
                    "reply": {
                        "type": "string",
                        "description": "要直接对用户说的简短中文回复。",
                    },
                },
                "required": ["mode", "reply"],
            },
        },
    }


def _coding_flow_tool_definition(*, slim=False):
    if slim:
        return {
            "type": "function",
            "function": {
                "name": "coding_flow",
                "description": (
                    "写码工作流控制。action: clarify 澄清需求 / plan 出计划 / "
                    "status 查进度 / cancel 取消 / revert 回退。执行写码需计划窗确认。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["clarify", "plan", "status", "cancel", "revert"],
                        },
                        "request": {
                            "type": "string",
                            "description": "用户的写码目标或澄清内容",
                        },
                        "plan_steps": {
                            "type": "array",
                            "description": "可编辑的实施步骤（plan 用）",
                            "items": {"type": "string"},
                        },
                        "risks": {
                            "type": "array",
                            "description": "真实风险与验证点（plan 用）",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["action"],
                },
            },
        }
    return {
        "type": "function",
        "function": {
            "name": "coding_flow",
            "description": (
                "Semantic work-agent flow control, separate from window management. "
                "Actions: clarify (ask/refine requirements), plan (prepare an editable plan), "
                "status (check progress), cancel, revert. Execution requires confirmation of a versioned work order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["clarify", "plan", "status", "cancel", "revert"],
                    },
                    "request": {
                        "type": "string",
                        "description": "The user's exact coding goal or clarification; do not invent requirements.",
                    },
                    "plan_steps": {
                        "type": "array",
                        "description": "Concrete editable implementation steps grounded in the request; plan action only.",
                        "items": {"type": "string"},
                    },
                    "risks": {
                        "type": "array",
                        "description": "Real uncertainties or verification risks; do not invent domain facts.",
                        "items": {"type": "string"},
                    },
                },
                "required": ["action"],
            },
        },
    }


def _build_chat_tools(aid, *, voice_mode=False):
    # 模型侧保持固定协议：对象能力从运行时 registry 发现，绝不随页面、设备、
    # 画布或已安装 Skill 的数量扩张 function schema。
    if voice_mode:
        tools = [
            _conversation_reply_tool_definition(),
            task_control.tool_definition(),
            object_control.tool_definition(),
        ]
    else:
        tools = [
            _conversation_reply_tool_definition(),
            object_control.tool_definition(),
            surface_skill.surface_manage_tool_definition(),
            surface_skill.surface_inspect_tool_definition(),
            surface_skill.surface_expect_input_tool_definition(slim=True),
            _realtime_info_tool_definition(),
            device_control.tool_definition(slim=True),
            canvas_control.tool_definition(),
        ]
    if _web_search_enabled() and not voice_mode:
        tools.append(deep_search.tool_definition(slim=True))
        tools.append(deep_search.extract_tool_definition(slim=True))
    # 分级常驻诊断：每轮请求记录工具集大小与构成，便于确认 token 节省与回归。
    try:
        print(
            "[muse] tools %s: %d 个，%d 字符，names=%s"
            % (
                "voice" if voice_mode else "text",
                len(tools),
                len(json.dumps(tools, ensure_ascii=False)),
                ",".join(t["function"]["name"] for t in tools),
            ),
            flush=True,
        )
    except Exception:
        pass
    return tools or None


def _tool_request_kwargs(tools):
    """Build function-calling kwargs, or disable tools for a forced answer round.

    Passing ``tools=None`` together with ``tool_choice=required`` is not enough:
    some OpenAI-compatible providers keep producing calls from prior context.
    Omitting both fields creates a real text-only generation boundary.

    tool_choice 恒为 auto——不动手是默认路径，不是需要主动选中的一个出口。
    以前语音每轮 required：用户只说「不错」，模型也必须产出一次调用，分类一抖
    就是误动作。防「光说不做」的担子已经转移到回执：播报锚在 after 上，没调
    工具就没有 after 可复述。唯一的例外是抓到无凭据声称之后那一轮，由调用方
    就地改成 required（见 force_tool_next_round）。
    """
    available = list(tools or [])
    if not available:
        return {}
    return {"tools": available, "tool_choice": "auto"}


def _execute_voice_action_tool(arguments, aid, request: Request = None):
    args = arguments if isinstance(arguments, dict) else {}
    action = str(args.get("action") or "")
    user_request = str(args.get("request") or "").strip()
    base = _claude_code_base_url(request)
    phase = coding_fsm.get_phase(aid)

    if action == "coding_write":
        if not _claude_code_enabled():
            return "工作 Agent 当前不可用。", {"ok": False, "action": action}
        brief = coding_fsm.load(aid).get("brief") or {}
        task = user_request or brief.get("goal") or ""
        if not task:
            return "没有可执行的明确写码目标。", {"ok": False, "action": action}
        started = coding_orch.start_writing(
            aid,
            task,
            get_setting=db.get_setting,
            set_setting=db.set_setting,
            base_url=base,
            mode="external",
            open_desk=False,
        )
        ok = bool(started.get("ok") or started.get("queued"))
        ack_text = started.get("speech") or (
            "开始处理。" if ok else "工作 Agent 没能启动。"
        )
        return ack_text, {
            "ok": ok,
            "action": action,
            "queued": bool(started.get("queued")),
            "run_id": started.get("run_id") or "",
            "speech": ack_text,
        }

    handlers = {
        key: _VOICE_DIRECT_HANDLERS[key]
        for key in (
            "open_blank", "open_studio", "open_preview", "open_browser",
            "window_status", "coding_status", "coding_cancel", "coding_revert",
            "coding_clarify", "coding_plan", "coding_diagram",
        )
    }
    handler = handlers.get(action)
    if not handler:
        return "未知 voice_action。", {"ok": False, "action": action}
    reply = handler(aid=aid, msg=user_request, base=base, phase=phase)
    return reply, {"ok": True, "action": action, "phase": coding_fsm.get_phase(aid)}


_VOICE_TURN_DEDUP = {}
_VOICE_TURN_DEDUP_LOCK = threading.RLock()

_MODEL_QUERY_RE = re.compile(r"(?:3\s*d|三维)\s*模型|(?:glb|gltf)", re.I)
_MODEL_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:glb|gltf)(?:\?[^\s\"'<>]*)?",
    re.I,
)
_BUILTIN_MODEL_URL = "/static/models/ev-demo-robot.glb"


def _is_model_search(query):
    return bool(_MODEL_QUERY_RE.search(str(query or "")))


def _is_generic_model_search(query):
    """是否只是在要“任意一个模型”，而没有指定对象。"""
    text = str(query or "").lower()
    text = re.sub(
        r"请|帮我|给我|麻烦|搜索|搜一下|搜|查找|找|显示|展示|预览|打开|"
        r"可交互|互动|一个|一份|任意|随便|来个|来一个|看看|3\s*d|三维|"
        r"模型|model|glb|gltf|下载|免费|资源|素材|文件|网站|平台|推荐|热门|"
        r"高质量|模型库|库|一下|在线|可以|能|直接|出来|使用|用|的",
        "",
        text,
        flags=re.I,
    )
    return not re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


_IDENTITY_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]*|\d+")
_SEARCH_LEAD_RE = re.compile(
    r"^(?:我?想?(?:知道|了解|问问|查查|查一下|搜一下|看看)|我说|帮我(?:查|搜)一?下?|"
    r"查一?下?|搜一?下?|上网(?:查|搜)一?下?)\s*"
)


def _identity_tokens(text):
    """取出可能是型号/专名的拉丁与数字标识；单个字母不算（「B站」的 B 不是型号）。"""
    joined = re.sub(r"\b([A-Za-z])\s+(?=[A-Za-z]\b)", r"\1", str(text or ""))
    while re.search(r"\b[A-Za-z]\s+[A-Za-z]\b", joined):
        joined = re.sub(r"\b([A-Za-z])\s+(?=[A-Za-z]\b)", r"\1", joined)
    return [
        token.lower() for token in _IDENTITY_TOKEN_RE.findall(joined)
        if len(token) >= 2
    ], joined


def _query_differs_from_user_wording(request, user_text):
    """检索词把用户说的标识换掉了吗？只报告，不改写。

    曾经在这里强行改回用户原话，是错的：ASR 把「DGX」听成「C G X」时，模型
    改成 DGX 是对的纠正，被这条规则硬拗回 CGX 反而查不到东西。而「雷鸟 IO」
    被换成「雷鸟 Air 3」是错的替换——两者在文本上一模一样（都是空格分开的
    单字母），只有靠知识才分得清，而知识在模型那儿不在规则这儿。

    所以这里只判断「换了没换」，换了就把这件事写进工具结果，让模型自己决定
    是坚持纠正（并对用户说明）还是改用用户的原话重查。
    """
    user_ids, normalized_user = _identity_tokens(user_text)
    request = str(request or "").strip()
    if not user_ids or not request:
        return ""
    request_ids, _ = _identity_tokens(request)
    if not request_ids or any(token in request_ids for token in user_ids):
        return ""
    spoken = _SEARCH_LEAD_RE.sub("", normalized_user).strip(" ，,。？?！!")
    return (
        "【检索词与用户原话不一致】用户说的是「%s」，你检索用的是「%s」。"
        "如果这是你在纠正语音听岔（比如把「C G X」听成的词改回 DGX），"
        "就在回答里点一句你按什么查的；如果不是，说明你把用户说的东西换成了"
        "别的，请改用用户的原话重查。" % (spoken[:40], request[:40])
    )


def _model_search_query(query):
    """3D 搜索优先找可直接渲染的文件，不把模型目录页当成模型。"""
    query = str(query or "").strip()
    if _is_model_search(query) and not re.search(r"(?:glb|gltf)", query, re.I):
        return "%s 可直接下载 GLB glTF" % query
    return query


def _is_direct_model_url(value):
    """Only a URL whose path is an actual GLB/glTF file is previewable."""
    raw = str(value or "").strip().rstrip(".,;，。；)）]】")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.path.lower().endswith((".glb", ".gltf"))
    )


def _direct_model_from_result(result):
    """从搜索器的任意嵌套返回中找真实 .glb/.gltf 资源。"""
    if not isinstance(result, dict):
        return None
    stack = [result]
    seen = set()
    while stack and len(seen) < 600:
        value = stack.pop()
        marker = id(value)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(value, dict):
            raw_url = value.get("url") or value.get("src") or value.get("href")
            if isinstance(raw_url, str):
                if _is_direct_model_url(raw_url):
                    return {
                        "url": raw_url,
                        "title": str(value.get("title") or value.get("name") or "3D 模型"),
                        "description": str(
                            value.get("snippet") or value.get("description") or "拖动旋转，滚轮缩放"
                        ),
                        "poster": str(value.get("image") or value.get("thumbnail") or ""),
                    }
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
        elif isinstance(value, str):
            match = next(
                (
                    candidate for candidate in _MODEL_URL_RE.finditer(value)
                    if _is_direct_model_url(candidate.group(0))
                ),
                None,
            )
            if match is not None:
                return {
                    "url": match.group(0).rstrip(".,;，。；)）]】"),
                    "title": "3D 模型",
                    "description": "拖动旋转，滚轮缩放",
                    "poster": "",
                }
    return None


def _search_model_asset(query, result):
    direct = _direct_model_from_result(result)
    if direct:
        return direct
    if _is_model_search(query) and _is_generic_model_search(query):
        return {
            "url": _BUILTIN_MODEL_URL,
            "title": "EV 示例机器人",
            "description": "内置离线 GLB 模型 · 拖动旋转 · 滚轮缩放",
            "poster": "",
        }
    return None


def _run_web_search_tool(
    query,
    profile="full",
    *,
    search_queries=None,
    include_visuals=None,
    grounding="",
):
    original_query = str(query or "").strip()
    result = deep_search.search(
        _model_search_query(original_query),
        get_setting=db.get_setting,
        profile=profile,
        query_variants=list(search_queries or [])[:3],
        include_images=include_visuals,
        grounding=grounding,
    )
    if isinstance(result, dict) and original_query:
        # 面板标题继续显示用户原话，检索增强词只是搜索器内部约束。
        result["query"] = original_query
    return result


_WEB_SEARCH_CACHE_TTL_S = 300.0
# voice 模式的搜索缓存极短：搜索结果会推送到常驻信息窗口，同 query 的
# 「重新查/更新」必须能较快刷新面板。20s 足够挡住连击重复搜索。
_VOICE_SEARCH_CACHE_TTL_S = 20.0


def _web_search_cache_key(query):
    """查询归一化：去空格与常见搜索动词，作为缓存 key。"""
    text = re.sub(r"\s+", "", query or "").strip().lower()
    text = re.sub(
        r"^(请|帮我|给我|麻烦|可否|能不能|请帮我|帮我搜|麻烦搜)?"
        r"(搜一下|搜索一下|查一下|查查|看看|检索一下|搜索|检索|搜)\s*",
        "",
        text,
    ).strip("：:，,。")
    return text[:80]


def _web_search_with_background(
    query,
    *,
    voice_mode=False,
    research_depth="quick",
    search_queries=None,
    include_visuals=None,
    grounding="",
):
    """Search once and return one stable evidence snapshot.

    Earlier voice turns rendered a basic result, then a background full result,
    then the model answer. Those three visible replacements caused reflow and
    let raw crawler summaries overwrite the useful conclusion. Voice now uses
    one synchronous transaction: quick for simple facts, full for explicitly
    thorough research. The canvas is committed only after the answer is ready.
    """
    q = (query or "").strip()
    if voice_mode:
        depth = "thorough" if str(research_depth or "").lower() == "thorough" else "quick"
        normalized_key = _web_search_cache_key(q)
        cache_key = "web_search:%s:%s" % (depth, normalized_key)
        if normalized_key:
            cached = _realtime_cache_get(cache_key, _VOICE_SEARCH_CACHE_TTL_S)
            if cached:
                return cached
        result = _run_web_search_tool(
            q,
            profile="full" if depth == "thorough" else "voice",
            search_queries=search_queries if depth == "thorough" else None,
            include_visuals=bool(include_visuals),
            grounding=grounding,
        )
        if result.get("ok") and normalized_key:
            _realtime_cache_put(cache_key, result)
        return result
    final = _run_web_search_tool(
        q,
        profile="full",
        search_queries=search_queries,
        include_visuals=include_visuals,
        grounding=grounding,
    )
    return final


def _push_search_to_info_board(
    result,
    *,
    expand=True,
    pending=False,
    activate=True,
    sync=True,
):
    """把搜索/读网页的结果推给状态栏的信息推送区（不再是独立窗口）。

    信息推送以前是一个单独的常驻窗口，用户得单独管理、风格也和状态栏割裂。
    现在它是状态栏往下展开的一块：有内容就展开，收起时状态栏恢复紧凑条。
    这里只负责把结构化数据交给 info_panel，几何交给 surface_tools，
    渲染交给 status_timeline.html —— 三层通过 rev 对齐。
    """
    try:
        from control_plane import info_panel
        from devices.coding.surface_tools import sync_status_timeline_to_canvas

        query = str(result.get("query") or "")
        want = str(result.get("want") or "").strip().lower()
        items = result.get("items") or []
        model = _search_model_asset(query, result)
        weak_evidence = (
            str(result.get("evidence_quality") or "").lower() == "weak"
            or result.get("answerable") is False
        )
        payload = None
        if pending and query:
            # A stable, content-free search state appears immediately. Evidence
            # remains staged until the answer is ready, so users never watch raw
            # crawler rows morph into a different final card.
            payload = {
                "kind": "search",
                "want": want,
                "query": query,
                "title": "搜索：%s" % query.strip(),
                "summary": query,
                "items": [],
                "images": [],
            }
        elif items or model or (result.get("images") and want == "images"):
            summary = str(result.get("summary") or "")
            if _is_model_search(query) and not model:
                summary = "没有找到可直接预览的 GLB/GLTF 文件；目录网页不会冒充 3D 模型。"
            payload = {
                "kind": "search",
                "want": want,
                "query": query,
                "title": "搜索：%s" % query.strip(),
                "summary": summary,
                "items": [] if weak_evidence else items,
                "images": (result.get("images") or []) if want == "images"
                else ([] if weak_evidence else (result.get("images") or [])),
                "chart": result.get("chart"),
                "model": model,
            }
        else:
            # web_extract 单页解析：没有 items，但有标题/正文段落
            title = str(result.get("title") or "").strip()
            paragraphs = [str(p) for p in (result.get("paragraphs") or []) if str(p).strip()]
            if not (title or paragraphs):
                return
            payload = {
                "kind": "page",
                "query": title,
                "title": title or "网页摘要",
                "summary": str(result.get("summary") or "") or "\n".join(paragraphs[:3]),
                "items": [{
                    "title": title or str(result.get("url") or ""),
                    "url": str(result.get("url") or ""),
                    "snippet": paragraphs[0] if paragraphs else "",
                }] if (title or result.get("url")) else [],
                "images": result.get("images") or [],
                "chart": result.get("chart"),
                "model": _search_model_asset(title, result),
            }
        if not payload:
            return None
        snap = info_panel.push(
            payload, expand=expand, activate=activate,
            kind=payload.get("kind") or "search", pending=pending,
        )
        if sync:
            sync_status_timeline_to_canvas()
        document = snap.get("document") if isinstance(snap.get("document"), dict) else {}
        nodes = document.get("nodes") if isinstance(document.get("nodes"), dict) else {}
        return {
            "ok": bool(document),
            "surface": "research_canvas",
            "visible": bool(snap.get("expanded") and document),
            "tab_id": snap.get("active_tab_id") or "",
            "node_types": [
                node.get("type") for node in nodes.values()
                if isinstance(node, dict) and node.get("type")
            ],
        }
    except Exception as error:
        print("[muse] 信息推送写入失败: %s" % error, flush=True)
        return None


def _begin_search_canvas(query):
    return _push_search_to_info_board(
        {"query": str(query or "").strip()},
        expand=True,
        pending=True,
        activate=True,
        sync=True,
    )


def _commit_search_answer(result, tab_id, answer, entries=None):
    """Atomically reveal one answer-first card and its small evidence set."""
    try:
        from control_plane import info_panel
        from devices.coding.surface_tools import sync_status_timeline_to_canvas

        receipt = None
        if isinstance(result, dict) and (
            result.get("items") or result.get("images")
            or _search_model_asset(str(result.get("query") or ""), result)
        ):
            receipt = _push_search_to_info_board(
                result,
                expand=True,
                pending=False,
                activate=True,
                sync=False,
            )
        target = str((receipt or {}).get("tab_id") or tab_id or "")
        committed = info_panel.set_answer(target, answer, entries=entries)
        sync_status_timeline_to_canvas()
        return committed
    except Exception as error:
        print("[muse] 搜索答案提交失败: %s" % error, flush=True)
        return {"ok": False, "error": str(error)[:300]}


def _run_web_extract_tool(url, question=""):
    page = deep_search.extract(
        url or "",
        get_setting=db.get_setting,
        query=question or "",
        include_images=True,
    )
    panel = None
    if page.get("ok"):
        panel = {
            "kind": "web",
            "title": page.get("title") or "网页预览",
            "url": page.get("url"),
            "data": {
                "url": page.get("url"),
                "title": page.get("title"),
                "site": page.get("site") or "",
                "summary": page.get("summary") or "",
                "images": page.get("images") or [],
                "paragraphs": page.get("paragraphs") or [],
                "full_text": page.get("text") or "",
            },
            "width": 680,
            "height": 540,
        }
    meta = dict(page)
    meta["panel"] = panel
    return page, meta



def _run_claude_code_tool(arguments, aid, request: Request = None, on_progress=None):
    args = arguments or {}
    mode = (args.get("mode") or "").strip() or None
    base = _claude_code_base_url(request)
    task = args.get("task") or ""

    # 聊天工具路径：走编排（快照/排队/Desk 预览）；同步等待本轮结果
    started = coding_orch.start_writing(
        aid,
        task,
        get_setting=db.get_setting,
        set_setting=db.set_setting,
        base_url=base,
        mode=mode or "external",
        cwd=args.get("cwd") or "",
        open_desk=True,
    )
    if started.get("queued"):
        return started.get("speech") or "已排队", {"ok": True, "queued": True}
    if not started.get("ok"):
        error_text = str(started.get("speech") or started.get("error") or "工作 Agent 没能启动")
        return error_text, {"ok": False, "error": error_text}
    run_id = started.get("run_id") or ""
    # 等待后台完成（聊天路径仍需工具结果）
    deadline = time.time() + float(args.get("timeout_s") or 300)
    result = None
    saw_running = False
    while time.time() < deadline:
        active = agent_runtime.get_active_run()
        if active and active.get("alive"):
            saw_running = True
        if saw_running and not (active and active.get("alive")):
            result = agent_runtime.get_result(run_id)
            break
        time.sleep(0.4)
    if result is None:
        result = {"ok": False, "error": "等待超时", "run_id": run_id}
    text = result.get("summary") or result.get("error") or ""
    if result.get("ok"):
        out = "工作 Agent 完成（cwd=%s）：\n%s" % (result.get("cwd"), text)
        if result.get("preview_path") or result.get("preview_url"):
            out += "\n预览（Desk）：%s" % (result.get("preview_url") or result.get("preview_path"))
    else:
        out = "工作 Agent 未完成：%s\n%s" % (result.get("error") or "", text)
    try:
        _remember_coding_run(aid, result, task=str(task))
        if result.get("ok") and result.get("preview_url"):
            coding_orch.ensure_preview_window(
                aid, url=result["preview_url"], open_native=False, base_url=base
            )
    except Exception:
        pass
    return out, result


def _parse_tool_arguments(raw):
    """容错解析模型工具参数 JSON。

    模型偶发输出损坏 JSON（长 HTML/内容里换行或引号没转义、多个 JSON 粘连、
    尾部残留文本），不能一失败就报错——写内容场景尤其常见。依次降级：
    1. 标准 json.loads；
    2. strict=False（允许字符串内裸换行/控制字符）；
    3. raw_decode 取第一个完整 JSON 对象（救尾部残留/粘连）；
    4. 去掉首尾非 JSON 内容后再试。
    都失败返回 None，调用方把原始内容回显给模型让它重试。
    """
    if not raw:
        return {}
    text = raw.strip()
    candidates = [text]
    # 尾部残留：截到最后一个成对的大括号再试
    depth = 0
    cut = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                cut = i + 1
                break
    if cut > 0 and cut < len(text):
        candidates.append(text[:cut])
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        try:
            obj = json.loads(cand, strict=False)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        try:
            obj, _ = json.JSONDecoder().raw_decode(cand.lstrip())
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


def _execute_chat_tool(name, arguments, aid, request: Request = None, *, voice_mode=False,
                       user_text=""):
    # 模型幻觉变体归一化：surface_update/surface_set 等 → surface_manage
    canonical = _action_registry.resolve(name) if name else None
    if canonical and canonical != name:
        name = canonical
    if name == "conversation_reply":
        mode = str((arguments or {}).get("mode") or "").strip()
        reply = str((arguments or {}).get("reply") or "").strip()
        if mode not in {"answer", "clarify"} or not reply:
            return "conversation_reply 缺少有效 mode/reply。", {
                "ok": False,
                "action": "conversation_reply",
                "response_mode": mode,
            }
        return reply, {
            "ok": True,
            "action": "conversation_reply",
            "response_mode": mode,
            "direct_reply": reply,
        }
    if name == "realtime_info":
        return _realtime_info_execute(arguments)
    if name == "surface_manage":
        return surface_skill.execute("surface_manage", arguments)
    if name == "surface_inspect":
        return surface_skill.execute("surface_inspect", arguments)
    if name == "surface_expect_input":
        return surface_skill.execute("surface_expect_input", arguments, aid=aid)
    if name == "surface_control":
        return surface_control.execute(arguments, aid=aid)
    if name == "device_control":
        return device_control.execute(arguments)
    if name == "canvas_control":
        return canvas_control.execute(arguments)
    if name == "object_control":
        return object_control.execute(arguments, ctx={
            "aid": aid,
            "request": request,
        })
    if name == "task_control":
        args = dict(arguments or {})
        kind = str(args.get("kind") or "")
        task_request = str(args.get("request") or "").strip()
        if kind in {"current_time", "date", "weather"}:
            out, meta = _realtime_info_execute({
                "kind": "time" if kind == "current_time" else kind,
                "location": args.get("location") or "",
            })
        elif kind == "web_search":
            wording_note = _query_differs_from_user_wording(task_request, user_text)
            include_visuals = (
                bool(args.get("include_visuals"))
                if "include_visuals" in args
                else (False if voice_mode else None)
            )
            out, meta = _execute_chat_tool(
                "web_search", {
                    "query": task_request,
                    "want": str(args.get("want") or ""),
                    "grounding": str(args.get("grounding") or ""),
                    "research_depth": args.get("research_depth") or "quick",
                    "search_queries": args.get("search_queries") or [],
                    "include_visuals": include_visuals,
                }, aid, request,
                voice_mode=voice_mode,
            )
            if wording_note:
                # 差异写进工具结果，模型看得到就能自己决定坚持纠正还是重查
                if isinstance(meta, dict):
                    meta["wording_note"] = wording_note
                    meta["answer_context"] = "%s\n%s" % (
                        wording_note, str(meta.get("answer_context") or ""),
                    )
                out = "%s\n%s" % (wording_note, out)
        elif kind == "web_extract":
            out, meta = _execute_chat_tool(
                "web_extract",
                {"url": args.get("url") or "", "question": task_request},
                aid, request, voice_mode=voice_mode,
            )
        elif kind.startswith("coding_"):
            out, meta = _execute_chat_tool(
                "coding_flow",
                {
                    "action": kind[len("coding_"):],
                    "request": task_request,
                    "plan_steps": args.get("plan_steps") or [],
                    "risks": args.get("risks") or [],
                },
                aid, request, voice_mode=voice_mode,
            )
        else:
            return "task_control 缺少有效 kind。", {
                "ok": False,
                "action": "task_control",
                "task_kind": kind,
                "error": "未知任务类型",
            }
        meta = dict(meta or {})
        meta["task_kind"] = kind
        return out, meta
    if name == "led_control":
        # 兼容旧对话/旧客户端；新工具统一走 device_control。
        args = dict(arguments or {})
        args.setdefault("device_id", device_control.DEFAULT_DEVICE_ID)
        return device_control.execute(args)
    if name == "coding_flow":
        args = arguments if isinstance(arguments, dict) else {}
        action = str(args.get("action") or "")
        request_text = str(args.get("request") or "").strip()
        if action in ("confirm", "write", "delete"):
            meta = {
                "ok": False,
                "action": action,
                "reason": "approval_required",
                "approval_channel": "plan_surface_submit_only",
            }
            return json.dumps(meta, ensure_ascii=False), meta
        if action == "plan":
            reply = _route_coding_plan(
                aid=aid,
                msg=request_text,
                plan_steps=args.get("plan_steps") if isinstance(args.get("plan_steps"), list) else [],
                risks=args.get("risks") if isinstance(args.get("risks"), list) else [],
            )
            meta = {
                "ok": True,
                "action": "plan",
                "phase": coding_fsm.get_phase(aid),
                "work_order": (coding_fsm.load(aid).get("work_order") or {}),
            }
            st = coding_fsm.load(aid)
            brief = st.get("brief") or {}
            last = st.get("last_run") or {}
            meta["runtime_truth"] = {
                "phase": st.get("phase") or "idle",
                "cwd": brief.get("cwd") or "",
                "preview_url": brief.get("preview_url") or "",
                "last_run_ok": last.get("ok"),
                "verified_changes": bool(last.get("verified_changes")),
                "task_outcome": last.get("task_outcome") or "",
                "project_files_exist_by_receipt": bool(
                    last.get("ok") and last.get("verified_changes")
                ),
            }
            return json.dumps({"receipt": meta, "fact": reply}, ensure_ascii=False), meta
        action_map = {
            "clarify": "coding_clarify",
            "write": "coding_write",
            "status": "coding_status",
            "cancel": "coding_cancel",
            "revert": "coding_revert",
        }
        mapped = action_map.get(action)
        if not mapped:
            return "coding_flow failed: unknown action.", {"ok": False, "action": args.get("action")}
        out, meta = _execute_voice_action_tool({
            "action": mapped,
            "request": request_text,
        }, aid, request=request)
        meta = dict(meta or {})
        st = coding_fsm.load(aid)
        brief = st.get("brief") or {}
        last = st.get("last_run") or {}
        meta["runtime_truth"] = {
            "phase": st.get("phase") or "idle",
            "cwd": brief.get("cwd") or "",
            "preview_url": brief.get("preview_url") or "",
            "last_run_ok": last.get("ok"),
            "verified_changes": bool(last.get("verified_changes")),
            "task_outcome": last.get("task_outcome") or "",
            "project_files_exist_by_receipt": bool(
                last.get("ok") and last.get("verified_changes")
            ),
        }
        return json.dumps({"receipt": meta or {}, "fact": out}, ensure_ascii=False), meta
    if name == "web_search":
        search_args = arguments if isinstance(arguments, dict) else {}
        query = str(search_args.get("query") or "").strip()
        want = str(search_args.get("want") or "").strip().lower()
        grounding = str(search_args.get("grounding") or "").strip().lower()
        if want == "images" and query:
            # 用户要看图：直接查图片索引并以图成篇。以前一律走网页检索，
            # 「显示一张故宫的图片」被当网页查询，自然拿不出可展示的图；
            # 也不该套用为事实核查设计的「弱证据→不能下结论」那条路。
            image_result = deep_search.search_images(query, get_setting=db.get_setting)
            image_result["want"] = "images"
            _push_search_to_info_board(image_result, expand=True, pending=False)
            return deep_search.format_tool_result(image_result), image_result
        include_visuals = (
            bool(search_args.get("include_visuals"))
            if "include_visuals" in search_args
            else (False if voice_mode else None)
        )
        staged_canvas = _begin_search_canvas(query) if voice_mode and query else None
        result = _web_search_with_background(
            query,
            voice_mode=voice_mode,
            research_depth=search_args.get("research_depth") or "quick",
            search_queries=search_args.get("search_queries") or [],
            include_visuals=include_visuals,
            grounding=grounding,
        )
        # 意图跟着结果走：证据落盘与最终答案提交是两次推送，
        # 只在其中一次带 want，另一次就会按默认模板重建文档。
        if want:
            result["want"] = want
        if grounding:
            result["grounding"] = grounding
        result["research_depth"] = (
            "thorough"
            if str(search_args.get("research_depth") or "").lower() == "thorough"
            else "quick"
        )
        # 记下真实链接供 result.N 引用：模型不写 URL，也就编不出 rickroll 那种假链接。
        # 弱证据同样记录——URL 依然不进模型上下文，但用户说「打开那个」时要开得对。
        try:
            from control_plane import search_results
            search_results.remember(query, result.get("items") or [])
        except Exception as error:
            print("[muse] 搜索结果引用表写入失败: %s" % error, flush=True)
        # Voice keeps evidence staged until the final natural answer is ready.
        # Text mode has no two-round voice canvas lifecycle and may commit now.
        #
        # 证据必须当场落盘：面板内容原本只在模型「搜完再调一次 conversation_reply」
        # 时才提交，可模型经常搜完就直接开口（continue_after=false，不再调工具），
        # 于是面板永远停在只有标题的暂存态——用户看到的就是一句没有内容的占位结论。
        # 这里先把证据写进画布；随后若真有最终自然答案，_commit_search_answer 会
        # 用它覆盖首屏结论，两条路径不冲突。
        if voice_mode and (result.get("items") or result.get("images")):
            evidence_receipt = _push_search_to_info_board(
                result, expand=True, pending=False, activate=True, sync=True,
            )
            canvas_receipt = evidence_receipt or staged_canvas
        else:
            canvas_receipt = staged_canvas if voice_mode else _push_search_to_info_board(result)
        if canvas_receipt:
            result["canvas"] = canvas_receipt
        formatted = deep_search.format_tool_result(result)
        if canvas_receipt and canvas_receipt.get("visible"):
            formatted += (
                "\n【显示回执】搜索结果已进入可操作的研究画布标签页（不是独立窗口）；"
                "实际节点类型：%s。"
                % "、".join(canvas_receipt.get("node_types") or [])
            )
        return formatted, result
    if name == "web_extract":
        page, meta = _run_web_extract_tool(
            (arguments or {}).get("url"),
            (arguments or {}).get("question") or "",
        )
        _push_search_to_info_board(page)
        return deep_search.format_extract_result(page), meta
    if name == "claude_code_run":
        return _run_claude_code_tool(arguments, aid, request=request)
    return "（未知工具：%s）" % name, None


def _search_hint_system(*, voice_mode=False):
    if not _web_search_enabled():
        return None
    if voice_mode:
        return (
            "需要联网检索时调用 task_control(kind=web_search, request=问题)；"
            "用户给了链接并要求读取时调用 task_control(kind=web_extract, url=链接, request=问题)。"
            "凡涉及时效、新闻、最新进展、真假核实、训练知识可能过时的事实问题必须先查，不要编造。"
            "范围很宽且结果取决于个人偏好时，先问一个最关键的条件，不要拿排行榜页凑答案。"
            "搜索结果会自动推送到常驻信息窗口，不要再建一个页面复制原样结果。"
        )
    return (
        "你可以使用 web_search（联网检索+综合摘要+原文/配图）和 web_extract（打开指定链接抽正文/图）。"
        "凡涉及时效、新闻、最新进展、真假核实、训练知识可能过时的事实问题，必须先 web_search，"
        "范围很宽且结果取决于个人偏好时，先问一个最关键的条件，不要拿排行榜页凑答案。"
        "回答带上来源链接；用户要打开某条链接或第N条详情时用 web_extract。不要编造。"
        "搜索/查询结果会自动推送到常驻信息窗口（info-board），不需要你手动放窗口。"
        "信息窗口是常驻的（不可关闭/删除），内容会被新搜索覆盖。"
        "只有用户明确要求把某条信息专项展示（如做对比、做清单、开专题窗口）时，"
        "才用 surface_manage 新建专门窗口从信息里整理，不要新建窗口放搜索原样结果。"
    )


def _claude_code_hint_system():
    if not _claude_code_enabled():
        return None
    try:
        ext = str(coding_path_policy.default_external_root(db.get_setting))
    except Exception:
        ext = str(Path.home() / "Documents" / "MuseWork")
    return (
        "你已接通通用工作 Agent，它通过固定对象 project.active 操作，不暴露供应商名。"
        "写改代码、修 bug、跑检查、建项目、PCB/CAD 工作先 invoke plan；用户确认后 invoke confirm。"
        "执行中用户补充要求用 update，不要另起一个相互冲突的任务。"
        "未拿到运行时与文件哈希回执前，禁止说已写好、已改好或文件已更新。"
        "默认外部工作目录为 %s；不要自行编造其他用户路径。"
        "闲聊、普通问答、联网搜索、看摄像头不要启动工作 Agent。"
    ) % ext

db.init_db()
app = FastAPI(title="Muse")
app.include_router(core_proxy_router)
app.include_router(admin_router)
app.include_router(core_router)
app.include_router(tts_router)
app.include_router(skills_router)
app.include_router(devices_router)


# ============ 动作注册表接线 ============
# 动作流范式：模型输出动作名 → 程序查表执行 → 回执。执行器统一走 _execute_chat_tool
# 分发（行为与旧工具轮完全一致），动作名即工具名；冲突域决定并行还是串行。
_action_registry = _ActionRegistry()

# 只读动作：调用它们不代表发生了任何变更。光说不做拦截判定"是否有变更回执"时，
# 这些动作即使 ok:true 也不算完成。surface_control/device_control/coding_flow 属于变更
# 动作（即便其内部某个子动作是只读 status，模型调用它通常也伴随实际改窗/控灯意图）。
_READONLY_ACTION_NAMES = frozenset({
    "surface_inspect",
    "realtime_info",
    "web_search",
    "web_extract",
    "deep_search",
    "extract",
    "conversation_reply",
})


# 只读的 task_control 任务类型：查资料/查时间天气，不改变任何外部状态。
# coding_plan/cancel/revert 是真动作，不在此列。
_READONLY_TASK_KINDS = frozenset({
    "current_time", "date", "weather", "web_search", "web_extract", "coding_status",
    "coding_clarify",
})


def _answer_only_tools(tools):
    """检索终态后彻底撤掉工具，强制下一轮生成自然语言。

    部分 OpenAI-compatible 上游会忽略缩减后的 tools 列表，继续复用历史里的
    task_control 调用。传 ``None`` 会让请求层同时省略 tools 和 tool_choice，
    搜索能力在程序层真实消失，而不是继续依赖模型遵守提示。

    试过改成「保留 tools + tool_choice=none」来保住前缀缓存，实测无效：
    搜索轮的命中率两种做法都是 2688 token（57~68%），而且不受影响的 round 0
    同样是 2688——天花板另有原因，不是 tool_choice 造成的。既然缓存上没有
    差别，就保留行为保证更强的这一版。
    """
    del tools
    return None


def _is_readonly_call(action_name, arguments) -> bool:
    """本次调用是否「只读」——按函数名+参数判定。

    固定协议后的常驻函数名是 conversation_reply/task_control/object_control；
    原先的 web_search、surface_inspect 等
    降级成了参数。_READONLY_ACTION_NAMES 仍按旧函数名匹配，导致搜索永远不算
    只读、空转保护对搜索完全失效（实测连搜 11 次才被硬上限拦住）。这里与
    _receipt_is_mutation 采用同一套「名字+参数」口径。
    未知工具按「非只读」处理：宁可放过一轮，也不要把真实动作误判成空转。
    """
    name = str(action_name or "")
    args = arguments if isinstance(arguments, dict) else {}
    if name in _READONLY_ACTION_NAMES:
        return True
    if name == "task_control":
        return str(args.get("kind") or "") in _READONLY_TASK_KINDS
    if name == "canvas_control":
        return str(args.get("action") or "") == "inspect"
    if name == "object_control":
        return str(args.get("op") or "") == "inspect"
    if name in {"surface_control", "surface_manage"}:
        return str(args.get("action") or "") in {"status", "inspect"}
    if name in {"device_control", "led_control"}:
        return str(args.get("action") or args.get("capability") or "") == "status"
    return False


def _receipt_is_mutation(action_name, meta):
    """Classify from the typed receipt, never from the user's wording."""
    name = str(action_name or "")
    receipt = meta if isinstance(meta, dict) else {}
    if name in _READONLY_ACTION_NAMES:
        return False
    if name in {"surface_control", "surface_manage"}:
        return str(receipt.get("action") or "") not in {"status", "inspect", ""}
    if name in {"device_control", "led_control"}:
        capability = receipt.get("capability") or receipt.get("action")
        return str(capability or "") != "status"
    if name == "task_control":
        return str(receipt.get("task_kind") or "") in {
            "coding_plan", "coding_cancel", "coding_revert",
        }
    if name == "canvas_control":
        return str(receipt.get("action") or "") == "apply" and bool(receipt.get("changed"))
    if name == "object_control":
        return (
            str(receipt.get("op") or "") in {"apply", "invoke"}
            and bool(receipt.get("changed", True))
            and bool(receipt.get("verified_target"))
        )
    return True

_PROGRESS_PROTOCOL_RE = re.compile(
    r"<\/?(?:tool_calls|invoke)|DSML|function_call|recipient=|\{\s*\"(?:kind|action)\"",
    re.I,
)
_PROGRESS_COMPLETION_RE = re.compile(
    r"(?:已经|已)(?:查到|搜到|完成|处理|打开|关闭|改好)|(?:查|搜)到了|弄好了|完成了",
)
_TOOL_PROGRESS_GRACE_S = 0.9


def _action_progress_starter(actions):
    """Use the model's task-specific transition sentence for genuinely long work.

    There is deliberately no phrase bank here. ``speak_while`` is the model's
    semantic decision; ``progress_reply`` is its natural wording for this exact
    request. Runtime adds a short grace period before actually speaking it, so
    an operation that finishes quickly stays silent even if it was overestimated.
    """
    for action in actions or []:
        args = (action or {}).get("args")
        args = args if isinstance(args, dict) else {}
        if not bool(args.get("speak_while")):
            continue
        text = re.sub(r"\s+", " ", str(args.get("progress_reply") or "")).strip()
        if not text or _PROGRESS_PROTOCOL_RE.search(text) or _PROGRESS_COMPLETION_RE.search(text):
            continue
        # 这只是垫场，不是第二份答案。按 Unicode 字符裁短，避免模型偶发塞进长解释。
        chars = list(text)
        if len(chars) > 36:
            text = "".join(chars[:36]).rstrip("，,；;：:") + "。"
        return text
    return ""


def _batch_requests_continuation(actions):
    """Whether the model declared a receipt-dependent next step."""
    for action in list(actions or []):
        args = (action or {}).get("args")
        if not isinstance(args, dict) or not bool(args.get("continue_after")):
            continue
        # 搜索回执已经自动创建并显示研究画布。“显示/预览”不是第二步；只有
        # 模型在调用前声明了用户要求的具体后处理，才保留画布工具。这样复杂
        # 指令仍可组合，普通搜索则不会在结果出来后临时编造几轮 inspect/apply。
        if (
            str((action or {}).get("action") or "") == "task_control"
            and str(args.get("kind") or "") == "web_search"
            and not str(args.get("post_search_goal") or "").strip()
        ):
            continue
        return True
    return False


def _batch_closes_readonly_phase(actions):
    """Whether this declared-terminal batch must be followed by an answer.

    ``continue_after=false`` is a transaction boundary, not a suggestion. A
    search/extract/status round may use one LLM pass to summarize its receipt,
    but it may not start another retrieval phase unless that next step was
    explicitly declared before execution.
    """
    batch = list(actions or [])
    if not batch or _batch_requests_continuation(batch):
        return False
    calls = [
        (
            str((item or {}).get("action") or ""),
            (item or {}).get("args")
            if isinstance((item or {}).get("args"), dict)
            else {},
        )
        for item in batch
    ]
    # 查目标 ≠ 查资料。object_control 的 inspect 存在的意义就是「先找到 target
    # 再动手」，把它当成阶段终点，等于在模型刚找到窗口时把工具收走——实测
    # 「把之前那个 GitHub 窗口显示出来」正是这样断在第二步，然后蹦出一句兜底话术。
    # 空转由 spin 检测兜底，这里不必用收工具的方式防它。
    if any(
        name == "object_control" and str((args or {}).get("op") or "") == "inspect"
        for name, args in calls
    ):
        return False
    return (
        any(name != "conversation_reply" for name, _ in calls)
        and all(_is_readonly_call(name, args) for name, args in calls)
    )


def _transaction_action_key(action):
    """稳定标识同一轮里的同一动作；只看结构化参数，不分析用户文本。"""
    item = action if isinstance(action, dict) else {}
    name = str(item.get("action") or "")
    args = item.get("args") if isinstance(item.get("args"), dict) else {}
    semantic_args = {
        key: value
        for key, value in args.items()
        if key not in {"continue_after", "reply", "speak_while", "progress_reply"}
    }
    if not name:
        return ""
    return "%s:%s" % (
        name,
        json.dumps(semantic_args, ensure_ascii=False, sort_keys=True),
    )


_CONTEXTUAL_RECEIPT_ACTIONS = frozenset({
    "surface_control", "device_control", "canvas_control", "object_control",
})


def _receipt_direct_reply(action_name, meta):
    """Use first-round natural copy; fixed mutation receipts fall back to the model."""
    receipt = meta if isinstance(meta, dict) else {}
    direct = str(receipt.get("direct_reply") or "").strip()
    if direct:
        return direct
    if str(action_name or "") in _CONTEXTUAL_RECEIPT_ACTIONS:
        return ""
    return str(receipt.get("speech") or "").strip()


def _verified_receipt_direct_reply(action_name, result):
    """Only a successful typed receipt may authorize a receipt-backed reply."""
    action_result = result if isinstance(result, dict) else {}
    if not action_result.get("ok"):
        return ""
    meta = (
        action_result.get("meta")
        if isinstance(action_result.get("meta"), dict)
        else {}
    )
    return _receipt_direct_reply(action_name, meta)


def _should_emit_legacy_panel(action_name, meta):
    """Search owns the research canvas; never duplicate its raw payload to UI."""
    receipt = meta if isinstance(meta, dict) else {}
    if (
        str(action_name or "") == "task_control"
        and str(receipt.get("task_kind") or "") == "web_search"
        and isinstance(receipt.get("canvas"), dict)
    ):
        return False
    return bool(receipt.get("panel"))


# 最近几轮「这句回复当时是不是真调了工具」。客户端传上来的历史只有说出去的
# 话，工具调用被剥掉了——模型在上下文里看到的规律就成了「用户要动作 → 助手
# 直接说一句完成」，这是一份完美的反面示范。实测：带上真实历史时，
# 「把哔哩哔哩窗口关上」8/8 都直接编出「Bilibili已关闭」而根本不调工具；
# 换成两条合成历史则 13/13 正常。
_TURN_ACTS = {}
_TURN_ACTS_LOCK = threading.Lock()
_TURN_ACTS_KEEP = 40
_TURN_ACTS_LOADED = False


def _turn_acts_path():
    from pathlib import Path

    base = Path(__file__).resolve().parent / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "turn_acts.json"


def _load_turn_acts():
    """标注必须扛得住重启：内存里的记录一没，历史又会开始教坏模型。

    实测：带标注的历史 4/4 正常；同一段历史没有标注时 6/6 全部编造完成，
    而且在提示里明写「历史里的工具调用被剥掉了」也救不回来——上下文里的
    示范压得过提示词里的规则。所以这份事实必须落盘。
    """
    global _TURN_ACTS_LOADED
    if _TURN_ACTS_LOADED:
        return
    _TURN_ACTS_LOADED = True
    try:
        raw = json.loads(_turn_acts_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, list):
                    _TURN_ACTS[int(key)] = value[-_TURN_ACTS_KEEP:]
    except Exception:
        pass


def _save_turn_acts_locked():
    try:
        _turn_acts_path().write_text(
            json.dumps({str(k): v for k, v in _TURN_ACTS.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _compact_tool_content(raw, limit=220):
    """回执压缩：原样贴回去会把提示词撑爆（inspect 回执有 1KB 出头）。

    只留能教会模型「这次做成没做成、结果是什么」的字段。
    """
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return str(raw or "")[:limit]
    if not isinstance(data, dict):
        return str(raw or "")[:limit]
    keep = {}
    for key in ("ok", "op", "command", "target_id", "reason", "after", "changed"):
        if data.get(key) is not None:
            keep[key] = data[key]
    if not keep:
        keep = {"ok": bool(data.get("ok"))}
    return json.dumps(keep, ensure_ascii=False)[:limit]


def _remember_turn_messages(aid, user_text, structured):
    """存下本轮真实的调用与回执，供下一轮原样贴回上下文。

    历史只留台词时，模型在上下文里读到的规律是「用户要动作 → 助手说一句完成」，
    实测会照着编。贴纸条（另起一条 system 说明「当时调过工具」）能挡住，但它是
    转述：教不了调用的形状，也给不了失败示范。这里存的是模型自己要产出的那个
    格式，下一轮原样回放。
    """
    body = re.sub(r"\s+", " ", str(user_text or "")).strip()
    if not body or not structured:
        return
    with _TURN_ACTS_LOCK:
        _load_turn_acts()
        log = _TURN_ACTS.setdefault(int(aid or 0), [])
        for item in log:
            if item.get("user") == body[:120] and item.get("messages"):
                item["messages"] = structured
                break
        else:
            log.append({"user": body[:120], "messages": structured})
        del log[:-_TURN_ACTS_KEEP]
        _save_turn_acts_locked()


def _replay_recorded_turns(aid, history, *, max_turns=4):
    """把「这句回复当时真的调过工具」这个事实补回历史，但不贴回调用本身。

    背景：客户端传的历史只有台词，工具调用被剥掉了，模型据此学会「说一句完成
    即可」（「Bilibili已关闭」而窗口还开着）。

    试过把真实的 assistant(tool_calls)+tool(回执) 原样贴回去——那是模型自己要
    产出的格式，理论上最好。实测两次翻车：先是把「DJX 价格」这个错误检索抄到
    「把哔哩哔哩关上」「计时35分钟」上；换成只回放动作之后，又把
    「inspect 哔哩哔哩 + show」抄到「和我闲聊」「零件还没到齐呢」这种纯闲聊上，
    而且每抄一次就落盘一次，越滚越大。

    调用是可复制的模板，模型会连内容一起抄；一句中文陈述不是。纸条版当初
    同样治好了说谎（开窗关窗 6/6 正确），代价小得多。所以取纸条版。
    """
    with _TURN_ACTS_LOCK:
        _load_turn_acts()
        log = [dict(item) for item in (_TURN_ACTS.get(int(aid or 0)) or [])]
    if not log:
        return history
    by_user = {}
    for item in log:
        acted = [
            m for m in (item.get("messages") or [])
            if isinstance(m, dict) and m.get("tool_calls")
        ]
        if not acted:
            continue
        try:
            name = acted[0]["tool_calls"][0]["function"]["name"]
        except Exception:
            name = "工具"
        by_user.setdefault(item.get("user") or "", name)
    if not by_user:
        return history
    out = []
    noted = 0
    index = 0
    items = list(history or [])
    while index < len(items):
        item = items[index]
        out.append(item)
        pairs_with_assistant = (
            str(item.get("role")) == "user"
            and index + 1 < len(items)
            and str(items[index + 1].get("role")) == "assistant"
        )
        if pairs_with_assistant and noted < max_turns:
            body = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()[:120]
            name = by_user.get(body)
            if name:
                out.append(items[index + 1])
                out.append({
                    "role": "system",
                    "content": "（上一句助手回复对应的真实动作：调用 %s，回执 ok:true）" % name,
                })
                noted += 1
                index += 2
                continue
        index += 1
    return out


def _voice_answer_retry(*, text, had_tool_call, had_mutation_receipt,
                       search_result, constrained_empty):
    """一轮说出口之前的统一体检：要不要回炉、为什么。

    这里原先是五个各写各的 if（协议残留、光说不做、反问代替动作、空输出、
    证据不足），彼此不知道对方存在——一轮里连触两个就会多加一次 action_round。
    合成一个判定：按严重程度排序，命中即返回原因码与要对模型说的话，
    每轮最多回炉一次。

    判定只看结构化事实（有没有调工具、有没有变更回执、检索证据够不够、
    话里提到的对象在不在运行时目录里），不解析用户语义。
    """
    body = str(text or "").strip()
    if not body:
        return "empty_output", (
            "你刚才没有输出任何可以念给用户听的话（也可能混进了工具语法）。"
            "现在用一两句自然中文直接说：手上拿到了什么、卡在哪一步、"
            "建议用户怎么做。不要输出任何函数或工具语法。"
        )
    if constrained_empty and search_result:
        return "weak_evidence", (
            "这轮检索的证据不足以支撑你刚才那个笃定的说法。"
            "用你自己的话重说一遍：找到了哪些相关的东西、还缺哪一条才敢下结论、"
            "建议用户怎么问更容易查到。不要编没有出处的数字或链接。"
        )
    if not had_mutation_receipt and _mentions_live_object(body):
        return "unbacked_claim", (
            "你刚才没有调用任何工具，所以外面什么都没变。"
            "如果用户要的是动作，现在就调工具真正去做；"
            "如果只是聊天或转述现状，就把要说的话原样再说一遍，但不许说成已经做完。"
        )
    # 这里曾有第四条 clarify_instead_of_act：「话以问号结尾 + 没调工具」判为
    # 该动手却在反问，回炉重来。实测 80 轮触发 9 次，只有 1 次是真的
    # （「薛之谦和席文是谁的？」该去搜却在反问，回炉救回来了）；另外 8 次全是
    # 纯闲聊——「你好」「可以和我用英文沟通吗？」「Just chat in English, OK?」
    # 「Hi, Vivian.」，回炉之后模型也没去调工具，因为本来就不该调。
    #
    # 判据本身站不住：正常说话经常以问号结尾，问号不能当「该动手」的证据。
    # 8 次误伤换 1 次命中，而每次误伤白烧一个完整 LLM 来回（中位 1.46 秒），
    # 摊到全部轮次上是 11% 的对话平白慢一倍。那 1 次真该搜却反问的，该由
    # 路由卡在事前解决，不该靠事后回炉去抓。
    return "", ""



def _mentions_live_object(text) -> bool:
    """这句话提到了世界里真实存在的对象吗？

    不是在分析语义，是拿运行时对象目录里的名字去比对——「Bilibili」「桌面灯带」
    这些名字来自 registry，不是手写的模式。用它来判断「模型在谈论某个对象」，
    从而决定要不要先核对再开口。
    """
    body = str(text or "")
    if not body:
        return False
    try:
        from tools import object_control

        object_control.ensure_builtin_provider()
        from control_plane.object_registry import object_registry

        catalog = object_registry.world()
    except Exception:
        return False
    for item in catalog or []:
        if not isinstance(item, dict):
            continue
        labels = [item.get("name")] + list(item.get("aliases") or [])
        # 状态里的字符串值也算：模型会说「输出切回 AirPods Pro 了」，
        # 那不是对象名而是对象的当前值，判据只认名字就漏了。
        state = item.get("state") if isinstance(item.get("state"), dict) else {}
        labels += [v for v in state.values() if isinstance(v, str) and 3 <= len(v) <= 40]
        for label in labels:
            name = str(label or "").strip()
            if len(name) >= 2 and name.lower() in body.lower():
                return True
    return False


def _voice_no_tool_response(parts):
    """Text-only answer round: recover a reply from DSML, never speak protocol errors."""
    text = "".join(parts).strip()
    dsml_calls, visible = dsml_gw.parse_dsml_tool_calls(text)
    for call in dsml_calls:
        function = (call or {}).get("function") or {}
        if str(function.get("name") or "") != "conversation_reply":
            continue
        args = _parse_tool_arguments(function.get("arguments") or "{}") or {}
        reply = re.sub(r"\s+", " ", str(args.get("reply") or "")).strip()
        if reply and not _PROGRESS_PROTOCOL_RE.search(reply):
            return reply, str(args.get("mode") or "answer")
    clean_visible = str(visible or "").strip()
    if clean_visible and clean_visible != text and not _PROGRESS_PROTOCOL_RE.search(clean_visible):
        return clean_visible, "answer"
    lowered = text.lower()
    protocol_markers = (
        "<tool_calls", "</tool_calls", "<invoke", "</invoke",
        "dsml", "function_call", "recipient=", "canvas_control.apply",
    )
    if any(marker in lowered for marker in protocol_markers):
        return "", "answer"
    return text, "answer"


def _forced_text_answer_instruction():
    """Instruction used after retrieval tools have been removed from the request."""
    return (
        "检索已经结束，而且下一轮请求不会提供任何工具。直接输出要给用户听的自然中文，"
        "不要调用 conversation_reply 或其他函数，也不要输出工具语法、JSON、DSML。"
        "第一句给结论；结果不足就直说哪一点没查到，再说明现有信息。"
        "讲到多个具体对象（项目、产品、型号、地点）时，每个对象名用 **粗体** 标一次，"
        "紧跟它的那句话写清关键信息——信息面板会照此列成条目，"
        "所以别把同一个对象拆到几处讲。"
        "只依据已有回执，不继续搜索，不编造回执里没有的数字。"
        "回执里的 evidence_quality 和 answerable 是硬约束；answerable=false 时只能说"
        "『本轮没找到明确匹配』，不能把相近页面说成找到了，也不能推断目标不存在。"
        "此时也不要从历史对话或自身记忆补充产品名、价格或其他候选。"
    )


_SEARCH_UNCERTAINTY_RE = re.compile(
    r"(?:没(?:有|查到|找到)|未(?:查到|找到|确认)|无法确认|不能确认|"
    r"不(?:能|足以)确定|不确定|证据不足|资料不足|匹配度不足)"
)


def _constrain_search_answer(answer, result):
    """Fail closed when retrieval explicitly says the question is unanswerable.

    The model still writes the natural sentence. We only keep its first
    uncertainty-bearing conclusion, preventing later prose from reviving a
    weak candidate as a recommendation. This is evidence policy, not a
    product- or query-specific patch.
    """
    text = re.sub(r"\s+", " ", str(answer or "")).strip()
    meta = result if isinstance(result, dict) else {}
    weak = (
        meta.get("answerable") is False
        or str(meta.get("evidence_quality") or "").lower() == "weak"
    )
    if not weak or not text:
        return text
    # 检索是「补充」而不是「依据」时，查不到不该把模型写好的回答整段丢掉。
    # 真实事故：用户问「我在洞洞板下走飞线可不可以」——这是他自己就该会答的
    # 常识问题，检索没命中，却被换成一句「本轮公开结果没有明确确认…，
    # 我暂时不能下结论」。失败即收口是给事实/型号/价格/链接这类问题准备的。
    if str(meta.get("grounding") or "").lower() == "helpful":
        return text
    first = (re.split(r"(?<=[。！？!?])", text, maxsplit=1)[0] or "").strip()
    if first and _SEARCH_UNCERTAINTY_RE.search(first):
        return first
    query = re.sub(r"\s+", " ", str(meta.get("query") or "这个问题")).strip()
    # 到这里说明证据不足、模型却写了个笃定的答案。以前在这里换成一句罐头话，
    # 结果把它真找到的东西也一起藏了（「树莓派5 价格」明明搜到了立创商城的报价页）。
    # 现在返回空串：调用方会让模型自己重说一遍——说清找到了什么、缺什么。
    return ""


def _is_benign_receipt(meta):
    """良性无操作：不该被当作失败播报的回执。

    典型是「删除所有页面」批量里混进的常驻窗（info-board/状态栏，本就不允许删）、
    以及关/删一个本来就不存在的窗口（目标已达成）。这些若报「有一项没成功」，
    会和真正成功的「窗口已删除」拼成自相矛盾的一句话，把用户绕晕。
    """
    meta = meta if isinstance(meta, dict) else {}
    reason = str(meta.get("reason") or "").strip()
    if reason == "pinned_surface":
        return True
    action = str(meta.get("action") or "").strip()
    if reason == "surface_not_found" and action in ("close", "delete"):
        return True
    return False


def _receipt_failure_text(meta, result, default):
    """真实失败原因优先取 meta.detail/error（reason 说明在 meta 里，不在 result 顶层）。"""
    meta = meta if isinstance(meta, dict) else {}
    result = result if isinstance(result, dict) else {}
    return str(
        meta.get("error")
        or meta.get("detail")
        or result.get("detail")
        or result.get("error")
        or default
    ).strip()


def _batch_direct_reply(results):
    """把已提交事务的成功回执收敛成终态播报；真失败不代劳，交给模型解释。"""
    speeches = []
    for result in list(results or []):
        result = result if isinstance(result, dict) else {}
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
        if result.get("ok"):
            speech = str(
                meta.get("direct_reply") or meta.get("speech") or ""
            ).strip()
            if not speech:
                return ""
        elif _is_benign_receipt(meta):
            # 常驻窗保护 / 目标本就不存在：良性无操作，不计入失败，不播报。
            continue
        else:
            # 真失败：不生成固定话术，让模型基于回执自己解释原因。
            return ""
        if speech not in speeches:
            speeches.append(speech)
    return "，".join(speeches)


def _register_voice_action(name, conflicts):
    def impl(args, ctx):
        ctx = ctx if isinstance(ctx, dict) else {}
        with coding_turn_trace.action_context(
            ctx.get("trace_id") or "", actor="voice_tool:%s" % name
        ):
            # voice 专用工具执行：web_search 走快路径（basic 摘要先出声，
            # 正文后台补），避免语音对话被深度检索阻塞十几秒。
            return _execute_chat_tool(
                name, args, ctx.get("aid"), ctx.get("request"),
                voice_mode=True,
                user_text=str(ctx.get("user_message") or ""),
            )
    _action_registry.register(name, impl, conflicts=conflicts)


def _surface_voice_action_wrapper(fn, name):
    def impl(args, ctx):
        ctx = ctx if isinstance(ctx, dict) else {}
        with coding_turn_trace.action_context(
            ctx.get("trace_id") or "", actor="voice_tool:%s" % name
        ):
            return fn(args, ctx)
    return impl


# 窗口三动作由 surfaces skill 自注册（带 trace 包装）
surface_skill.register(_action_registry, wrapper=_surface_voice_action_wrapper)
# 高层页面与 IoT 能力工具；底层 surface/LED 仅作为兼容别名保留。
surface_control.register(_action_registry, wrapper=_surface_voice_action_wrapper)
device_control.register(_action_registry, wrapper=_surface_voice_action_wrapper)
canvas_control.register(_action_registry, wrapper=_surface_voice_action_wrapper)
object_control.register(_action_registry, wrapper=_surface_voice_action_wrapper)
_register_voice_action("coding_flow", "fsm")
_register_voice_action("claude_code_run", "fsm")
_register_voice_action("web_search", None)
_register_voice_action("web_extract", None)
_register_voice_action("realtime_info", None)
_register_voice_action(
    "task_control",
    lambda args: (
        ("task_fsm",)
        if str((args or {}).get("kind") or "").startswith("coding_")
        else None
    ),
)
_register_voice_action("conversation_reply", None)



@app.post("/api/agents/{aid}/chat")
def api_agent_chat(aid: int, payload: dict = Body(...)):
    """不连设备的快速试聊：用该智能体的 LLM + 人设直接对话（openai 兼容）。"""
    agent = db.get_agent(aid)
    if not agent:
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    msg = (payload.get("message") or "").strip()
    if not msg:
        return JSONResponse({"ok": False, "error": "空消息"}, status_code=400)
    lm = (agent.get("modules") or {}).get("LLM") or {}
    name = lm.get("selected")
    if not name:
        return {"ok": False, "error": "该智能体未选择 LLM"}
    blk = dict(db.provider_catalog().get("LLM", {}).get(name, {}) or {})
    blk.update(lm.get("overrides") or {})
    if blk.get("type") != "openai":
        return {"ok": False, "error": "快速试聊目前仅支持 openai 兼容 LLM（DeepSeek/ChatGLM 等），当前类型: %s" % blk.get("type")}
    key, url, model = blk.get("api_key"), blk.get("url"), blk.get("model_name")
    if not key or "你的" in str(key) or "请替换" in str(key):
        return {"ok": False, "error": "该智能体的 LLM 未填写 api_key"}
    try:
        client = _openai_client(url, key)
        messages = []
        if agent.get("prompt"):
            messages.append({"role": "system", "content": agent["prompt"]})
        _inject_agent_context_messages(messages, aid, voice_mode=False)
        for h in (payload.get("history") or [])[-10:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": msg})
        temp = float(blk.get("temperature", 0.7) or 0.7)
        maxtok = int(blk.get("max_tokens", 500) or 500)

        tools = _build_chat_tools(aid)
        for hint in (_search_hint_system(), _claude_code_hint_system()):
            if hint:
                messages.insert(-1 if messages and messages[-1].get("role") == "user" else len(messages),
                                {"role": "system", "content": hint})

        r = client.chat.completions.create(
            model=model, messages=messages, temperature=temp,
            max_tokens=maxtok, timeout=90,
            **({"tools": tools} if tools else {}))
        m0 = r.choices[0].message
        used_vision = None
        search_panel = None
        search_meta = None
        tool_calls, visible = dsml_gw.extract_tool_calls(m0)
        coding_site_panel = None
        if tool_calls:
            messages.append({"role": "assistant", "content": visible or (m0.content or ""),
                             "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                try:
                    out, meta = _execute_chat_tool(fn.get("name") or "", args, aid, request=None)
                except Exception as ce:
                    out, meta = ("（工具失败：%s）" % ce), None
                if isinstance(meta, dict):
                    if meta.get("vision"):
                        used_vision = meta["vision"]
                    if meta.get("panel"):
                        search_panel = meta["panel"]
                        if meta.get("query") is not None or meta.get("sources") is not None:
                            search_meta = {
                                "query": meta.get("query"),
                                "sources": meta.get("sources") or [],
                                "elapsed_ms": meta.get("elapsed_ms"),
                                "items": meta.get("items") or [],
                                "pages": [
                                    {
                                        "url": p.get("url"),
                                        "title": p.get("title"),
                                        "summary": p.get("summary"),
                                        "images": p.get("images") or [],
                                        "ok": p.get("ok"),
                                    }
                                    for p in (meta.get("pages") or [])
                                ],
                            }
                    if meta.get("site_panel"):
                        coding_site_panel = meta["site_panel"]
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "content": out,
                })
            r = client.chat.completions.create(
                model=model, messages=messages, temperature=temp,
                max_tokens=maxtok, timeout=90)
            m0 = r.choices[0].message
        reply_text = dsml_gw.strip_dsml((m0.content or "")).strip()
        dossier = db.get_agent_dossier(aid) or dossier_lib.empty_dossier()
        if (
            dossier_lib.should_update_dossier_with_state(msg, reply_text, dossier)
            or _MEMORY_CANDIDATE_RE.search(msg)
        ):
            threading.Thread(
                target=_consider_agent_memory,
                args=(aid, blk, msg, reply_text),
                daemon=True,
            ).start()
        resp = {"ok": True, "reply": reply_text or (m0.content or ""), "model": model}
        if used_vision is not None:
            resp["vision"] = used_vision
        if search_panel is not None:
            resp["panel"] = search_panel
        if coding_site_panel is not None:
            resp["site_panel"] = coding_site_panel
        if search_meta is not None:
            resp["search"] = search_meta
        return resp
    except Exception as e:
        return JSONResponse({"ok": False, "error": "LLM 调用失败: %s" % e}, status_code=500)


@app.get("/api/agents/{aid}/conversation")
def api_agent_conversation(aid: int, after_id: int = 0, limit: int = 40):
    if not db.get_agent(aid):
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    messages = db.get_conversation_messages(aid, after_id=after_id, limit=limit)
    return {
        "ok": True,
        "messages": messages,
        "last_id": messages[-1]["id"] if messages else int(after_id or 0),
    }


@app.get("/api/agents/{aid}/live")
def api_agent_live(aid: int, after: int = 0):
    """本机语音旁路：实时对话增量 + 声波电平（供终端预览）。"""
    if not db.get_agent(aid):
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    snapshot = live_hub.snapshot(aid, after_seq=after)
    # 信息推送区只带 rev/expanded 这两个小字段随高频轮询走；内容较大，
    # 由状态栏页面在 rev 变化时单独取一次，避免每 220ms 拉整包。
    from control_plane import info_panel
    panel = info_panel.snapshot()
    snapshot["info_panel_rev"] = panel["rev"]
    snapshot["info_panel_expanded"] = panel["expanded"]
    return snapshot


@app.get("/api/agents/{aid}/info_panel")
def api_agent_info_panel(aid: int):
    """研究画布快照：标签、当前文档、节点、布局树和视图状态。"""
    if not db.get_agent(aid):
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    from control_plane import info_panel
    return info_panel.snapshot()


@app.post("/api/agents/{aid}/info_panel/canvas")
def api_agent_info_canvas(aid: int, payload: dict = Body(...)):
    """Direct gestures and AI use the exact same canvas transaction path."""
    if not db.get_agent(aid):
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    _text, result = canvas_control.execute(payload or {})
    status = 409 if result.get("error") == "revision_conflict" else 200
    return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})


@app.post("/api/agents/{aid}/info_panel/measure")
def api_agent_info_measure(aid: int, payload: dict = Body(...)):
    """Let the local status page shrink its native window to real content."""
    if not db.get_agent(aid):
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    try:
        requested_height = int((payload or {}).get("height") or 0)
    except (TypeError, ValueError):
        requested_height = 0
    changed = surface_tools.set_status_timeline_measured_height(requested_height)
    surface = scene_store.get(surface_tools.STATUS_TIMELINE_SURFACE) or {}
    data = surface.get("data") if isinstance(surface.get("data"), dict) else {}
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    return {
        "ok": True,
        "changed": bool(changed),
        "height": int(window.get("height") or 0),
    }


@app.post("/api/agents/{aid}/live")
def api_agent_live_push(aid: int, payload: dict = Body(...)):
    """本机语音进程推送旁路事件（跨进程，不能走内存 import）。"""
    if not db.get_agent(aid):
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    kind = (payload or {}).get("type") or "stage"
    if kind == "utterance":
        live_hub.push_utterance(
            aid,
            (payload or {}).get("role") or "",
            (payload or {}).get("text") or "",
            turn_id=str((payload or {}).get("turn_id") or ""),
            final=bool((payload or {}).get("final", True)),
        )
    elif kind == "status":
        live_hub.push_status(
            aid,
            (payload or {}).get("status") or "",
            (payload or {}).get("detail") or "",
            turn_id=str((payload or {}).get("turn_id") or ""),
        )
    elif kind == "heartbeat":
        live_hub.heartbeat(
            aid,
            pid=int((payload or {}).get("pid") or 0),
            listening=(payload or {}).get("listening"),
            standby=(payload or {}).get("standby"),
        )
    else:
        live_hub.set_stage(
            aid,
            speaking=(payload or {}).get("speaking"),
            level=(payload or {}).get("level"),
            listening=(payload or {}).get("listening"),
            turn_id=(payload or {}).get("turn_id"),
            standby=(payload or {}).get("standby"),
        )
    return {"ok": True, **live_hub.snapshot(aid, after_seq=0)}


@app.post("/api/agents/{aid}/conversation")
def api_append_agent_conversation(aid: int, payload: dict = Body(...)):
    if not db.get_agent(aid):
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    message_id = db.append_conversation_message(
        aid,
        payload.get("role"),
        payload.get("content"),
        payload.get("source") or "",
    )
    if message_id is None:
        return JSONResponse({"ok": False, "error": "无效消息"}, status_code=400)
    return {"ok": True, "id": message_id}


@app.post("/api/agents/{aid}/conversation/clear")
def api_clear_agent_conversation(aid: int):
    """清空该智能体的全部会话消息（历史记忆归零，窗口内容不受影响）。"""
    if not db.get_agent(aid):
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    db.clear_conversation_messages(aid)
    live_hub.clear_panels(aid)
    live_hub.reset(aid)
    return {"ok": True}


@app.post("/api/agents/{aid}/chat/stream")
def api_agent_chat_stream(aid: int, payload: dict = Body(...), request: Request = None):
    """摄像头低延迟试聊：按 NDJSON 增量返回 LLM 文本。"""
    agent = db.get_agent(aid)
    if not agent:
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    msg = (payload.get("message") or "").strip()
    if not msg:
        return JSONResponse({"ok": False, "error": "空消息"}, status_code=400)
    lm = (agent.get("modules") or {}).get("LLM") or {}
    name = lm.get("selected")
    if not name:
        return JSONResponse({"ok": False, "error": "该智能体未选择 LLM"}, status_code=400)
    blk = dict(db.provider_catalog().get("LLM", {}).get(name, {}) or {})
    blk.update(lm.get("overrides") or {})
    if blk.get("type") != "openai":
        return JSONResponse(
            {"ok": False, "error": "流式试聊仅支持 openai 兼容 LLM"},
            status_code=400,
        )
    key, url, model = blk.get("api_key"), blk.get("url"), blk.get("model_name")
    if not key or "你的" in str(key) or "请替换" in str(key):
        return JSONResponse({"ok": False, "error": "该智能体的 LLM 未填写 api_key"}, status_code=400)

    voice_mode = bool(payload.get("voice_mode"))
    trace_id = str(payload.get("turn_id") or uuid.uuid4().hex)
    if voice_mode:
        fingerprint = hashlib.sha256(
            ("%s\0%s\0%s" % (
                aid, msg, str(payload.get("speaker_name") or payload.get("speaker_status") or ""),
            )).encode("utf-8")
        ).hexdigest()
        now = time.monotonic()
        duplicate = False
        with _VOICE_TURN_DEDUP_LOCK:
            for old_key, old_at in list(_VOICE_TURN_DEDUP.items()):
                if now - old_at > 3.0:
                    _VOICE_TURN_DEDUP.pop(old_key, None)
            duplicate = now - float(_VOICE_TURN_DEDUP.get(fingerprint) or 0) < 1.5
            if not duplicate:
                _VOICE_TURN_DEDUP[fingerprint] = now
        if duplicate:
            def duplicate_stream():
                yield json.dumps({
                    "metrics": {"tool_name": "none", "finish_reason": "duplicate_utterance"},
                    "ignored": True,
                }, ensure_ascii=False) + "\n"
                yield json.dumps({"done": True}, ensure_ascii=False) + "\n"
            return StreamingResponse(
                duplicate_stream(), media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        coding_turn_trace.record(trace_id, "user", {"agent_id": aid, "text": msg})
    # Voice intent is model/tool-schema driven. There is no lexical router in
    # front of the model: ordinary chat and action requests enter the same turn.

    messages = []
    if agent.get("prompt"):
        agent_prompt = agent["prompt"]
        if voice_mode and len(agent_prompt) > 2200:
            agent_prompt = agent_prompt[:1800] + "\n…" + agent_prompt[-400:]
        messages.append({"role": "system", "content": agent_prompt})
    _inject_agent_context_messages(
        messages,
        aid,
        voice_mode=voice_mode,
    )
    # 语音也吃满客户端带来的最近轮次（客户端约 16 条）；过短会像失忆
    history_limit = 16 if voice_mode else 10
    for history_item in (payload.get("history") or [])[-history_limit:]:
        if history_item.get("role") in ("user", "assistant") and history_item.get("content"):
            messages.append({
                "role": history_item["role"],
                "content": history_item["content"],
            })
    if voice_mode and (payload.get("history") or []):
        # 历史分界：上面这些 user/assistant 都是「已经发生过的对话」。模型容易把
        # 历史里的旧动作（如「打开YouTube→打开了」）当成当前状态，导致新指令
        # 零工具调用就口头声称完成（幻觉）。这里明确隔离历史与当前指令：
        # 历史只用于记忆上下文，用户接下来这句是最新指令，动作类指令必须当场
        # 重新调用工具拿回执，即使以前做过一模一样的事。
        messages.append({
            "role": "system",
            "content": (
                "以上是已经发生的过去对话，只是背景记忆，不是当前状态。"
                "用户接下来那句才是现在要执行的指令。"
                "历史里只留下了说出去的话，当时的工具调用没有记录在内——那些"
                "「已开好/已关掉/已调成」的回复，当时都是真调了工具、拿到 ok:true 才说的。"
                "你这一轮做同样的事同样得先调工具；照着历史的说话样子直接宣布完成，就是撒谎。"
            ),
        })
    if voice_mode:
        addressed_hint = str(
            payload.get("addressed_hint") or "conversation_window"
        )
        messages.append({
            "role": "system",
            "content": (
                _VOICE_PERSONA_SYSTEM
                + "默认短：能一句说完就一句，别铺垫、别总结腔、别「首先其次」；"
                "话要说完整、自然收尾，别为省字数说一半就停。"
                "语音里不要列 1、2、3 清单，不要输出只有文字才合适的编号/分段；"
                "多要点就挑最要紧的一句说，剩下的被问再补。"
                "口语清楚自然，不拿腔作势；不确定就直接说明哪里不确定。"
                "先懂意图再答，别复述用户的话。复杂事先给结论，细节被问再补。"
                "不要每次反问结尾；不要「有什么我可以帮你的吗」这类生分套话。"
                "不要输出舞台说明（笑着说/叹气）。"
                "用户要求开窗/改窗时先做，做完按回执简短说一句就行；"
                "话随情景自然来，不用每次都一样，也别念窗口参数。"
                "有检索/工具结果时：第一句就给结论或要点，像已经知道了一样自然说；"
                "别报来源出处、更新时间、搜索方法，除非用户问；"
                "禁止过程旁白：不要「稍等/我搜一下/我查一下/还在查/马上好/我查到了/搜了一下/看了下资料」。"
                "禁止空头支票：不要说「等会发给你」「搜完再告诉你」「我记下了」却不给内容。"
                "记住不等于秀记忆：别主动点名旧闲聊，也别说「最近你在聊XX」。"
                "用户提出新指令时以新指令为准，不要接续旧任务（写码/改文件/修游戏）的进度，"
                "除非用户主动提起那件事。"
                "禁止复述或暗示系统里的档案标题/标签（如用户画像、相处状态、技术咨询需求）。"
                "关于对方的信息要自然用，别像汇报「我知道你…」。"
                "认人靠对话：陌生人自报后记住称呼；已认识就自然用，不要秀「我记住你的声音了」。"
                "告别只需简短确认，勿用亲昵套话，勿罗列旧话题。"
                "同一点只说一遍，禁止原句或近义句重复，说过的话不换说法再说。"
                "工具/渲染报错时一句带过原因并给下一步（如「没开成，我再试一次」），"
                "不要解释过程、不要罗列补救方案、不要承诺多个动作；"
                "要说重试就当场在同一轮调用工具，做不到就不说。"
                "工具多次尝试（失败重试后成功）只报最终结果：成功就说「好了/开了」，"
                "失败就说一句原因；不要逐次描述「先失败…再失败…最后成功」的过程。"
            ),
        })
        messages.append({
            "role": "system",
            "content": surface_skill.truth_system(),
        })
        messages.append({
            "role": "system",
            "content": (
                "指代消歧即可：遇到「这个/那个/刚才」默默接上，别刻意点名旧话题。"
                "评价、感叹、追问都算同一话题接着聊。"
            ),
        })
        # 世界现状不在这里注入：它是动态块，放在静态段中间会把后面几千字符的
        # 静态内容一起挤出前缀缓存。改为贴着用户那句话注入（见下方 stream 分支），
        # 顺带让状态离问题更近。
        # 实体设备的命令签名：只写在对象描述符里还不够——模型不 inspect 就看不到，
        # inspect 本身又是一整个 LLM 来回。实测「把灯调成黄色」因此要三次调用
        # （set_color 不存在 → color 传 color:yellow 参数错 → 才对），4.4 秒里
        # 3.3 秒在猜。设备少且稳定，签名直接进提示。
        # 参数形状不再只覆盖设备：计时器、便签、以后的 MCP 能力共用同一份投影
        device_hint = world_snapshot.capability_hint()
        if device_hint:
            messages.append({"role": "system", "content": device_hint})
        # 技能路由卡：动作指令的决策表，命中「必须调用」则不许只说话。
        messages.append({"role": "system", "content": _skill_routing_card()})
        # 记录模式：用户之前开启「把接下来要说的话记到窗口」。是否记录本条
        # 由模型自己判定（内容→append；操作/停止/提问→正常处理），不做硬编码。
        # 记录模式也是动态块：和世界现状一起贴着用户那句注入（见 stream 分支），
        # 夹在静态段中间会把后面的静态内容一起挤出前缀缓存。
        record_hint = surface_skill.record_mode_hint(aid)
        # 是否在对 EV 说话属于动作提议协议的一部分。旧逻辑让最终回答模型
        # 同时判断注意力和选择工具，容易让背景残句进入活跃工程 FSM。
        requires_address_decision = False
    else:
        addressed_hint = "text"
        requires_address_decision = False
    search_hint = _search_hint_system(voice_mode=voice_mode)
    if search_hint:
        messages.append({"role": "system", "content": search_hint})
    engineering_turn = False
    cc_hint = _claude_code_hint_system()
    if cc_hint and (not voice_mode or engineering_turn):
        messages.append({"role": "system", "content": cc_hint})
    if not voice_mode or engineering_turn:
        try:
            messages.append({"role": "system", "content": coding_fsm.phase_system_prompt(aid)})
        except Exception:
            pass
    messages.append({"role": "user", "content": msg})
    chat_tools = _build_chat_tools(aid, voice_mode=voice_mode)
    temperature = float(blk.get("temperature", 0.7) or 0.7)
    max_tokens = int(blk.get("max_tokens", 500) or 500)
    if voice_mode:
        max_tokens = min(max_tokens, 180)
    client = _openai_client(url, key)
    request_overrides = _llm_request_overrides(voice_mode, url, model)
    # 备用 LLM 列表：设置界面配置（lm.backups）或环境变量回退。黑名单过滤在
    # _voice_llm_backups 内部实时做，这里只需把候选名带进 generate。
    voice_backups = _voice_llm_backups(name, lm.get("backups")) if voice_mode else []
    create_timeout_s = _llm_create_timeout_s(voice_mode)

    def generate():
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        nonlocal model, url, key, request_overrides, voice_backups
        llm_started_at = time.perf_counter()
        stream_ready_at = None
        first_chunk_at = None
        first_text_at = None
        finish_reason = ""
        assistant_parts = []
        address_buffer = ""
        address_decided = not requires_address_decision
        ignored_as_background = False
        retry_reasons = []
        executed_tool_name = ""
        response_mode = ""
        direct_tool_ms = 0.0
        last_search_tab_id = ""
        last_search_result = None
        turn_had_mutation_receipt = False
        committed_mutation_keys = set()
        slow_starter_said = False
        active_provider = name
        failover_used = ""
        ttft_budget_s = _llm_ttft_budget_s(voice_mode)
        try:
            active_client = client
            retry_count = 0
            stream_messages = list(messages)
            selected_chat_tools = chat_tools
            # 模型一次流式调用直接带四个结构化出口。语音使用 tool_choice=required：
            # 普通回答走 conversation_reply(answer)，不确定走 clarify，明确动作走
            # 真实工具。它不是第二个分类模型，也不靠关键词/正则判断意图；同一次
            # 生成同时完成路由和参数生成，但自由文本不能再绕过执行回执。
            recent_context = []
            # 护栏体检表：这一轮每层补偿层到底在不在场、有没有触发。
            # 之前只有 print，进程一重启就没了，问「哪层管用」只能靠猜。
            # 落到 trace 里，日常用几天就能按层统计命中率和后果。
            guard_facts = {}
            if voice_mode:
                # 语音上下文保留最近 8 轮（user/assistant 成对计），每条截 800 字。
                # 之前只留 4 轮导致「刚才已确认要查什么/刚放过窗口」转头就忘，
                # 用户得反复重复要求。8 轮在 token 成本与记忆之间取平衡。
                raw_history = [
                    {
                        "role": item.get("role"),
                        "content": str(item.get("content"))[:800],
                    }
                    for item in (payload.get("history") or [])[-8:]
                    if item.get("role") in ("user", "assistant")
                    and item.get("content")
                ]
                # 历史不改写内容，只补回被剥掉的事实：哪几句回复当时真的调过工具。
                # 否则上下文教给模型的规律是「说一句完成即可」。
                recent_context = _replay_recorded_turns(aid, raw_history)
                print(
                    "[muse] voice_req turn=%s msg=%r history轮数=%d"
                    % (trace_id[:8], msg[:40], len(payload.get("history") or [])),
                    flush=True,
                )
                # 普通对话/窗口编辑不拖整段旧工程 transcript：只留系统身份、
                # 最近直接上下文与当前句。写码轮由 coding_flow 回执补上下文。
                stream_messages = [
                    item for item in messages
                    if item.get("role") == "system"
                ]
                stream_messages.extend(recent_context)
                # 世界现状：对象契约的紧凑投影，取代此前手写的「窗口记忆」+
                # 「设备状态」两套文案（3221 → 约 300 字符），并第一次带上
                # 计时器/面板这些原本完全没有状态供给的对象。
                try:
                    world = world_snapshot.render(
                        search_hint=surface_skill.search_results_hint(),
                    )
                except Exception:
                    world = ""
                if record_hint:
                    stream_messages.append({"role": "system", "content": record_hint})
                if world:
                    stream_messages.append({"role": "system", "content": world})
                guard_facts.update({
                    "history_turns": len(payload.get("history") or []),
                    "replayed_notes": sum(
                        1 for m in recent_context if m.get("role") == "system"
                    ),
                    "world_snapshot": len(world or ""),
                    "routing_card": len(_skill_routing_card() or ""),
                })
                voice_user_index = len(stream_messages)
                stream_messages.append({"role": "user", "content": msg})
            selected_chat_tools = chat_tools
            # 动作流批1：慢工具快路径（deferred_realtime_intent 死块）已删。
            # 搜索/视觉等统一走下方动作流循环，由模型自己发起工具调用。
            # 动作流批1：非流式工具轮已删，动作执行并入下方流式循环。

            # 动作流：单次流式 function-calling，边说边收动作。动作完整即按注册表
            # 执行（冲突域并行），回执回填后继续下一轮，直到模型只输出最终答复。
            action_round = 0
            prompt_cache_hit = 0
            prompt_cache_miss = 0
            # 不设硬轮数上限：复杂任务（搜索→抽文→整理→写窗口）天然要 5-6 轮，
            # 硬上限会在任务刚要完成时截断。改为：
            #  1. 自然终止：模型本轮不调工具（纯聊天或最终答复）即结束；
            #  2. 空转检测：连续多轮只调只读工具、无任何变更回执，判定在空转，
            #     注入收尾提示逼它给结论；
            #  3. 软保险：一个非常高的兜底上限，正常任务永远碰不到。
            spin_rounds = 0
            spin_corrected = False
            answer_retry_used = 0
            force_tool_next_round = False
            turn_had_tool_call = False
            executed_readonly_keys = set()
            voice_user_index = 0
            last_readonly_success = None
            SPIN_LIMIT = 3
            ACTION_HARD_LIMIT = 12
            # 最终答复只取最后一轮的文本。中间工具轮模型也会输出叙述
            # （"我把XX整理进窗口"），这些已实时播报，若再拼进最终答复，
            # 会把多轮相似叙述叠成一大段重复的话。
            last_round_parts = []
            while stream_messages is not None and action_round < ACTION_HARD_LIMIT:
                last_round_parts = []
                held_voice_round_parts = []
                response = None
                try:
                    # 语音主链只发一次；超时后才在异常分支串行切备用模型。
                    # 同一设备动作永远不会由多个模型并行竞争决定。
                    if voice_mode:
                        tool_request_kwargs = _tool_request_kwargs(selected_chat_tools)
                        if force_tool_next_round and tool_request_kwargs.get("tools"):
                            tool_request_kwargs["tool_choice"] = "required"
                            force_tool_next_round = False
                        response = _llm_create_with_budget(
                            active_client,
                            create_timeout_s,
                            model=model,
                            messages=stream_messages,
                            temperature=min(temperature, 0.2),
                            max_tokens=max(max_tokens, 2400),
                            timeout=_llm_stream_read_timeout_s(),
                            stream=True,
                            **tool_request_kwargs,
                            **request_overrides,
                        )
                    else:
                        tool_request_kwargs = _tool_request_kwargs(selected_chat_tools)
                        response = active_client.chat.completions.create(
                            model=model,
                            messages=stream_messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            timeout=_llm_stream_read_timeout_s(),
                            stream=True,
                            **tool_request_kwargs,
                            **request_overrides,
                        )
                    stream_ready_at = time.perf_counter()
                    chunk_iter = iter(response)
                    ttft_deadline = (
                        (stream_ready_at + ttft_budget_s) if ttft_budget_s > 0 else None
                    )
                    tool_calls_by_index = {}
                    # 首包用短超时拉取，避免卡在上游排队；出首包后恢复阻塞迭代
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        while True:
                            if first_chunk_at is None and ttft_deadline is not None:
                                remain = ttft_deadline - time.perf_counter()
                                if remain <= 0:
                                    raise TimeoutError(
                                        "llm_ttft_timeout after %.0fms"
                                        % (ttft_budget_s * 1000)
                                    )
                                fut = pool.submit(next, chunk_iter, None)
                                try:
                                    chunk = fut.result(timeout=remain)
                                except FuturesTimeout:
                                    raise TimeoutError(
                                        "llm_ttft_timeout after %.0fms"
                                        % (ttft_budget_s * 1000)
                                    )
                            else:
                                chunk = next(chunk_iter, None)
                            if chunk is None:
                                break
                            if first_chunk_at is None:
                                first_chunk_at = time.perf_counter()
                            # 最后一帧带 usage：前缀缓存命中多少 token 只有这里看得到。
                            usage = getattr(chunk, "usage", None)
                            if usage is not None:
                                hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
                                miss = int(getattr(usage, "prompt_cache_miss_tokens", 0) or 0)
                                if hit or miss:
                                    prompt_cache_hit = hit
                                    prompt_cache_miss = miss
                                    print(
                                        "[muse] prefix_cache turn=%s 命中=%d 未命中=%d (%.0f%%)"
                                        % (trace_id[:8], hit, miss,
                                           hit * 100.0 / max(hit + miss, 1)),
                                        flush=True,
                                    )
                            choice = chunk.choices[0] if chunk.choices else None
                            if choice and choice.finish_reason:
                                finish_reason = choice.finish_reason
                            # 先累积工具调用增量再处理文本：文本分支的 continue
                            # 不应把同一 chunk 里的动作增量丢进黑洞。
                            for tc in (choice.delta.tool_calls if choice and choice.delta else None) or []:
                                idx = int(tc.index or 0)
                                entry = tool_calls_by_index.setdefault(idx, {
                                    "id": "", "name": "", "arguments": "",
                                })
                                if tc.id:
                                    entry["id"] = tc.id
                                if tc.function:
                                    if tc.function.name:
                                        entry["name"] += tc.function.name
                                    if tc.function.arguments:
                                        entry["arguments"] += tc.function.arguments
                            delta = choice.delta.content if choice else None
                            if delta:
                                # 首轮文本只暂存、不外发。语音协议要求结构化出口；若
                                # 上游仍违规返回自由文本，它也不能绕过回执门禁。
                                if voice_mode:
                                    held_voice_round_parts.append(delta)
                                    continue
                                if not address_decided:
                                    address_buffer += delta
                                    candidate = address_buffer.lstrip()
                                    marker = "<NO_REPLY>"
                                    if candidate.startswith(marker):
                                        address_decided = True
                                        ignored_as_background = True
                                        continue
                                    if marker.startswith(candidate):
                                        continue
                                    address_decided = True
                                    delta = address_buffer
                                    address_buffer = ""
                                if not ignored_as_background:
                                    if first_text_at is None:
                                        first_text_at = time.perf_counter()
                                    assistant_parts.append(delta)
                                    last_round_parts.append(delta)
                                    yield json.dumps(
                                        {"delta": delta},
                                        ensure_ascii=False,
                                    ) + "\n"
                    if not tool_calls_by_index:
                        # 工具已撤掉后的自然回答轮。若上游仍把历史工具协议写进
                        # content，先静默重试一次；内部协议错误永远不能拿去播报。
                        if voice_mode:
                            direct_text, response_mode = _voice_no_tool_response(
                                held_voice_round_parts
                            )
                            constrained = (
                                _constrain_search_answer(direct_text, last_search_result)
                                if (direct_text and last_search_result) else direct_text
                            )
                            reason, instruction = (
                                _voice_answer_retry(
                                    text=direct_text,
                                    had_tool_call=turn_had_tool_call,
                                    had_mutation_receipt=turn_had_mutation_receipt,
                                    search_result=last_search_result,
                                    constrained_empty=bool(
                                        direct_text and last_search_result and not constrained
                                    ),
                                )
                                if answer_retry_used < 1 else ("", "")
                            )
                            if reason:
                                answer_retry_used += 1
                                # 说了没做：软提醒不够，实测模型重说时又说同样的话。
                                # 这一轮强制它必须产出一个结构化出口——要么真去调
                                # 工具，要么明确选 conversation_reply（那就不能声称
                                # 做完了）。这是把 required 用在真正需要的那一轮，
                                # 而不是全程强制。
                                force_tool_next_round = reason == "unbacked_claim"
                                guard_facts["retry_reason"] = reason
                                guard_facts["forced_required"] = force_tool_next_round
                                coding_turn_trace.record(trace_id, "answer_retry", {
                                    "reason": reason,
                                    "forced_required": force_tool_next_round,
                                    "action_round": action_round,
                                    "had_tool_call": bool(turn_had_tool_call),
                                    "text": direct_text[:120],
                                }, severity="warning")
                                print(
                                    "[muse] answer_retry turn=%s 原因=%s: %s"
                                    % (trace_id[:8], reason, direct_text[:36]),
                                    flush=True,
                                )
                                stream_messages.append(
                                    {"role": "system", "content": instruction}
                                )
                                _close_llm_stream(response)
                                action_round += 1
                                held_voice_round_parts = []
                                last_round_parts = []
                                continue
                            if not direct_text:
                                # 回炉之后仍然一个字没有：只能承认是我这边的问题，
                                # 不装成「查过了但没结论」。
                                direct_text = "我这边卡住了，这句没答上来，你再说一遍试试。"
                            elif last_search_result:
                                direct_text = constrained or direct_text
                            if last_search_tab_id:
                                _commit_search_answer(
                                    last_search_result or {},
                                    last_search_tab_id,
                                    direct_text,
                                    entries=_panel_entries_from_answer(
                                        direct_text, last_search_result,
                                    ),
                                )
                                last_search_result = None
                            # tool_choice=auto 之后，「只说话不调工具」是闲聊的
                            # 正常出口，不是协议错误；只有抽不出可播文本才算。
                            executed_tool_name = executed_tool_name or (
                                "direct_answer" if direct_text else "protocol_error"
                            )
                            if first_text_at is None:
                                first_text_at = time.perf_counter()
                            assistant_parts.append(direct_text)
                            last_round_parts.append(direct_text)
                            for direct_chunk in _split_direct_reply_chunks(direct_text):
                                yield json.dumps(
                                    {"delta": direct_chunk},
                                    ensure_ascii=False,
                                ) + "\n"
                        # 后续回执轮零工具调用代表自然结束。
                        break
                    if action_round >= ACTION_HARD_LIMIT - 1:
                        # 软保险：极罕见情况下（正常任务不会到）模型持续调工具
                        # 又不结束，明确报告未收敛；不能再用「先说到这儿」伪装成
                        # 正常回答，让用户误以为只是搜索时间长。
                        executed_tool_name = "protocol_error"
                        delta = "我这次没查出足够可靠的结论。"
                        assistant_parts.append(delta)
                        yield json.dumps({"delta": delta}, ensure_ascii=False) + "\n"
                        break
                    # 动作入队：assistant 消息带 tool_calls 回填上下文
                    turn_had_tool_call = True
                    ordered_calls = [
                        tool_calls_by_index[i] for i in sorted(tool_calls_by_index)
                    ]
                    ctx = {
                        "aid": aid,
                        "request": request,
                        "trace_id": trace_id,
                        "user_message": msg,
                    }
                    stream_messages.append({
                        "role": "assistant",
                        # 只回填当前模型轮说过的话；assistant_parts 是整轮累计，
                        # 用它会把前几步话术反复塞回上下文并诱发重复。
                        "content": "".join(last_round_parts) or None,
                        "tool_calls": [
                            {
                                "id": tc["id"] or ("call-%s-%d" % (action_round, i)),
                                "type": "function",
                                "function": {
                                "name": tc["name"] or "invalid_tool_call",
                                    "arguments": tc["arguments"] or "{}",
                                },
                            }
                            for i, tc in enumerate(ordered_calls)
                        ],
                    })
                    direct_reply = None
                    batch = []
                    round_tool_names = []
                    round_readonly_flags = []
                    for i, tc in enumerate(ordered_calls):
                        tool_name = tc["name"] or ""
                        if tool_name and not executed_tool_name:
                            executed_tool_name = tool_name
                        raw = tc["arguments"] or "{}"
                        args = _parse_tool_arguments(raw)
                        if args is None:
                            print(
                                "[muse] invalid tool arguments name=%s raw=%s"
                                % (tool_name, raw[:2000]),
                                flush=True,
                            )
                            # 把原始损坏内容回显给模型：让它看到自己的参数
                            # 无法解析，下一轮重新生成合法 JSON。
                            args = {
                                "__parse_error__": (
                                    "工具参数 JSON 无法解析（可能含未转义的引号/换行或多余内容）。"
                                    "请重新调用本工具，参数必须是单个合法 JSON 对象，"
                                    "字符串里的引号要转义为 \\\"、换行写成 \\n。原始内容：%s"
                                    % raw[:800]
                                )
                            }
                        batch.append({
                            "action": tool_name or "invalid_tool_call",
                            "id": tc["id"] or ("call-%s-%d" % (action_round, i)),
                            "args": args,
                        })
                        if tool_name:
                            if tool_name == "conversation_reply":
                                candidate_mode = str(args.get("mode") or "")
                                if (
                                    response_mode != "act"
                                    and candidate_mode in {"answer", "clarify"}
                                ):
                                    response_mode = candidate_mode
                            else:
                                response_mode = "act"
                            round_tool_names.append(tool_name)
                            round_readonly_flags.append(
                                _is_readonly_call(tool_name, args)
                            )
                            coding_turn_trace.record(trace_id, "tool_call", {
                                "name": tool_name, "arguments": args,
                            })
                    round_requests_continuation = _batch_requests_continuation(batch)
                    duplicate_key = next(
                        (
                            _transaction_action_key(item)
                            for item in batch
                            if _transaction_action_key(item) in committed_mutation_keys
                        ),
                        "",
                    )
                    # 只读调用重复了就别再打一遍：结果已经在上面。
                    # 真实事故：ASR 只听到半句「呼吸的」，模型连着调了 6 次
                    # 一模一样的 inspect(query=哔哩哔哩)，7 秒后放弃。原先
                    # 「inspect 关闭动作阶段」这道刹车被我摘掉了（为了让
                    # inspect→show 两步走通），而变更去重只管成功的变更动作。
                    repeated_readonly = bool(batch) and all(
                        _is_readonly_call(
                            item.get("action") or "",
                            item.get("args") if isinstance(item.get("args"), dict) else {},
                        )
                        and _transaction_action_key(item) in executed_readonly_keys
                        for item in batch
                    )
                    if voice_mode and repeated_readonly:
                        stream_messages.append({
                            "role": "system",
                            "content": (
                                "你刚才重复了同一次查询，结果上面已经有了，别再查。"
                                "根据已有结果直接回答用户；如果结果里没有他要的东西，"
                                "就说清楚没找到什么、缺什么。"
                            ),
                        })
                        selected_chat_tools = _answer_only_tools(selected_chat_tools)
                        _close_llm_stream(response)
                        action_round += 1
                        held_voice_round_parts = []
                        last_round_parts = []
                        continue
                    for item in batch:
                        if _is_readonly_call(
                            item.get("action") or "",
                            item.get("args") if isinstance(item.get("args"), dict) else {},
                        ):
                            executed_readonly_keys.add(_transaction_action_key(item))
                    if voice_mode and duplicate_key:
                        # 显式 continuation 也不能重复提交已经成功的同一事务。
                        # 这是结构化动作键去重，不读取或匹配用户原话。
                        duplicate_reply = "检测到重复操作，已经停止继续执行。"
                        for item in batch:
                            coding_turn_trace.record(trace_id, "tool_result", {
                                "name": item.get("action") or "",
                                "result": {
                                    "ok": False,
                                    "action": item.get("action") or "",
                                    "reason": "duplicate_committed_action",
                                },
                            })
                        if first_text_at is None:
                            first_text_at = time.perf_counter()
                        assistant_parts.append(duplicate_reply)
                        last_round_parts.append(duplicate_reply)
                        for chunk in _split_direct_reply_chunks(duplicate_reply):
                            yield json.dumps({"delta": chunk}, ensure_ascii=False) + "\n"
                        break
                    starter = ""
                    # 模型只负责判断任务是否值得垫一句、并临场写自然话术；程序
                    # 还会等一个很短的 grace period。工具在此期间完成就保持沉默。
                    if (
                        voice_mode
                        and not "".join(last_round_parts).strip()
                        and not slow_starter_said
                    ):
                        starter = _action_progress_starter(batch)
                    # 先把工作通道真正提交到后台，再把进度事件交给语音通道。
                    # 终端播放开始语期间 future 已在执行；无冲突动作由 run_batch
                    # 再并行，同设备/同页面/同工程状态机仍按序串行。
                    mutation_before_round = turn_had_mutation_receipt
                    _tool_batch_started_at = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=1) as action_pool:
                        action_future = action_pool.submit(
                            _action_registry.run_batch,
                            [
                                {"action": a["action"], "id": a["id"], "args": a["args"]}
                                for a in batch
                            ],
                            ctx,
                        )
                        batch_results = None
                        if starter:
                            try:
                                batch_results = action_future.result(
                                    timeout=_TOOL_PROGRESS_GRACE_S,
                                )
                            except FuturesTimeout:
                                slow_starter_said = True
                                yield json.dumps(
                                    {"kind": "tool_progress", "speak": starter},
                                    ensure_ascii=False,
                                ) + "\n"
                        if batch_results is None:
                            batch_results = action_future.result()
                    direct_tool_ms = round(
                        (time.perf_counter() - _tool_batch_started_at) * 1000,
                        1,
                    )
                    for act, res in zip(batch, batch_results):
                        action_name = act["action"]
                        meta = res.get("meta") if isinstance(res.get("meta"), dict) else {}
                        if (
                            action_name == "task_control"
                            and str(meta.get("task_kind") or "") == "web_search"
                        ):
                            canvas_receipt = meta.get("canvas") if isinstance(meta.get("canvas"), dict) else {}
                            if canvas_receipt.get("tab_id"):
                                last_search_tab_id = str(canvas_receipt["tab_id"])
                                last_search_result = dict(meta)
                        elif (
                            action_name == "conversation_reply"
                            and meta.get("ok")
                            and last_search_tab_id
                            and meta.get("direct_reply")
                        ):
                            # 搜索工具负责证据，回答模型负责把证据变成用户能读懂的结论。
                            # 两者在这里汇合：最终自然回答同步成为画布首屏答案；后台
                            # 仍可补图片/来源，但不能再用抓取垃圾覆盖它。
                            safe_search_reply = _constrain_search_answer(
                                meta.get("direct_reply"), last_search_result,
                            )
                            meta["direct_reply"] = safe_search_reply
                            _commit_search_answer(
                                last_search_result or {},
                                last_search_tab_id,
                                safe_search_reply,
                                entries=_panel_entries_from_answer(
                                    safe_search_reply, last_search_result,
                                ),
                            )
                            last_search_result = None
                        # 失败不再由代码代播固定话术：真实失败原因写进工具回执
                        # 上下文（下方 out），由下一轮模型自己解释，避免机器人感。
                        # 搜索结果已经进入版本化研究画布；旧 panel 会把 items/links/
                        # 抓取摘要整包再推一次，正是用户看到“满屏垃圾数据”的来源。
                        # 其他工具的专用 panel 继续兼容，搜索则只保留精简画布。
                        if _should_emit_legacy_panel(action_name, meta):
                            payload_out = {"panel": meta["panel"]}
                            if meta.get("query") is not None or meta.get("sources") is not None:
                                payload_out["search"] = {
                                    "query": meta.get("query"),
                                    "sources": meta.get("sources") or [],
                                    "items": meta.get("items") or [],
                                    "pages": [
                                        {
                                            "url": p.get("url"),
                                            "title": p.get("title"),
                                            "summary": p.get("summary"),
                                            "images": p.get("images") or [],
                                            "ok": p.get("ok"),
                                        }
                                        for p in (meta.get("pages") or [])
                                    ],
                                }
                            yield json.dumps(payload_out, ensure_ascii=False) + "\n"
                        # 确定性回执由 direct_reply 单一路径输出；不再同时发送
                        # tool_ack，避免终端依赖去重逻辑才能防止同一句播两遍。
                        receipt_reply = _verified_receipt_direct_reply(
                            action_name, res
                        )
                        if voice_mode and receipt_reply and not direct_reply:
                            direct_reply = receipt_reply
                        if meta.get("site_panel"):
                            yield json.dumps(
                                {"panel": meta["site_panel"]},
                                ensure_ascii=False,
                            ) + "\n"
                        out = res.get("result")
                        if not res.get("ok"):
                            out = out or "（工具失败：%s）" % _receipt_failure_text(
                                meta, res, "设备没有返回成功回执"
                            )
                        stream_messages.append({
                            "role": "tool",
                            "tool_call_id": act["id"],
                            "content": out,
                        })
                        if action_name:
                            coding_turn_trace.record(trace_id, "tool_result", {
                                "name": action_name,
                                "result": meta if isinstance(meta, dict) else {"text": str(out)[:2000]},
                            })
                        # 收集变更类工具的成功回执：光说不做拦截需要知道本轮是否有
                        # 真正的变更动作完成（surface_control/device_control 的变更动作、
                        # coding_flow、claude_code）。只读工具（inspect/status/search/
                        # 查询动作不算，调了它们模型仍可能空口声称"改好了"。
                        if res.get("ok") and _receipt_is_mutation(action_name, meta):
                            turn_had_mutation_receipt = True
                            action_key = _transaction_action_key(act)
                            if action_key:
                                committed_mutation_keys.add(action_key)
                    readonly_outcomes = [
                        bool(res.get("ok"))
                        for act, res in zip(batch, batch_results)
                        if act.get("action") != "conversation_reply"
                        and _is_readonly_call(act.get("action"), act.get("args"))
                    ]
                    if readonly_outcomes:
                        last_readonly_success = any(readonly_outcomes)
                    # 事务终态：continue_after=false 表示当前批次就是最终提交。
                    # 成功回执可直接收尾；真失败不代播固定话术，交给模型解释，
                    # 因此有失败时清空 direct_reply，让下一轮模型基于回执说明原因。
                    if round_requests_continuation:
                        direct_reply = None
                    elif voice_mode:
                        batch_reply = _batch_direct_reply(batch_results)
                        if batch_reply:
                            direct_reply = batch_reply
                        else:
                            # 回执没有确定播报文本（含真失败）时由模型接管，
                            # 不用单条成功回执的 direct_reply 抢快路径。
                            direct_reply = None
                        if direct_reply and first_text_at is None:
                            first_text_at = time.perf_counter()
                    # 查询类工具没有确定性 direct_reply，需要再让模型把回执整理成
                    # 自然答案。但 continue_after=false 已声明检索阶段结束：下一轮
                    # 从请求层移除所有工具，让下一轮成为真正的文本回答轮。
                    readonly_phase_closed = _batch_closes_readonly_phase(batch)
                    if voice_mode and readonly_phase_closed and not direct_reply:
                        selected_chat_tools = _answer_only_tools(selected_chat_tools)
                        stream_messages.append({
                            "role": "system",
                            "content": _forced_text_answer_instruction(),
                        })
                    # 空转检测：本轮全只读且无任何新增变更回执 → 连续累计；一旦
                    # 产生变更回执即清零。累计到 SPIN_LIMIT 就注入收尾提示，逼模型
                    # 给结论或真正动手，而不是无限 inspect/search 原地打转。
                    if turn_had_mutation_receipt != mutation_before_round:
                        spin_rounds = 0
                    elif round_readonly_flags and all(round_readonly_flags):
                        spin_rounds += 1
                    else:
                        spin_rounds = 0
                    if voice_mode and spin_rounds >= SPIN_LIMIT:
                        # 第一次只给提示；提示无效还在空转，就把工具彻底收走，
                        # 下一轮直接生成自然语言。
                        # 光提示不管用：实测模型会无视提示继续搜到硬上限 12 次，
                        # 用户侧表现为「说一半卡住」再蹦一句收尾模板。
                        spin_rounds = 0
                        if not spin_corrected:
                            spin_corrected = True
                            hint = (
                                "你已经连续几轮只做查询/搜索，没有任何实质改动"
                                "（没有变更类工具的成功回执）。如果已经拿到需要的信息，"
                                "直接给最终结论收尾，一句话说清结果，不要再继续查；"
                                "如果确实要动手改，就调用变更类工具真正去改，"
                                "然后给结论。不要再说『还在查/还没弄完』这类话。"
                            )
                        else:
                            selected_chat_tools = _answer_only_tools(selected_chat_tools)
                            print("[muse] 空转锁死：本轮起关闭全部工具", flush=True)
                            hint = (
                                "已经查了很多轮仍未收敛，检索工具本轮起不再可用。"
                                "就用现在手上的信息回答用户，说清已知的部分；"
                                "确实查不到就直说没查到，别再尝试检索。"
                            )
                        stream_messages.append({"role": "system", "content": hint})
                        _close_llm_stream(response)
                        action_round += 1
                        continue
                    # realtime_info 直答快路径：工具已生成完整、口语化的答复，
                    # 直接作为最终答复播报，不再发起第二轮 LLM（省一次 prefill）。
                    if voice_mode and direct_reply:
                        if first_text_at is None:
                            first_text_at = time.perf_counter()
                        assistant_parts.append(direct_reply)
                        last_round_parts.append(direct_reply)
                        # 整句一次性送达会让 TTS 等完整句子才出首 PCM（实测
                        # 10字首PCM 782ms vs 3字 198ms）。这里按自然边界拆成
                        # 小段增量，配合 turn.py 的 FIRST_SEGMENT_CHARS 阈值，
                        # TTS 提前开始合成第一段，首字出声更快。
                        chunks = _split_direct_reply_chunks(direct_reply)
                        for chunk in chunks:
                            yield json.dumps(
                                {"delta": chunk},
                                ensure_ascii=False,
                            ) + "\n"
                        break
                    # 回执铁律：下一轮模型仅依据 ok:true 回执描述结果；无回执只能说未完成。
                    if voice_mode:
                        stream_messages.append({
                            "role": "system",
                            "content": (
                                "以上是工具回执。回执是唯一真相：只有 ok:true 才代表真正执行过。"
                                "在决定不再调用工具、给出最终答复之前，你必须先对照上面的"
                                "工具回执逐一自检：你打算说出口的每一项『已完成』（如已关/已开/"
                                "已改/已补/已更新/已删除/已记录）是否都有对应的 ok:true 变更回执？"
                                "没有回执支撑的声称必须改成『还没做/还没执行/需要再操作一次』，"
                                "绝不能声称没做过的事。"
                                "动作指令场景下，没有调用任何工具就声称完成"
                                "（如说「已关/已开/已改」却没调过工具）是错误行为。"
                                "如果确实已完成：给最终答复，简短自然、一句话收尾，"
                                "不要重复查询或继续调用工具。"
                                "如果还要继续调用工具：本轮最多说一句极短的过渡语"
                                "（如「好的」「稍等」「马上」），别长篇复述进度或预告接下来做什么，"
                                "把具体说明留到最后给最终答复时一次说清。"
                            ),
                        })
                    action_round += 1
                    # 轮次边界标记：本轮 LLM 输出到此为止，下一轮会重新生成。
                    # 客户端（turn.py）收到后丢弃已累积的回复正文——中间工具轮
                    # 的叙述（"我把XX整理进窗口"）已实时播报，不该拼进最终答复。
                    yield json.dumps({"kind": "round_done"}, ensure_ascii=False) + "\n"
                    # 批4 打断语义：每轮 LLM 流用完即关。客户端插话/断开时生成器被
                    # GeneratorExit 关闭，这里保证上游连接及时释放，未执行的后续动作轮不再发起。
                    _close_llm_stream(response)
                except (Exception, GeneratorExit) as stream_error:
                    _close_llm_stream(response)
                    if isinstance(stream_error, GeneratorExit):
                        # 用户打断：保留已执行动作回执，中止后续生成与动作轮
                        raise
                    if "llm_ttft_timeout" in str(stream_error) and assistant_parts:
                        # 首包预算超时但已产出文本：保留已播内容收尾，不整轮失败。
                        # 重试会重播已说的部分，语音场景比完整回答更糟。
                        print(
                            "[muse] LLM 中途断流但已有文本，保留已播内容收尾: %s"
                            % stream_error,
                            flush=True,
                        )
                        break
                    if assistant_parts and first_text_at is not None:
                        # 已开始生成文本但流中途断了（如 DeepSeek 长回复 ReadTimeout）。
                        # 保留已播文本收尾，避免整轮失败导致用户听到错误提示。
                        print(
                            "[muse] LLM 流中途断开，保留已播文本收尾: %s" % stream_error,
                            flush=True,
                        )
                        break
                    if assistant_parts or retry_count >= 1:
                        raise
                    retry_count += 1
                    reason = (
                        "ttft_timeout"
                        if "llm_ttft_timeout" in str(stream_error)
                        else type(stream_error).__name__
                    )
                    retry_reasons.append(reason)
                    stream_ready_at = None
                    first_chunk_at = None
                    first_text_at = None
                    # 语音：优先切备用模型，不要在同一条卡住的 DeepSeek 上再等一轮。
                    # 但备用模型若已被黑名单（403 等鉴权失败），切过去只会再撞一次错误，
                    # 此时留在原 provider 上重试（排队可能已缓解）。
                    if voice_mode and voice_backups:
                        # 取第一个未黑名单的备用（_voice_llm_backups 构造时已过滤，
                        # 但黑名单可能中途新增，这里再兜底检查一次）。
                        failover_name = ""
                        failover_blk = {}
                        for _name, _blk in voice_backups:
                            if _name == active_provider:
                                continue
                            if not _failover_blacklisted(_name):
                                failover_name, failover_blk = _name, _blk
                                break
                        if not failover_name:
                            print(
                                "[muse] 所有备用已黑名单/不可用，留在 %s 重试: %s"
                                % (active_provider, stream_error),
                                flush=True,
                            )
                            _openai_client.cache_clear()
                            active_client = _openai_client(url, key)
                        else:
                            failover_used = failover_name
                            previous_provider = active_provider
                            active_provider = failover_name
                            model = failover_blk.get("model_name")
                            url = failover_blk.get("url")
                            key = failover_blk.get("api_key")
                            request_overrides = _llm_request_overrides(
                                True, url, model
                            )
                            _openai_client.cache_clear()
                            active_client = _openai_client(url, key)
                            # 当前主链已切成该备用，从候选里去掉它
                            voice_backups = [
                                (_n, _b) for _n, _b in voice_backups
                                if _n != failover_name
                            ]
                            print(
                                "[muse] LLM 首包失败，切换备用 %s ← %s: %s"
                                % (failover_name, previous_provider, stream_error),
                                flush=True,
                            )
                    else:
                        print(
                            "[muse] LLM 流式首包失败，重试一次: %s" % stream_error,
                            flush=True,
                        )
                        _openai_client.cache_clear()
                        active_client = _openai_client(url, key)
            completed_at = time.perf_counter()
            if ignored_as_background:
                print(
                    "[muse] 寻址判断：忽略非对话语音 hint=%s text=%r"
                    % (addressed_hint, msg),
                    flush=True,
                )
            if not ignored_as_background:
                # 最终答复优先用最后一轮的文本（中间轮叙述已实时播报，不重复拼进
                # 汇总）。个别路径（如用户打断后无最后一轮文本）回退到累计全部。
                final_text = "".join(last_round_parts).strip()
                assistant_text = final_text or "".join(assistant_parts).strip()
                if voice_mode:
                    coding_turn_trace.record(trace_id, "assistant", {
                        "text": assistant_text, "tool_name": executed_tool_name or "none",
                        # 护栏体检表：在场的层 + 触发的层 + 这一轮的结果。
                        # 「哪层管用」要靠这三样凑在一起才能回答。
                        "guards": dict(guard_facts, **{
                            "action_rounds": action_round,
                            "had_tool_call": bool(turn_had_tool_call),
                            "had_mutation_receipt": bool(turn_had_mutation_receipt),
                            "hard_limit_hit": executed_tool_name == "protocol_error",
                            "spin_corrected": bool(spin_corrected),
                        }),
                    })
                    # 本轮真实的调用与回执：从当前句之后截出来，压缩后存下，
                    # 下一轮原样贴回上下文（见 _replay_recorded_turns）。
                    try:
                        structured = []
                        # 只回放动作，不回放查询。真实事故：模型对着「把哔哩哔哩
                        # 关上」「计时35分钟」去搜「DJX 价格」，这些错误调用被当成
                        # 示范贴回历史，下一轮照抄——自我强化的污染环。动作调用
                        # 带着明确的 target 和回执，抄错了也立刻能看出来；检索调用
                        # 只是一串自由文本，最容易被跨话题复制。
                        def _is_replayable(call):
                            try:
                                fn = (call or {}).get("function") or {}
                                if str(fn.get("name") or "") != "task_control":
                                    return True
                                args = json.loads(fn.get("arguments") or "{}")
                                return str(args.get("kind") or "") not in (
                                    "web_search", "web_extract",
                                )
                            except Exception:
                                return False

                        for item in stream_messages[voice_user_index + 1:]:
                            role = item.get("role")
                            if role == "assistant" and item.get("tool_calls"):
                                if not all(_is_replayable(c) for c in item["tool_calls"]):
                                    structured = []
                                    break
                                structured.append({
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": copy.deepcopy(item["tool_calls"])[:2],
                                })
                            elif role == "tool":
                                structured.append({
                                    "role": "tool",
                                    "tool_call_id": item.get("tool_call_id") or "",
                                    "content": _compact_tool_content(item.get("content")),
                                })
                            if len(structured) >= 4:
                                break
                        if structured and assistant_text:
                            structured.append({"role": "assistant", "content": assistant_text})
                            _remember_turn_messages(aid, msg, structured)
                    except Exception:
                        pass
                dossier = db.get_agent_dossier(aid) or dossier_lib.empty_dossier()
                if (
                    dossier_lib.should_update_dossier_with_state(
                        msg, assistant_text, dossier
                    )
                    or _MEMORY_CANDIDATE_RE.search(msg)
                ):
                    threading.Thread(
                        target=_consider_agent_memory,
                        args=(aid, blk, msg, assistant_text),
                        daemon=True,
                    ).start()
            yield json.dumps({
                "metrics": {
                    "upstream_stream_ready_ms": round(
                        ((stream_ready_at or completed_at) - llm_started_at) * 1000,
                        1,
                    ),
                    "upstream_first_chunk_ms": round(
                        ((first_chunk_at or completed_at) - llm_started_at) * 1000,
                        1,
                    ),
                    "upstream_first_text_ms": round(
                        ((first_text_at or completed_at) - llm_started_at) * 1000,
                        1,
                    ),
                    "upstream_total_ms": round(
                        (completed_at - llm_started_at) * 1000,
                        1,
                    ),
                    "tool_name": executed_tool_name or "none",
                    "response_mode": response_mode or (
                        "act" if turn_had_mutation_receipt else "answer"
                    ),
                    "completion_authorized": bool(turn_had_mutation_receipt),
                    "tool_ms": round(direct_tool_ms, 1),
                    "retry_count": retry_count,
                    "retry_reasons": retry_reasons,
                    "ttft_budget_ms": round(ttft_budget_s * 1000, 1),
                    "llm_provider": active_provider,
                    "llm_model": model,
                    "failover_provider": failover_used or "",
                    "finish_reason": (
                        "not_addressed"
                        if ignored_as_background
                        else finish_reason
                    ),
                    "addressed": not ignored_as_background,
                    "addressed_hint": addressed_hint,
                },
            }, ensure_ascii=False) + "\n"
            if ignored_as_background:
                yield json.dumps(
                    {"ignored": True, "reason": "not_addressed"},
                    ensure_ascii=False,
                ) + "\n"
            yield json.dumps({"done": True}, ensure_ascii=False) + "\n"
        except Exception as error:
            yield json.dumps(
                {"error": "LLM 调用失败: %s" % error},
                ensure_ascii=False,
            ) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ============ 摄像头视觉（go2rtc 抓帧 → VLM 识别）============
# 摄像头：视频问候（视觉相关）

@app.websocket("/api/scene")
async def scene_ws(websocket: WebSocket):
    """Scene Protocol v1: hello/welcome/snapshot, patches, resync and intents."""
    await websocket.accept()
    shell_id = ""
    unsubscribe = None
    sender_task = None
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if not isinstance(hello, dict) or hello.get("v") != SCENE_PROTOCOL_VERSION or hello.get("type") != "hello":
            await websocket.close(code=1002)
            return
        shell_id = "%s:%s" % (hello.get("shell") or "shell", uuid.uuid4().hex[:8])
        loop = asyncio.get_running_loop()
        outbound = asyncio.Queue(maxsize=256)

        def enqueue(message):
            def put_now():
                try:
                    outbound.put_nowait(message)
                except asyncio.QueueFull:
                    # One full snapshot heals any dropped patch sequence.
                    while not outbound.empty():
                        try:
                            outbound.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    try:
                        outbound.put_nowait(scene_store.snapshot())
                    except asyncio.QueueFull:
                        pass
            loop.call_soon_threadsafe(put_now)

        unsubscribe = scene_store.subscribe(enqueue)
        scene_store.shell_connected(shell_id)
        await websocket.send_json({
            "v": SCENE_PROTOCOL_VERSION,
            "type": "welcome",
            "rev": scene_store.rev,
            "serverVersion": "ev-scene-1",
        })
        await websocket.send_json(scene_store.snapshot())

        async def send_loop():
            while True:
                await websocket.send_json(await outbound.get())

        sender_task = asyncio.create_task(send_loop())
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict) or message.get("v") != SCENE_PROTOCOL_VERSION:
                continue
            message_type = message.get("type")
            if message_type == "resync":
                await outbound.put(scene_store.snapshot())
            elif message_type == "ping":
                await outbound.put({"v": SCENE_PROTOCOL_VERSION, "type": "pong"})
            elif message_type == "intent":
                surface_id = str(message.get("surface") or "")
                name = str(message.get("name") or "")
                coding_turn_trace.record_runtime("shell.intent", {
                    "shell_id": shell_id, "surface_id": surface_id,
                    "name": name, "data": message.get("data") if isinstance(message.get("data"), dict) else {},
                }, category="shell")
                if name == "surface.close":
                    try:
                        scene_store.set_visible(surface_id, False)
                    except KeyError:
                        pass
                elif name == "surface.ready":
                    data = message.get("data") if isinstance(message.get("data"), dict) else {}
                    scene_store.mark_surface_ready(
                        surface_id,
                        shell_id=shell_id,
                        rev=int(data.get("rev") or scene_store.rev),
                        visible=data.get("visible") if isinstance(data.get("visible"), bool) else None,
                        focused=data.get("focused") if isinstance(data.get("focused"), bool) else None,
                        bounds=data.get("bounds") if isinstance(data.get("bounds"), dict) else None,
                    )
                    content_status = str(data.get("contentStatus") or "")
                    if content_status in ("loading", "ready", "error"):
                        scene_store.mark_content_status(
                            surface_id, shell_id=shell_id, status=content_status,
                            url=str(data.get("contentUrl") or ""), error=str(data.get("contentError") or ""),
                        )
                elif name in ("surface.content_loading", "surface.content_ready", "surface.content_error"):
                    data = message.get("data") if isinstance(message.get("data"), dict) else {}
                    scene_store.mark_content_status(
                        surface_id,
                        shell_id=shell_id,
                        status=name.rsplit("_", 1)[-1],
                        url=str(data.get("url") or ""),
                        error=str(data.get("error") or ""),
                    )
                elif name == "surface.content_size":
                    data = message.get("data") if isinstance(message.get("data"), dict) else {}
                    scene_store.mark_content_size(
                        surface_id,
                        shell_id=shell_id,
                        width=int(data.get("width") or 0),
                        height=int(data.get("height") or 0),
                    )
                    surface_layout.apply_measured_window_size(
                        surface_id,
                        height=int(data.get("height") or 0),
                        width=int(data.get("width") or 0),
                        declared_fit=str(data.get("fit") or ""),
                    )
                elif name == "surface.event":
                    data = message.get("data") if isinstance(message.get("data"), dict) else {}
                    event_name = str(data.get("name") or "")
                    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
                    surface = scene_store.get(surface_id) or {}
                    surface_data = surface.get("data") if isinstance(surface.get("data"), dict) else {}
                    content = surface_data.get("content") if isinstance(surface_data.get("content"), dict) else {}
                    source = content.get("source") if isinstance(content.get("source"), dict) else {}
                    app_arguments = surface_apps.command_from_event(surface_id, payload)
                    if app_arguments:
                        asyncio.create_task(asyncio.to_thread(surface_apps.execute, app_arguments))
                    elif source.get("type") == "project-plan" and event_name in ("plan.update", "plan.submit"):
                        aid = int((surface_data.get("source_state") or {}).get("agent_id") or 1)
                        plan_text = str(payload.get("plan_text") or "")[:12000]
                        steps = [line.strip().lstrip("-•0123456789. ").strip() for line in plan_text.splitlines() if line.strip()][:40]
                        work_id = str(source.get("work_id") or "")
                        work_revision = int(source.get("revision") or 0)
                        if event_name == "plan.update":
                            updated = coding_fsm.update_work_order(
                                aid, work_id=work_id, expected_revision=work_revision, plan_steps=steps,
                            )
                            if updated:
                                coding_fsm.update_brief(aid, {"plan_steps": steps})
                                coding_orch.push_studio(aid, status="待确认计划", detail="计划已保存，可继续修改或提交。", phase="awaiting_confirm", plan_steps=steps)
                            else:
                                coding_orch.push_studio(aid, status="计划已变化", detail="这份计划不是当前版本，请在最新窗口继续编辑。", phase="awaiting_confirm")
                        elif coding_fsm.get_phase(aid) == "awaiting_confirm":
                            approved = coding_fsm.approve_work_order(
                                aid, work_id=work_id, expected_revision=work_revision, plan_steps=steps,
                            )
                            if not approved:
                                coding_orch.push_studio(aid, status="未提交", detail="计划版本已变化，没有启动工作 Agent。", phase="awaiting_confirm")
                                continue
                            coding_fsm.update_brief(aid, {"plan_steps": steps})
                            brief = coding_fsm.load(aid).get("brief") or {}
                            task = "%s\n\nApproved plan:\n%s" % (brief.get("goal") or "按已确认计划完成项目", "\n".join(steps))
                            coding_fsm.transition(aid, "writing", reason="plan_submitted")

                            async def launch_confirmed_plan():
                                started = await asyncio.to_thread(
                                    coding_orch.start_writing, aid, task,
                                    get_setting=db.get_setting, set_setting=db.set_setting,
                                    base_url="http://127.0.0.1:8002", mode="external", open_desk=True,
                                )
                                if not (started.get("ok") or started.get("queued")):
                                    coding_fsm.transition(aid, "awaiting_confirm", reason="claude_start_failed")
                                    coding_orch.push_studio(
                                        aid, status="启动失败", detail=str(started.get("speech") or "工作 Agent 未启动"),
                                        phase="awaiting_confirm", plan_steps=steps,
                                    )

                            asyncio.create_task(launch_confirmed_plan())
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        if unsubscribe:
            unsubscribe()
        if shell_id:
            scene_store.shell_disconnected(shell_id)
        if sender_task:
            sender_task.cancel()
            with contextlib.suppress(BaseException):
                await sender_task


@app.websocket("/api/speaker/ws")
async def speaker_ws(websocket: WebSocket):
    """ESP32 等扬声器接入：握手报 {name, mac}，随后只收 Muse 下发的
    控制帧(JSON) 与 裸 PCM(binary)。断线即注销。"""
    await websocket.accept()
    loop = asyncio.get_running_loop()
    out_queue = asyncio.Queue(maxsize=512)
    client = websocket.client
    addr = client.host if client else ""
    mac, name = "", ""
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        mac = str(hello.get("mac") or "").strip()
        name = str(hello.get("name") or "").strip()
    except Exception:
        pass
    if not mac:
        mac = "esp32:%s" % (addr or uuid.uuid4().hex[:8])
    if not name:
        name = "网络扬声器"
    try:
        db.upsert_speaker_device(mac, name, addr)
    except Exception as e:
        print("[muse] 扬声器登记失败:", e, flush=True)
    sid = _SPEAKERS.add(mac, name, addr, out_queue, loop)
    print("[muse] 扬声器接入:", name, mac, addr, flush=True)

    async def pump():
        while True:
            item = await out_queue.get()
            if item is None:
                return
            if isinstance(item, (bytes, bytearray)):
                await websocket.send_bytes(item)
            else:
                await websocket.send_json(item)

    async def recv_loop():
        # 只为侦测断线/吸收设备心跳文本；无业务语义。
        while True:
            await websocket.receive_text()

    pump_task = asyncio.create_task(pump())
    recv_task = asyncio.create_task(recv_loop())
    try:
        await asyncio.wait({pump_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        _SPEAKERS.remove(sid)
        for task in (pump_task, recv_task):
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        print("[muse] 扬声器断开:", name, mac, flush=True)






def _startup_prewarm():
    time.sleep(0.2)
    try:
        from devices.coding.surface_tools import (
            ensure_pinned_surfaces,
            ensure_status_timeline_surface,
            reconcile_status_timeline_height,
        )
        ensure_pinned_surfaces()
        ensure_status_timeline_surface()
        # 面板内容在内存、窗口高度在磁盘，重启后必须对齐，否则状态栏会顶着空白展开区
        reconcile_status_timeline_height()
    except Exception as error:
        print("[muse] 常驻窗口初始化失败: %s" % error, flush=True)
    result = _prewarm_agent(1)
    print("[muse] 低延迟连接预热:", result, flush=True)
    for query, kind in ((_DEFAULT_WEATHER_LOCATION + "天气", "weather"), ("最近新闻", "web_search")):
        try:
            _voice_realtime_tool(query, forced_kind=kind)
        except Exception as error:
            print("[muse] 实时工具预取失败:", error, flush=True)


_VOICE_SUPERVISOR_STOP = threading.Event()
_VOICE_MANUAL_STOP = threading.Event()
_VOICE_WAKE = threading.Event()
_VOICE_CHILD_LOCK = threading.Lock()
_VOICE_CHILD = {"proc": None}


def _voice_feature_enabled() -> bool:
    raw = db.get_setting("feat.voice", None)
    if raw is None:
        raw = db.get_setting("feat.camera_voice", "1")
    return str(raw or "1") == "1"


def _set_voice_feature(enabled: bool) -> None:
    flag = "1" if enabled else "0"
    db.set_setting("feat.voice", flag)
    db.set_setting("feat.camera_voice", flag)


def _voice_control_snapshot(agent_id: int) -> dict:
    runtime = live_hub.local_voice_status(agent_id)
    stopped = (
        _VOICE_MANUAL_STOP.is_set()
        or db.get_setting("voice.runtime.stopped", "0") == "1"
    )
    enabled = _voice_feature_enabled()
    if stopped:
        mode = "stopped"
    elif not enabled:
        mode = "paused"
    elif runtime.get("running"):
        mode = "running"
    else:
        mode = "starting"
    return {
        "ok": True,
        "agent_id": int(agent_id),
        "mode": mode,
        "enabled": enabled,
        "runtime": runtime,
        "status_theme": surface_tools.status_timeline_theme(),
    }


def _terminate_voice_child() -> None:
    with _VOICE_CHILD_LOCK:
        proc = _VOICE_CHILD.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


@app.get("/api/agents/{aid}/voice/control")
def api_agent_voice_control_get(aid: int):
    if not db.get_agent(aid):
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    return _voice_control_snapshot(aid)


@app.post("/api/agents/{aid}/voice/control")
def api_agent_voice_control(aid: int, payload: dict = Body(...)):
    """状态浮层的语音 Agent 控制：pause 不识别，stop 同时释放麦克风。"""
    if not db.get_agent(aid):
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    action = str((payload or {}).get("action") or "").strip().lower()
    if action == "pause":
        _set_voice_feature(False)
        live_hub.set_stage(
            aid, speaking=False, listening=False, standby=True, level=0.0,
        )
    elif action in ("start", "resume"):
        db.set_setting("voice.runtime.stopped", "0")
        _set_voice_feature(True)
        _VOICE_MANUAL_STOP.clear()
        _VOICE_WAKE.set()
        live_hub.set_stage(aid, listening=False, standby=False, level=0.0)
    elif action == "stop":
        _set_voice_feature(False)
        db.set_setting("voice.runtime.stopped", "1")
        _VOICE_MANUAL_STOP.set()
        _VOICE_WAKE.set()
        _terminate_voice_child()
        live_hub.mark_voice_stopped(aid)
    else:
        return JSONResponse(
            {"ok": False, "error": "action 必须是 start/resume/pause/stop"},
            status_code=400,
        )
    return _voice_control_snapshot(aid)


def _voice_terminal_supervisor():
    """主进程拥有并持续看护语音终端；退出后退避重启。"""
    autostart = os.environ.get("MUSE_VOICE_AUTOSTART", "1").lower() not in (
        "0", "off", "false", "no",
    )
    persisted_stop = db.get_setting("voice.runtime.stopped", "0") == "1"
    if not autostart or persisted_stop:
        _VOICE_MANUAL_STOP.set()
        reason = "MUSE_VOICE_AUTOSTART=0" if not autostart else "用户上次手动停止"
        print("[muse] 语音终端等待面板启动（%s）" % reason, flush=True)
    # 等本进程 HTTP 可响应，避免终端启动瞬间连不上 Muse
    deadline = time.time() + 45
    while time.time() < deadline:
        if _tcp_open("127.0.0.1", int(os.environ.get("MUSE_PORT", "8002"))):
            break
        time.sleep(0.4)
    log_path = MUSE_DIR / "tmp" / "voice_terminal.log"
    err_path = MUSE_DIR / "tmp" / "voice_terminal.err.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("MUSE_URL", "http://127.0.0.1:8002")
    env.setdefault("VOICE_INPUT", env.get("CAMERA_VOICE_INPUT", "auto"))
    env.setdefault("VOICE_OUTPUT", env.get("CAMERA_VOICE_OUTPUT", "pc"))
    env.setdefault("VOICE_AGENT", env.get("CAMERA_VOICE_AGENT", "1"))
    env["MUSE_PARENT_PID"] = str(os.getpid())
    py = env.get("MUSE_PYTHON") or sys.executable
    if Path(py).resolve() != Path(sys.executable).resolve():
        print(
            "[muse] 拒绝混用 Python：主进程=%s，配置=%s；语音使用主进程解释器"
            % (sys.executable, py),
            flush=True,
        )
    py = sys.executable
    restarts = 0
    while not _VOICE_SUPERVISOR_STOP.is_set():
        if _VOICE_MANUAL_STOP.is_set():
            _VOICE_WAKE.wait(0.5)
            _VOICE_WAKE.clear()
            continue
        started_at = time.monotonic()
        try:
            with open(log_path, "a", encoding="utf-8") as out, open(err_path, "a", encoding="utf-8") as err:
                out.write(
                    "\n--- voice terminal supervised %s python=%s ---\n"
                    % (datetime.datetime.now().isoformat(timespec="seconds"), py)
                )
                out.flush()
                proc = subprocess.Popen(
                    [py, "-X", "utf8", "-m", "devices.voice.terminal"],
                    cwd=str(MUSE_DIR),
                    env=env,
                    stdout=out,
                    stderr=err,
                )
                with _VOICE_CHILD_LOCK:
                    _VOICE_CHILD["proc"] = proc
                print(
                    "[muse] 语音终端已启动 pid=%d python=%s" % (proc.pid, py),
                    flush=True,
                )
                returncode = None
                agent_id = int(env.get("VOICE_AGENT") or 1)
                next_health_check = started_at + 15
                while proc.poll() is None:
                    if _VOICE_SUPERVISOR_STOP.is_set() or _VOICE_MANUAL_STOP.is_set():
                        proc.terminate()
                        break
                    now = time.monotonic()
                    if now >= next_health_check:
                        next_health_check = now + 2.0
                        status = live_hub.local_voice_status(agent_id)
                        if not (
                            status.get("pid") == proc.pid
                            and status.get("running")
                        ):
                            print(
                                "[muse] 语音终端心跳失联，终止并重启 pid=%d status=%s"
                                % (proc.pid, status),
                                flush=True,
                            )
                            proc.terminate()
                            break
                    _VOICE_WAKE.wait(0.25)
                    _VOICE_WAKE.clear()
                try:
                    returncode = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    returncode = proc.wait(timeout=2)
                finally:
                    with _VOICE_CHILD_LOCK:
                        if _VOICE_CHILD.get("proc") is proc:
                            _VOICE_CHILD["proc"] = None
        except Exception as error:
            returncode = "launch_error:%s" % error
        if _VOICE_SUPERVISOR_STOP.is_set():
            break
        if _VOICE_MANUAL_STOP.is_set():
            live_hub.mark_voice_stopped(int(env.get("VOICE_AGENT") or 1))
            restarts = 0
            continue
        uptime = time.monotonic() - started_at
        restarts = 0 if uptime >= 60 else restarts + 1
        delay = min(30, max(1, 2 ** min(restarts, 4)))
        print(
            "[muse] 语音终端退出 code=%s uptime=%.1fs，%ds 后重启"
            % (returncode, uptime, delay),
            flush=True,
        )
        _VOICE_WAKE.wait(delay)
        _VOICE_WAKE.clear()


def _stop_voice_terminal_supervisor():
    _VOICE_SUPERVISOR_STOP.set()
    _VOICE_WAKE.set()
    _terminate_voice_child()


def _tauri_shell_binary() -> Path:
    return MUSE_DIR / "desktop-tauri" / "src-tauri" / "target" / "release" / "ev-tauri-shell"


def _desktop_shell_process_prefix() -> str:
    return str(_tauri_shell_binary())


def _ensure_desktop_shell():
    """启动项目唯一的 Tauri Scene 桌面壳。"""
    if sys.platform != "darwin":
        return
    if os.environ.get("MUSE_DESKTOP_AUTOSTART", "1").lower() in ("0", "off", "false", "no"):
        print("[muse] 桌面壳自启已关闭（MUSE_DESKTOP_AUTOSTART=0）", flush=True)
        return
    now = time.monotonic()
    with _DESKTOP_SHELL_LAUNCH_LOCK:
        if _desktop_shell_alive():
            return
        if now - _DESKTOP_SHELL_LAST_LAUNCH_AT["at"] < 3:
            return
        _DESKTOP_SHELL_LAST_LAUNCH_AT["at"] = now
        tauri_bin = _tauri_shell_binary()
        desktop_dir = MUSE_DIR / "desktop-tauri"
        if not tauri_bin.is_file():
            print("[muse] Tauri 尚未构建，桌面壳不会启动", flush=True)
            return
        command = [str(tauri_bin)]
        log_path = MUSE_DIR / "tmp" / "desktop-shell.log"
        err_path = MUSE_DIR / "tmp" / "desktop-shell.err.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.setdefault("MUSE_SCENE_WS", "ws://127.0.0.1:%s/api/scene" % os.environ.get("MUSE_PORT", "8002"))
        try:
            with open(log_path, "a", encoding="utf-8") as out, open(err_path, "a", encoding="utf-8") as err:
                subprocess.Popen(
                    command,
                    cwd=str(desktop_dir),
                    env=env,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
            print("[muse] 已请求启动 Tauri Scene 桌面壳", flush=True)
        except Exception as error:
            print("[muse] Tauri 桌面壳自启失败:", error, flush=True)


def _desktop_shell_alive() -> bool:
    """桌面壳主进程是否存活（精确命令前缀，不使用 pgrep/正则）。"""
    try:
        out = subprocess.run(
            ["ps", "-axo", "command="],
            capture_output=True, text=True, timeout=5,
        )
        target = _desktop_shell_process_prefix()
        return any(
            line.strip().startswith(target)
            for line in out.stdout.splitlines()
        )
    except Exception:
        return True  # 检测失败时保守处理，不盲目重启


_DESKTOP_SHELL_LAST_LAUNCH_AT = {"at": 0.0}
_DESKTOP_SHELL_LAUNCH_LOCK = threading.Lock()


def _watch_desktop_shell():
    """周期看护：桌面壳进程消失后自动拉起。"""
    if sys.platform != "darwin":
        return
    if os.environ.get("MUSE_DESKTOP_WATCH", "1").lower() in ("0", "off", "false", "no"):
        print("[muse] 桌面壳看护已关闭（MUSE_DESKTOP_WATCH=0）", flush=True)
        return
    if os.environ.get("MUSE_DESKTOP_AUTOSTART", "1").lower() in ("0", "off", "false", "no"):
        return
    while True:
        try:
            if not _desktop_shell_alive():
                now = time.monotonic()
                if now - _DESKTOP_SHELL_LAST_LAUNCH_AT["at"] > 30:
                    print("[muse] 桌面壳进程消失，自动拉起", flush=True)
                    _ensure_desktop_shell()
        except Exception as error:
            print("[muse] 桌面壳看护异常:", error, flush=True)
        time.sleep(15)


def _search_stack_specs():
    """本地开源检索栈：SearXNG（多引擎聚合）+ AgentSearch（编排与正文抽取）。

    两者都跑在自己的 venv 里（依赖版本与 EV 主环境冲突，见 vendor/README 说明），
    通过回环 HTTP 互相调用，这里只负责「没起就拉起来」。
    """
    vendor = MUSE_DIR / "vendor"
    return [
        ("searxng", vendor / "start_searxng.sh", "http://127.0.0.1:8088/healthz"),
        ("agentsearch", vendor / "start_agentsearch.sh", "http://127.0.0.1:3939/health"),
    ]


def _search_service_alive(probe_url: str) -> bool:
    try:
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            return client.get(probe_url).status_code < 500
    except Exception:
        return False


def _ensure_search_stack():
    """确保本地检索栈在跑。缺服务时搜索会退回 Tavily/本地兜底，不会让语音链路挂掉。"""
    if os.environ.get("MUSE_SEARCH_AUTOSTART", "1").lower() in ("0", "off", "false", "no"):
        print("[muse] 本地检索栈自启已关闭（MUSE_SEARCH_AUTOSTART=0）", flush=True)
        return
    log_dir = MUSE_DIR / "tmp"
    log_dir.mkdir(parents=True, exist_ok=True)
    for name, script, probe in _search_stack_specs():
        if not script.is_file():
            print("[muse] 检索栈 %s 未安装（缺 %s），跳过" % (name, script.name), flush=True)
            continue
        if _search_service_alive(probe):
            continue
        try:
            with open(log_dir / ("%s.log" % name), "a", encoding="utf-8") as out:
                subprocess.Popen(
                    ["bash", str(script)],
                    cwd=str(script.parent),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            print("[muse] 已拉起检索服务 %s" % name, flush=True)
        except Exception as error:
            print("[muse] 拉起 %s 失败: %s" % (name, error), flush=True)


def _watch_search_stack():
    """SearXNG 起得慢（要加载引擎），首轮多等一会儿；之后每分钟巡检一次。"""
    time.sleep(20)
    while True:
        try:
            _ensure_search_stack()
        except Exception as error:
            print("[muse] 检索栈看护异常:", error, flush=True)
        time.sleep(60)


@app.on_event("startup")
def start_latency_prewarm():
    threading.Thread(target=_startup_prewarm, daemon=True).start()
    threading.Thread(target=_voice_terminal_supervisor, daemon=True).start()
    threading.Thread(target=_ensure_desktop_shell, daemon=True).start()
    threading.Thread(target=_watch_desktop_shell, daemon=True).start()
    threading.Thread(target=_ensure_search_stack, daemon=True).start()
    threading.Thread(target=_watch_search_stack, daemon=True).start()


@app.on_event("shutdown")
def stop_supervised_processes():
    _stop_voice_terminal_supervisor()




def _gateway_auth_ok(request: Request, authorization: str = None, x_api_key: str = None) -> bool:
    cfg = claude_code_skill.load_config(db.get_setting, db.set_setting)
    if not cfg.get("enabled"):
        return False
    token = claude_code_skill.ensure_gateway_token(db.get_setting, db.set_setting)
    if not token:
        return False
    auth = (authorization or request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        got = auth[7:].strip()
        if secrets.compare_digest(got, token):
            return True
    xk = (x_api_key or request.headers.get("x-api-key") or "").strip()
    if xk and secrets.compare_digest(xk, token):
        return True
    # also accept ANTHROPIC_AUTH_TOKEN style via header anthropic-api-key if present
    ak = (request.headers.get("anthropic-api-key") or "").strip()
    if ak and secrets.compare_digest(ak, token):
        return True
    return False


@app.get("/v1/models")
def api_v1_models(request: Request, authorization: str = Header(None), x_api_key: str = Header(None)):
    if not _gateway_auth_ok(request, authorization, x_api_key):
        return JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": "invalid token"}}, status_code=401)
    cfg = claude_code_skill.load_config(db.get_setting, db.set_setting)
    blk, err = anthropic_gw.resolve_agent_llm(db, cfg.get("agent_id"))
    model = (blk or {}).get("model_name") or "ev-gateway"
    return {
        "object": "list",
        "data": [{
            "id": model,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "ev-gateway",
        }],
    }


@app.post("/v1/messages")
async def api_v1_messages(
    request: Request,
    payload: dict = Body(...),
    authorization: str = Header(None),
    x_api_key: str = Header(None),
):
    """Anthropic Messages 兼容网关 → 智能体 OpenAI 兼容 LLM（Claude Code 后端）。"""
    if not _gateway_auth_ok(request, authorization, x_api_key):
        return JSONResponse(
            {"type": "error", "error": {"type": "authentication_error", "message": "invalid token or skill disabled"}},
            status_code=401,
        )
    cfg = claude_code_skill.load_config(db.get_setting, db.set_setting)
    blk, err = anthropic_gw.resolve_agent_llm(db, cfg.get("agent_id"))
    if err:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error", "message": err}},
            status_code=400,
        )
    messages, tools, extras = anthropic_gw.anthropic_messages_to_openai(payload or {})
    if not messages:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "empty messages"}},
            status_code=400,
        )
    url, key, model = blk.get("url"), blk.get("api_key"), blk.get("model_name")
    # Claude Code may send its own model name; ignore and use agent LLM
    max_tokens = int((payload or {}).get("max_tokens") or blk.get("max_tokens") or 4096)
    max_tokens = max(64, min(max_tokens, 128000))
    temperature = (payload or {}).get("temperature")
    if temperature is None:
        temperature = float(blk.get("temperature", 0.7) or 0.7)
    client = _openai_client(url, key)
    req_kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": float(temperature),
        "timeout": 180,
    }
    if tools:
        req_kwargs["tools"] = tools
    req_kwargs.update(extras)
    req_kwargs.update(anthropic_gw.deepseek_agent_extras(
        url, model, enable_thinking=bool(cfg.get("enable_thinking"))))
    want_stream = bool((payload or {}).get("stream"))

    if want_stream:
        def gen():
            try:
                stream = client.chat.completions.create(stream=True, **req_kwargs)
                for chunk in anthropic_gw.stream_openai_to_anthropic_sse(stream, model):
                    yield chunk
            except Exception as e:
                yield anthropic_gw._sse("error", {
                    "type": "error",
                    "error": {"type": "api_error", "message": str(e)},
                })
        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        r = client.chat.completions.create(stream=False, **req_kwargs)
        msg = anthropic_gw.openai_message_to_anthropic(r.choices[0].message, model)
        usage = getattr(r, "usage", None)
        if usage is not None:
            msg["usage"] = {
                "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            }
        return msg
    except Exception as e:
        return JSONResponse(
            {"type": "error", "error": {"type": "api_error", "message": str(e)}},
            status_code=502,
        )



# ============ EV Desk ============
@app.get("/desk/{window_id}", response_class=HTMLResponse)
def desk_page(window_id: str):
    """默认 Desk 壳（零构建 desk.html）。React+Streamdown+Pretext 构建版见 /desk-app/{id}。"""
    page = UI_DIR / "desk.html"
    if not page.exists():
        return HTMLResponse("desk UI missing", status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/desk-app/{window_id}", response_class=HTMLResponse)
def desk_app_page(window_id: str):
    page = UI_DIR / "desk" / "index.html"
    if not page.exists():
        return HTMLResponse("desk-app build missing; run npm run build:desk in ui/desk-app", status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/api/desk/windows")
def api_desk_windows():
    return {"ok": True, "windows": desk_hub.list_windows()}


@app.get("/api/desk/windows/{window_id}")
def api_desk_window_get(window_id: str):
    w = desk_hub.get_window(window_id)
    return {"ok": bool(w), "window": w}


@app.post("/api/desk/windows")
def api_desk_window_upsert(payload: dict = Body(...)):
    body = payload or {}
    try:
        w = desk_hub.upsert_window(body.get("schema") or body, data=body.get("data"), replace=bool(body.get("replace")))
        return {"ok": True, "window": w}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/desk/{window_id}/events")
def api_desk_events(window_id: str):
    return StreamingResponse(
        desk_hub.sse_events(window_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/api/desk/action")
def api_desk_action(payload: dict = Body(...), request: Request = None):
    body = payload or {}
    aid = int(body.get("agent_id") or 0)
    result = desk_actions.dispatch(
        body.get("action") or "",
        body.get("payload") or {},
        aid=aid,
        get_setting=db.get_setting,
    )
    return result


@app.post("/api/desk/compose")
def api_desk_compose(payload: dict = Body(...), request: Request = None):
    body = payload or {}
    cwd = body.get("cwd") or str(coding_path_policy.default_external_root(db.get_setting))
    out = desk_compose.compose_and_open(
        user_text=body.get("text") or body.get("query") or "",
        cwd=cwd,
        get_setting=db.get_setting,
        gather_plan=body.get("gather_plan"),
        llm_schema=body.get("schema"),
        window_id=body.get("window_id") or "",
        title=body.get("title") or "",
    )
    wid = (out.get("window") or {}).get("id")
    # 默认只落 Desk 数据；Chrome --app= 需显式 open_native=true（终端用 live_hub 浮窗）
    if body.get("open_native") and wid:
        base = _claude_code_base_url(request)
        coding_native_ui.open_desk_window("%s/desk/%s" % (base, wid))
    return out







# ============ UI 托管 ============
def _status_ui_revision():
    """运行时读取页面版本；只改 HTML 也能让已打开的桌面 iframe 自刷新。"""
    page = UI_DIR / "status_timeline.html"
    try:
        stat = page.stat()
        return "%x-%x" % (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return "missing"


@app.get("/api/ui/status-version")
def api_status_ui_version():
    return JSONResponse(
        {"revision": _status_ui_revision()},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((UI_DIR / "index.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@app.get("/remote")
@app.get("/remote/{agent_id}")
def remote_entry(agent_id: int = None):
    q = "muse=1&remote=1&agent_id=%d&v=0256" % agent_id if agent_id else "muse=1&remote=1&v=0256"
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><script>location.replace("/terminal/index.html?' + q + '");</script>',
        headers={"Cache-Control": "no-store"},
    )


@app.get("/static/{fname:path}")
def static_file(fname: str):
    p = (UI_DIR / fname).resolve()
    try:
        p.relative_to(UI_DIR.resolve())
    except Exception:
        return Response(status_code=404)
    if not p.exists() or not p.is_file():
        return Response(status_code=404)
    suffix = p.suffix.lower()
    mt = {
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".mjs": "application/javascript; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".svg": "image/svg+xml",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".json": "application/json",
        ".map": "application/json",
        ".glb": "model/gltf-binary",
        ".gltf": "model/gltf+json",
    }.get(suffix, "application/octet-stream")
    if suffix in (".css", ".js", ".mjs", ".html", ".svg", ".json", ".map"):
        return Response(p.read_text(encoding="utf-8", errors="replace"), media_type=mt,
                        headers={"Cache-Control": "no-store"})
    return FileResponse(str(p), media_type=mt, headers={"Cache-Control": "no-store"})


# ============ ESP-Claw WebSerial 刷写器（同源、自包含静态快照） ============
def _flash_page_html():
    page = ESP_CLAW_FLASH_DIR / "index.html"
    if not page.exists():
        return None
    cfg = _esp_claw_runtime_config()
    runtime = """<script>
window.__MUSE_ESP_CLAW_CONFIG__=%s;
(()=>{const cfg=window.__MUSE_ESP_CLAW_CONFIG__;const nativeFetch=window.fetch.bind(window);
window.fetch=(input,init)=>{const raw=typeof input==='string'?input:(input&&input.url)||'';let next=raw;
const versions='https://esp-claw.com/versions';const firmware='https://esp-claw.com/firmware';
if(raw.startsWith(versions))next=cfg.versions_url+raw.slice(versions.length);
else if(raw.startsWith(firmware))next=cfg.firmware_origin+'/firmware'+raw.slice(firmware.length);
return nativeFetch(next,init);};})();
</script>
<link rel="stylesheet" href="/flash/muse-bridge.css">
<script src="/flash/muse-bridge.js" defer></script>
""" % json.dumps(cfg, ensure_ascii=False).replace("<", "\\u003c")
    html = page.read_text(encoding="utf-8")
    return html.replace("</head>", runtime + "</head>", 1)


@app.get("/flash", response_class=HTMLResponse)
@app.get("/flash/", response_class=HTMLResponse)
def esp_claw_flash_page():
    html = _flash_page_html()
    if html is None:
        return HTMLResponse("ESP-Claw flash assets not built", status_code=503)
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "Permissions-Policy": "serial=(self), usb=(self)",
        },
    )


@app.get("/flash/{path:path}")
def esp_claw_flash_asset(path: str):
    candidate = (ESP_CLAW_FLASH_DIR / path).resolve()
    root = ESP_CLAW_FLASH_DIR.resolve()
    try:
        inside = candidate.is_relative_to(root)
    except AttributeError:
        inside = str(candidate).startswith(str(root) + os.sep)
    if not inside or not candidate.exists() or candidate.is_dir():
        return Response("not found", status_code=404)
    return FileResponse(str(candidate), headers={"Cache-Control": "public, max-age=3600"})


# ============ Muse Terminal（同源托管 digital-human，正式会话终端） ============
@app.get("/terminal")
def terminal_index():
    return terminal_static("index.html")


@app.get("/terminal/{path:path}")
def terminal_static(path: str):
    if not path or path.endswith("/"):
        path = path + "index.html"
    p = (DH_DIR / path).resolve()
    if not str(p).startswith(str(DH_DIR.resolve())) or not p.exists() or p.is_dir():
        return Response("not found", status_code=404)
    # 终端调试期禁用缓存，避免浏览器/WebView 吃旧 JS 导致修复不生效
    return FileResponse(str(p), headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.get("/api/agents/{agent_id}/terminal")
def api_agent_terminal(agent_id: int, request: Request, role: str = "host"):
    agent = db.get_agent(agent_id)
    if not agent:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    agent_name = str(agent.get("name") or "Agent").strip()
    terminal_name = (
        "EV Terminal"
        if agent_name.lower() == "ev"
        else "%s · EV Terminal" % agent_name
    )
    mac = "muse:%012d" % agent_id
    is_remote = role == "remote"
    client_id = "muse-agent-%d-remote" % agent_id if is_remote else "muse-agent-%d-terminal" % agent_id
    device = db.touch_or_create_device(mac, client_id)
    if device and device["agent_id"] is None and not is_remote:
        db.bind_device(device["bind_code"], agent_id, terminal_name)
        device = db.get_device_by_mac(mac)
    base = _external_base_url(request)
    bound = bool(device and device["agent_id"])
    return {
        "agent_id": agent_id,
        "agent_name": agent.get("name"),
        "avatar": agent.get("avatar") or AVATAR_VISUALIZER,
        "avatar_model": _resolve_avatar_model(agent.get("avatar")),
        "device": {
            "mac": mac,
            "client_id": client_id,
            "name": terminal_name,
            "agent_id": device["agent_id"] if device else agent_id,
            "bind_code": device["bind_code"] if device else "",
            "bound": bound,
            "last_seen": device["last_seen"] if device else None,
        },
        "terminal_url": "/terminal/index.html?muse=1&agent_id=%d" % agent_id,
        "ota_url": "%s/xiaozhi/ota/" % base,
    }


@app.get("/api/agents/{agent_id}/session/host")
def api_session_host(agent_id: int):
    # 浏览器终端不再占语音会话；语音始终走本机 camera/PC 链路。
    return {
        "registered": False,
        "ready": False,
        "agent_id": agent_id,
        "browser_voice": False,
    }

LIVE2D_DIR = DH_DIR / "js" / "live2d"
RES_DIR = DH_DIR / "resources"


@app.get("/avatar-lib/{fname}")
def avatar_lib(fname: str):
    p = LIVE2D_DIR / fname
    if ".." in fname or not p.exists():
        return Response(status_code=404)
    return FileResponse(str(p))


@app.get("/avatar-res/{path:path}")
def avatar_res(path: str):
    p = (RES_DIR / path).resolve()
    if not str(p).startswith(str(RES_DIR.resolve())) or not p.exists():
        return Response(status_code=404)
    return FileResponse(str(p))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002, log_level="info")
