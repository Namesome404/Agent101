# -*- coding: utf-8 -*-
"""Agent dossier: static persona + evolving user/relationship/event profiles.

Core persona stays in agents.prompt (manual).
This module owns structured, auto-updated state injected beside the persona.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

VERSION = 1
MAX_LIST = 12
MAX_EVENTS = 16
MAX_NOTES = 10

_TZ_LOCAL = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(_TZ_LOCAL).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ_LOCAL)
        return dt
    except ValueError:
        return None


def empty_dossier() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "user_profile": {
            "name": "",
            "aliases": [],
            "preferences": [],
            "dislikes": [],
            "habits": [],
            "context": {},
            "notes": [],
        },
        "relationship": {
            "tone": "",
            "style": "",
            "trust_notes": [],
            "ongoing_topics": [],
        },
        "events": [],
        "updated_at": "",
    }


def _as_list(value: Any, limit: int = MAX_LIST) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text[:120])
        if len(out) >= limit:
            break
    return out


def _as_str(value: Any, limit: int = 80) -> str:
    return str(value or "").strip()[:limit]


def normalize_dossier(raw: Any) -> Dict[str, Any]:
    base = empty_dossier()
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        return base

    user = raw.get("user_profile") if isinstance(raw.get("user_profile"), dict) else {}
    rel = raw.get("relationship") if isinstance(raw.get("relationship"), dict) else {}
    events_in = raw.get("events") if isinstance(raw.get("events"), list) else []

    context = user.get("context") if isinstance(user.get("context"), dict) else {}
    clean_context = {}
    for key, value in list(context.items())[:12]:
        k = str(key or "").strip()[:40]
        v = str(value or "").strip()[:120]
        if k and v:
            clean_context[k] = v

    events: List[Dict[str, Any]] = []
    for item in events_in[: MAX_EVENTS * 2]:
        if not isinstance(item, dict):
            continue
        title = _as_str(item.get("title"), 80)
        if not title:
            continue
        status = _as_str(item.get("status"), 20) or "open"
        if status not in ("open", "waiting", "done", "dropped"):
            status = "open"
        event_id = _as_str(item.get("id"), 40) or uuid.uuid4().hex[:10]
        events.append({
            "id": event_id,
            "title": title,
            "status": status,
            "when_text": _as_str(item.get("when_text"), 80),
            "when_at": _as_str(item.get("when_at"), 40),
            "last_mentioned_at": _as_str(item.get("last_mentioned_at"), 40) or _now_iso(),
            "notes": _as_str(item.get("notes"), 160),
        })
        if len(events) >= MAX_EVENTS:
            break

    # 读档时顺带丢掉 CRM/分析腔备注，避免继续污染提示词
    notes = [
        n for n in _as_list(user.get("notes"), MAX_NOTES)
        if not _looks_like_meta_crm(n)
    ]
    preferences = [
        n for n in _as_list(user.get("preferences"))
        if not _looks_like_meta_crm(n)
    ]
    clean_context = {
        k: v for k, v in clean_context.items()
        if not _looks_like_meta_crm(k) and not _looks_like_meta_crm(v)
    }

    return {
        "version": VERSION,
        "user_profile": {
            "name": _as_str(user.get("name"), 40),
            "aliases": _as_list(user.get("aliases"), 6),
            "preferences": preferences,
            "dislikes": _as_list(user.get("dislikes")),
            "habits": _as_list(user.get("habits")),
            "context": clean_context,
            "notes": notes,
        },
        "relationship": {
            "tone": _as_str(rel.get("tone"), 40),
            "style": _as_str(rel.get("style"), 80),
            "trust_notes": [
                n for n in _as_list(rel.get("trust_notes"), 8)
                if not _looks_like_meta_crm(n)
            ],
            "ongoing_topics": _as_list(rel.get("ongoing_topics"), 8),
        },
        "events": events,
        "updated_at": _as_str(raw.get("updated_at"), 40),
    }


def bootstrap_from_memory_items(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One-time: fold legacy fact bullets into user_profile.notes."""
    dossier = empty_dossier()
    notes = []
    for item in items or []:
        text = (item.get("text") if isinstance(item, dict) else str(item) or "").strip()
        if text:
            notes.append(text[:120])
        if len(notes) >= MAX_NOTES:
            break
    dossier["user_profile"]["notes"] = notes
    dossier["updated_at"] = _now_iso()
    return dossier


