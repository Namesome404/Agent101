from ..base import MemoryProviderBase, logger
import time
import json
import os
import re
import yaml
from config.config_loader import get_project_dir
try:
    from config.manage_api_client import (
        generate_and_save_chat_summary,
        save_agent_memory,
        append_agent_memory_items,
    )
except ImportError:
    # 兼容未热重载的旧进程 / 缺符号时仍可加载记忆模块
    async def generate_and_save_chat_summary(session_id: str):
        return None

    async def save_agent_memory(mac_address: str, summary_memory: str):
        return None

    async def append_agent_memory_items(mac_address: str, texts, source: str = "explicit"):
        return None
import asyncio
from core.utils.util import check_model_key


_EXPLICIT_MEMORY_PATTERNS = [
    (re.compile(r"^记住[：:，,\s]*(.+)$"), lambda m: m.group(1).strip()),
    (re.compile(r"^别忘了[：:，,\s]*(.+)$"), lambda m: m.group(1).strip()),
    (re.compile(r"^要记得[：:，,\s]*(.+)$"), lambda m: m.group(1).strip()),
    (re.compile(r"^叫我[：:，,\s]*(.+)$"), lambda m: "用户叫" + m.group(1).strip()),
    (re.compile(r"^我的名字[是叫]?[：:，,\s]*(.+)$"), lambda m: "用户叫" + m.group(1).strip()),
]

# 即时记忆条目前缀：把「我…」规范为关于用户的第三人称描述
_MEMORY_PREFIX_RULES = [
    (re.compile(r"^我喜欢(.+)$"), "用户喜欢"),
    (re.compile(r"^我不喜欢(.+)$"), "用户不喜欢"),
    (re.compile(r"^我是(.+)$"), "用户是"),
    (re.compile(r"^我叫(.+)$"), "用户叫"),
    (re.compile(r"^我的名字[是叫]?(.+)$"), "用户叫"),
    (re.compile(r"^我住在(.+)$"), "用户住在"),
    (re.compile(r"^我在(.+)$"), "用户在"),
    (re.compile(r"^我有(.+)$"), "用户有"),
    (re.compile(r"^我想(.+)$"), "用户想"),
    (re.compile(r"^我需要(.+)$"), "用户需要"),
    (re.compile(r"^我的(.+)$"), "用户的"),
    (re.compile(r"^我(.+)$"), "用户"),
]


def normalize_explicit_memory(text: str) -> str:
    """把用户第一人称原话规范为「用户…」第三人称记忆条目。"""
    t = (text or "").strip().strip("。．.!！?？ ")
    if not t or t.startswith("用户"):
        return t
    for pattern, prefix in _MEMORY_PREFIX_RULES:
        m = pattern.match(t)
        if not m:
            continue
        rest = (m.group(1) or "").strip()
        if prefix.endswith("的") and rest:
            return prefix + rest
        if prefix == "用户" and rest:
            return prefix + rest
        if rest:
            return prefix + rest
        return prefix
    return t


def extract_explicit_memory_items(text: str):
    """从用户原话提取可直接落库的记忆条目。"""
    if not text:
        return []
    raw = str(text).strip()
    # 去掉 ASR 情绪/语言 JSON 包装
    try:
        if raw.startswith("{") and raw.endswith("}"):
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("content"):
                raw = str(data["content"]).strip()
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    items = []
    for pattern, fmt in _EXPLICIT_MEMORY_PATTERNS:
        m = pattern.match(raw)
        if not m:
            continue
        line = normalize_explicit_memory((fmt(m) or "").strip())
        if line and line not in items:
            items.append(line)
    return items


short_term_memory_prompt = """
你是长期记忆筛选器。根据本次对话和历史记忆，只保留未来多次对话仍然有用的信息。

可以记：
1. 用户明确、稳定的身份信息，例如称呼、职业、长期居住城市。
2. 持续性的偏好、厌恶、习惯、沟通方式和无障碍需求。
3. 重要关系、宠物、长期项目、长期目标或反复出现的需求。
4. 尚未完成且未来确实需要继续跟进的承诺、计划或期限。

不要记：
1. 问候、闲聊、玩笑、故事内容、一次性问题和助手的回答或建议。
2. 当前时间、天气、新闻、临时情绪、临时位置、正在做什么等短期状态。
3. “用户问了什么”“用户说过什么”这类对话流水账。
4. 根据语气猜测出来的信息，或用户没有明确表达的结论。
5. 密码、密钥、身份证号、银行卡号、精确住址等敏感信息。

规则：
- 历史记忆仍有效则保留；新信息与旧信息冲突时只保留较新的事实。
- 普通偏好至少要表达明确，含糊或随口一说时不要记。
- 每条必须以“用户”开头，简洁独立，不超过60个汉字。
- 最多输出8条；没有值得长期保存的信息就输出空数组。
- 只输出合法JSON，不要解释，不要Markdown。

格式：
{"version":1,"items":[{"text":"用户喜欢简洁直接的回答","source":"auto"}]}
"""


def extract_json_data(json_code):
    start = json_code.find("```json")
    # 从start开始找到下一个```结束
    end = json_code.find("```", start + 1)
    # print("start:", start, "end:", end)
    if start == -1 or end == -1:
        try:
            jsonData = json.loads(json_code)
            return json_code
        except Exception as e:
            print("Error:", e)
        return ""
    jsonData = json_code[start + 7 : end]
    return jsonData