def event_temporal_state(event: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """Derive temporal label for prompting: upcoming / ongoing / overdue / past / undated."""
    now = now or datetime.now(_TZ_LOCAL)
    status = event.get("status") or "open"
    if status == "done":
        return "past_done"
    if status == "dropped":
        return "past_dropped"
    when_at = _parse_iso(event.get("when_at"))
    if when_at is None:
        # Soft decay: not mentioned for 14 days → likely stale
        mentioned = _parse_iso(event.get("last_mentioned_at"))
        if mentioned and now - mentioned > timedelta(days=14):
            return "stale"
        return "ongoing" if status in ("open", "waiting") else "undated"
    if when_at > now + timedelta(hours=1):
        return "upcoming"
    if when_at >= now - timedelta(hours=6):
        return "ongoing"
    if status == "waiting":
        return "overdue"
    return "overdue"


def _join_lines(label: str, values: List[str]) -> str:
    if not values:
        return ""
    return "%s：%s" % (label, "；".join(values))


_CHITCHAT_TOPIC_RE = re.compile(
    r"(蜘蛛侠|钢铁侠|漫威|dc\b|电影|电视剧|综艺|动漫|漫画|游戏|"
    r"f1|赛车|足球|篮球|比赛|八卦|闲聊|吐槽|"
    r"刚聊|最近聊|上次聊)",
    re.I,
)

# CRM/分析腔：像在写用户报告，注入后模型爱复述炫耀
_META_CRM_RE = re.compile(
    r"(技术咨询需求|可能涉及|用户对.+有.+需求|工作或项目|"
    r"用户画像|相处状态|运行时档案|只调语气|勿当话题|"
    r"稳定特质|话题菜单|静默背景|长期背景分析|"
    r"咨询意向|画像备注|关系备注：)",
    re.I,
)


def _looks_like_chitchat_topic(text: str) -> bool:
    """Entertainment/chitchat titles are interests, not work items."""
    value = str(text or "").strip()
    if not value:
        return False
    if re.search(r"(截止|交稿|提交|开会|提醒|项目|作业|约定|报名|预约|交付|跟进)", value):
        return False
    return bool(_CHITCHAT_TOPIC_RE.search(value))


def _looks_like_meta_crm(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(_META_CRM_RE.search(value))


def _clean_fact_list(values: List[str], limit: int = MAX_LIST) -> List[str]:
    out = []
    for item in values or []:
        text = str(item or "").strip()
        if not text or _looks_like_meta_crm(text):
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out


def dossier_to_prompt(
    dossier: Dict[str, Any],
    voice_mode: bool = False,
) -> str:
    """Render structured dossier into one quiet private-memory system message."""
    data = normalize_dossier(dossier)
    user = data["user_profile"]
    rel = data["relationship"]
    now = datetime.now(_TZ_LOCAL)

    # 用「私房备忘」语气，避免「用户画像/相处状态」这类标题被模型原样念出来
    lines = [
        "Private memory for you only. Never announce, quote, or summarize this block.",
        "Do not say things like「根据你的档案」「我知道你在做…」「相处状态是…」.",
        "Use facts only when the current turn naturally needs them; never show off memory.",
    ]

    facts = []
    if user.get("name"):
        facts.append("name/call: " + user["name"])
    if user.get("aliases"):
        facts.append("also called: " + "、".join(user["aliases"][:4]))
    prefs = _clean_fact_list(user.get("preferences") or [], 6 if voice_mode else 10)
    if prefs:
        facts.append("likes/prefs: " + "；".join(prefs))
    dislikes = _clean_fact_list(user.get("dislikes") or [], 4 if voice_mode else 8)
    if dislikes:
        facts.append("dislikes: " + "；".join(dislikes))
    habits = _clean_fact_list(user.get("habits") or [], 4 if voice_mode else 8)
    if habits:
        facts.append("habits: " + "；".join(habits))
    notes = _clean_fact_list(user.get("notes") or [], 4 if voice_mode else MAX_NOTES)
    if notes:
        facts.append("notes: " + "；".join(notes))
    if user.get("context"):
        ctx_bits = []
        for key, value in user["context"].items():
            if _looks_like_meta_crm(key) or _looks_like_meta_crm(value):
                continue
            ctx_bits.append("%s=%s" % (key, value))
            if len(ctx_bits) >= (4 if voice_mode else 8):
                break
        if ctx_bits:
            facts.append("context: " + "；".join(ctx_bits))
    if facts:
        lines.append("Known facts:")
        lines.extend("- " + bit for bit in facts[: (6 if voice_mode else 12)])

    tone_bits = []
    if rel.get("tone") and not _looks_like_meta_crm(rel["tone"]):
        tone_bits.append(rel["tone"])
    if rel.get("style") and not _looks_like_meta_crm(rel["style"]):
        tone_bits.append(rel["style"])
    trust = _clean_fact_list(rel.get("trust_notes") or [], 4 if voice_mode else 8)
    if trust:
        tone_bits.append("；".join(trust))
    if tone_bits:
        lines.append("Delivery only (do not verbalize): " + " / ".join(tone_bits[:4]))

    active_events = []
    for event in data.get("events") or []:
        temporal = event_temporal_state(event, now)
        if event.get("status") in ("done", "dropped") and temporal.startswith("past"):
            continue
        if temporal == "stale" and voice_mode:
            continue
        if _looks_like_chitchat_topic(event.get("title") or ""):
            continue
        if _looks_like_meta_crm(event.get("title") or "") or _looks_like_meta_crm(
            event.get("notes") or ""
        ):
            continue
        when = event.get("when_text") or event.get("when_at") or "undated"
        active_events.append(
            "- %s (%s, %s)%s"
            % (
                event.get("title"),
                event.get("status"),
                when,
                (" — " + event["notes"]) if event.get("notes") else "",
            )
        )
        if voice_mode and len(active_events) >= 4:
            break
        if not voice_mode and len(active_events) >= 10:
            break
    if active_events:
        lines.append("Open commitments (mention only if asked or clearly relevant):")
        lines.extend(active_events)

    if len(lines) <= 3 and not facts and not tone_bits and not active_events:
        return ""
    text = "\n".join(lines)
    if voice_mode and len(text) > 1100:
        text = text[:950] + "\n…"
    return text


def _uniq_extend(base: List[str], extra: List[str], limit: int) -> List[str]:
    out = list(base)
    seen = set(out)
    for item in extra or []:
        text = str(item or "").strip()[:120]
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out[-limit:]


def apply_patch(dossier: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    data = normalize_dossier(dossier)
    if not isinstance(patch, dict):
        return data

    user_patch = patch.get("user_profile_patch") or {}
    if isinstance(user_patch, dict):
        user = data["user_profile"]
        if user_patch.get("name"):
            user["name"] = _as_str(user_patch.get("name"), 40)
        user["aliases"] = _uniq_extend(
            user["aliases"], user_patch.get("add_aliases") or [], 6
        )
        user["preferences"] = _uniq_extend(
            user["preferences"], user_patch.get("add_preferences") or [], MAX_LIST
        )
        user["dislikes"] = _uniq_extend(
            user["dislikes"], user_patch.get("add_dislikes") or [], MAX_LIST
        )
        user["habits"] = _uniq_extend(
            user["habits"], user_patch.get("add_habits") or [], MAX_LIST
        )
        user["notes"] = _uniq_extend(
            user["notes"], user_patch.get("add_notes") or [], MAX_NOTES
        )
        set_context = user_patch.get("set_context") or {}
        if isinstance(set_context, dict):
            for key, value in set_context.items():
                k = str(key or "").strip()[:40]
                v = str(value or "").strip()[:120]
                if k and v:
                    user["context"][k] = v
            # keep context bounded
            if len(user["context"]) > 12:
                user["context"] = dict(list(user["context"].items())[-12:])

    rel_patch = patch.get("relationship_patch") or {}
    if isinstance(rel_patch, dict):
        rel = data["relationship"]
        if rel_patch.get("tone"):
            rel["tone"] = _as_str(rel_patch.get("tone"), 40)
        if rel_patch.get("style"):
            rel["style"] = _as_str(rel_patch.get("style"), 80)
        rel["trust_notes"] = _uniq_extend(
            rel["trust_notes"], rel_patch.get("add_trust_notes") or [], 8
        )
        rel["ongoing_topics"] = _uniq_extend(
            rel["ongoing_topics"], rel_patch.get("add_ongoing_topics") or [], 8
        )

    events_patch = patch.get("events_patch") or {}
    if isinstance(events_patch, dict):
        by_id = {e["id"]: dict(e) for e in data["events"]}
        for event_id in events_patch.get("close_ids") or []:
            eid = str(event_id or "").strip()
            if eid in by_id:
                by_id[eid]["status"] = "done"
                by_id[eid]["last_mentioned_at"] = _now_iso()
        for event_id in events_patch.get("drop_ids") or []:
            eid = str(event_id or "").strip()
            if eid in by_id:
                by_id[eid]["status"] = "dropped"
                by_id[eid]["last_mentioned_at"] = _now_iso()
        for item in events_patch.get("upsert") or []:
            if not isinstance(item, dict):
                continue
            title = _as_str(item.get("title"), 80)
            if not title or _looks_like_chitchat_topic(title):
                continue
            eid = _as_str(item.get("id"), 40)
            matched = None
            if eid and eid in by_id:
                matched = by_id[eid]
            else:
                for existing in by_id.values():
                    if existing.get("title") == title:
                        matched = existing
                        break
            if matched is None:
                eid = eid or uuid.uuid4().hex[:10]
                matched = {
                    "id": eid,
                    "title": title,
                    "status": "open",
                    "when_text": "",
                    "when_at": "",
                    "last_mentioned_at": _now_iso(),
                    "notes": "",
                }
                by_id[eid] = matched
            matched["title"] = title
            if item.get("status") in ("open", "waiting", "done", "dropped"):
                matched["status"] = item["status"]
            if item.get("when_text") is not None:
                matched["when_text"] = _as_str(item.get("when_text"), 80)
            if item.get("when_at") is not None:
                matched["when_at"] = _as_str(item.get("when_at"), 40)
            if item.get("notes") is not None:
                matched["notes"] = _as_str(item.get("notes"), 160)
            matched["last_mentioned_at"] = _now_iso()

        # Prefer active events, keep newest
        ordered = sorted(
            by_id.values(),
            key=lambda e: (
                0 if e.get("status") in ("open", "waiting") else 1,
                e.get("last_mentioned_at") or "",
            ),
            reverse=False,
        )
        # stable: open/waiting first by recency
        active = [e for e in by_id.values() if e.get("status") in ("open", "waiting")]
        closed = [e for e in by_id.values() if e.get("status") not in ("open", "waiting")]
        active.sort(key=lambda e: e.get("last_mentioned_at") or "", reverse=True)
        closed.sort(key=lambda e: e.get("last_mentioned_at") or "", reverse=True)
        data["events"] = (active + closed)[:MAX_EVENTS]

    data["updated_at"] = _now_iso()
    return normalize_dossier(data)


UPDATER_PROMPT = """
你是智能体运行时档案更新器。根据「当前档案」和「本轮对话」，输出 JSON patch。
目标是让助手越来越懂用户，而不是把闲聊写成待办或话题菜单。

更新原则：
1) user_profile：稳定身份、称呼、持续偏好/习惯/忌讳、长期背景。
   - 对方自报「我是X / 我叫X / 叫我X」时：写入 name（若空）或 add_aliases；
     不要把临时玩笑称呼当正式名。
   - 稳定兴趣可进 preferences（如「喜欢漫威」「关注F1」）。
   - 写成短事实，像「对超声换能器感兴趣」；禁止 CRM/分析腔
     （如「有技术咨询需求，可能涉及相关工作或项目」）。
   - 不要把「刚聊过某部电影」写成必须跟进的事项。
2) relationship：只记相处调性（更直接/更玩笑/更简短）和少量信任备注。
   - add_ongoing_topics 尽量留空；禁止把娱乐闲聊写成要口头复述的话题清单。
3) events：仅真实要推进的事——约定、截止、项目、承诺、提醒（能 work on / 要兑现）。
   - 禁止把闲聊主题（电影、吐槽、随口八卦、刚聊的蜘蛛侠/F1）写成事件。
   - 填写 when_text；能推断绝对时间则填 when_at（ISO8601，+08:00）。
   - 已完成→close_ids；明确取消→drop_ids；新建或推进→upsert。
4) 没新信息时各 patch 用空对象/空数组，不要编造。
5) 只输出合法 JSON，不要 markdown。

输出 schema：
{
  "user_profile_patch": {
    "name": "",
    "add_aliases": [],
    "add_preferences": [],
    "add_dislikes": [],
    "add_habits": [],
    "add_notes": [],
    "set_context": {}
  },
  "relationship_patch": {
    "tone": "",
    "style": "",
    "add_trust_notes": [],
    "add_ongoing_topics": []
  },
  "events_patch": {
    "upsert": [
      {"id":"", "title":"", "status":"open|waiting|done|dropped", "when_text":"", "when_at":"", "notes":""}
    ],
    "close_ids": [],
    "drop_ids": []
  }
}
"""


_DOSSIER_CANDIDATE_RE = re.compile(
    r"(记住|别忘了|要记得|叫我|我的名字|"
    r"我是(?!觉得|想|说|问|在想)|"
    r"我喜欢|我不喜欢|我讨厌|我习惯|我通常|"
    r"我住在|我来自|我的(?:工作|职业|生日|家人|孩子|宠物|目标|计划)|"
    r"长期|以后都|每周|每天|经常|总是|"
    r"下周|明天|后天|今晚|周末|提醒|约定|计划|要做|得去|开会|截止|deadline|"
    r"帮我跟进|别再|以后别|改叫|你可以更|说话太|太啰嗦|简短点)"
)


def should_update_dossier(user_text: str, assistant_text: str = "") -> bool:
    blob = "%s\n%s" % (user_text or "", assistant_text or "")
    if _DOSSIER_CANDIDATE_RE.search(blob):
        return True
    # Also refresh when there are open events (time-state / completion cues)
    return False


def should_update_dossier_with_state(
    user_text: str,
    assistant_text: str,
    dossier: Dict[str, Any],
) -> bool:
    if should_update_dossier(user_text, assistant_text):
        return True
    data = normalize_dossier(dossier)
    return any(e.get("status") in ("open", "waiting") for e in data.get("events") or [])


def parse_updater_response(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("```"):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        raw = match.group(0) if match else ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except (TypeError, ValueError):
            return {}
    return data if isinstance(data, dict) else {}


def dossier_has_content(dossier: Dict[str, Any]) -> bool:
    data = normalize_dossier(dossier)
    user = data["user_profile"]
    rel = data["relationship"]
    if user.get("name") or user.get("aliases") or user.get("preferences"):
        return True
    if user.get("dislikes") or user.get("habits") or user.get("notes") or user.get("context"):
        return True
    if rel.get("tone") or rel.get("style") or rel.get("trust_notes") or rel.get("ongoing_topics"):
        return True
    if data.get("events"):
        return True
    return False