TAG = __name__


class MemoryProvider(MemoryProviderBase):
    def __init__(self, config, summary_memory):
        super().__init__(config)
        self.short_memory = ""
        self.save_to_file = True
        self.memory_path = get_project_dir() + "data/.memory.yaml"
        self.load_memory(summary_memory)

    def init_memory(
        self, role_id, llm, summary_memory=None, save_to_file=True, **kwargs
    ):
        super().init_memory(role_id, llm, **kwargs)
        self.save_to_file = save_to_file
        self.load_memory(summary_memory)

    def load_memory(self, summary_memory):
        # api获取到总结记忆后直接返回
        if summary_memory or not self.save_to_file:
            self.short_memory = summary_memory
            return

        all_memory = {}
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                all_memory = yaml.safe_load(f) or {}
        if self.role_id in all_memory:
            self.short_memory = all_memory[self.role_id]

    def _append_short_memory_lines(self, lines):
        for line in lines or []:
            t = (line or "").strip().lstrip("-•* ").strip()
            if not t:
                continue
            bullet = "- " + t
            if bullet in (self.short_memory or "") or t in (self.short_memory or ""):
                continue
            if self.short_memory:
                self.short_memory = self.short_memory.rstrip() + "\n" + bullet
            else:
                self.short_memory = bullet

    async def remember_explicit(self, user_text: str) -> bool:
        """用户明确说「记住…」时即时写入管理端，并更新本会话可读记忆。"""
        items = extract_explicit_memory_items(user_text)
        if not items:
            return False
        self._append_short_memory_lines(items)
        if self.save_to_file:
            self.save_memory_to_file()
            logger.bind(tag=TAG).info(
                f"Explicit memory saved locally - Role: {self.role_id}, items: {items}"
            )
            return True
        saved = await append_agent_memory_items(self.role_id, items, source="explicit")
        if saved:
            logger.bind(tag=TAG).info(
                f"Explicit memory saved to API - Role: {self.role_id}, items: {items}"
            )
            return True
        logger.bind(tag=TAG).warning(
            f"Explicit memory save failed - Role: {self.role_id}, items: {items}"
        )
        return False

    def save_memory_to_file(self):
        all_memory = {}
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                all_memory = yaml.safe_load(f) or {}
        all_memory[self.role_id] = self.short_memory
        with open(self.memory_path, "w", encoding="utf-8") as f:
            yaml.dump(all_memory, f, allow_unicode=True)

    async def save_memory(self, msgs, session_id=None):
        # 打印使用的模型信息
        model_info = getattr(self.llm, "model_name", str(self.llm.__class__.__name__))
        logger.bind(tag=TAG).debug(f"使用记忆保存模型: {model_info}")
        api_key = getattr(self.llm, "api_key", None)
        memory_key_msg = check_model_key("记忆总结专用LLM", api_key)
        if memory_key_msg:
            logger.bind(tag=TAG).error(memory_key_msg)
        if self.llm is None:
            logger.bind(tag=TAG).error("LLM is not set for memory provider")
            return None

        if len(msgs) < 2:
            return None

        msgStr = ""
        for msg in msgs:
            content = msg.content

            # Extract content from JSON format if present (for ASR with emotion/language tags)
            try:
                if content and content.strip().startswith("{") and content.strip().endswith("}"):
                    data = json.loads(content)
                    if "content" in data:
                        content = data["content"]
            except (json.JSONDecodeError, KeyError, TypeError):
                # If parsing fails, use original content
                pass

            if msg.role == "user":
                msgStr += f"User: {content}\n"
            elif msg.role == "assistant":
                msgStr += f"Assistant: {content}\n"
        if self.short_memory and len(self.short_memory) > 0:
            msgStr += "历史记忆：\n"
            msgStr += self.short_memory

        # 当前时间
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        msgStr += f"当前时间：{time_str}"

        try:
            result = self.llm.response_no_stream(
                short_term_memory_prompt,
                msgStr,
                max_tokens=600,
                temperature=0.2,
            )
            json_str = extract_json_data(result)
            json.loads(json_str)  # 检查json格式是否正确
            self.short_memory = json_str
            saved_ok = False
            if self.save_to_file:
                self.save_memory_to_file()
                saved_ok = True
            else:
                # API 模式：本地总结后写回管理端；失败时再走会话总结兜底
                saved = await save_agent_memory(self.role_id, json_str)
                saved_ok = saved is not None
                if not saved_ok:
                    summary_id = session_id if session_id else self.role_id
                    await generate_and_save_chat_summary(summary_id)
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in saving memory: {e}")
            saved_ok = False
            if not self.save_to_file:
                summary_id = session_id if session_id else self.role_id
                try:
                    await generate_and_save_chat_summary(summary_id)
                except Exception as api_err:
                    logger.bind(tag=TAG).error(f"Fallback chat-summary failed: {api_err}")
        if saved_ok:
            logger.bind(tag=TAG).info(
                f"Save memory successful - Role: {self.role_id}, Session: {session_id}"
            )
        else:
            logger.bind(tag=TAG).warning(
                f"Save memory failed or skipped - Role: {self.role_id}, Session: {session_id}"
            )

        return self.short_memory

    async def query_memory(self, query: str) -> str:
        return self.short_memory
