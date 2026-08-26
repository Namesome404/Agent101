import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
EMOJI_MAP = {
    "😂": "funny",
    "😭": "crying",
    "😠": "angry",
    "😔": "sad",
    "😍": "loving",
    "😲": "surprised",
    "😱": "shocked",
    "🤔": "thinking",
    "😌": "relaxed",
    "😴": "sleepy",
    "😜": "silly",
    "🙄": "confused",
    "😶": "neutral",
    "🙂": "happy",
    "😆": "laughing",
    "😳": "embarrassed",
    "😉": "winking",
    "😎": "cool",
    "🤤": "delicious",
    "😘": "kissy",
    "😏": "confident",
}
EMOJI_RANGES = [
    (0x1F600, 0x1F64F),
    (0x1F300, 0x1F5FF),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
]


def fix_asr_stutter(text: str) -> str:
    """修正 ASR 逐字/逐词重复（如「打打开开」「天气天气」）。"""
    if not text or len(text) < 2:
        return text

    def _collapse_runs(s: str, unit_len: int) -> str:
        if len(s) < unit_len * 2:
            return s
        out = []
        i = 0
        while i < len(s):
            unit = s[i : i + unit_len]
            if len(unit) < unit_len:
                out.append(unit)
                i += 1
                continue
            out.append(unit)
            i += unit_len
            while i + unit_len <= len(s) and s[i : i + unit_len] == unit:
                i += unit_len
        return "".join(out)

    def _fix_plain(s: str) -> str:
        fixed = s
        for n in (1, 2, 3):
            fixed = _collapse_runs(fixed, n)
        return fixed

    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) and "content" in data:
                content = data.get("content") or ""
                collapsed = _fix_plain(content)
                if collapsed != content:
                    data = dict(data)
                    data["content"] = collapsed
                    return json.dumps(data, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass

    collapsed = _fix_plain(text)
    if collapsed == text:
        return text
    double_runs = sum(1 for i in range(len(text) - 1) if text[i] == text[i + 1])
    if double_runs >= 1 or len(collapsed) <= int(len(text) * 0.85):
        return collapsed
    return text


def is_likely_tts_echo(asr_text: str, recent_tts_texts) -> bool:
    """判断 ASR 结果是否像是刚播过的 TTS 被麦克风拾到。"""
    if not asr_text or not recent_tts_texts:
        return False
    plain = get_string_no_punctuation_or_emoji(asr_text).lower().replace(" ", "")
    if len(plain) < 2:
        return False
    for tts in recent_tts_texts[-6:]:
        ref = get_string_no_punctuation_or_emoji(tts or "").lower().replace(" ", "")
        if not ref:
            continue
        if plain == ref:
            return True
        if len(plain) >= 4 and (plain in ref or ref in plain):
            return True
        # 短句：字符重叠率高
        if len(plain) <= 20 and len(ref) <= 80:
            overlap = sum(1 for c in set(plain) if c in ref)
            if overlap / max(len(set(plain)), 1) >= 0.85 and len(plain) >= 3:
                return True
    return False


def get_string_no_punctuation_or_emoji(s):
    """去除字符串首尾的空格、标点符号和表情符号"""
    chars = list(s)
    # 处理开头的字符
    start = 0
    while start < len(chars) and is_punctuation_or_emoji(chars[start]):
        start += 1
    # 处理结尾的字符
    end = len(chars) - 1
    while end >= start and is_punctuation_or_emoji(chars[end]):
        end -= 1
    return "".join(chars[start : end + 1])


def is_punctuation_or_emoji(char):
    """检查字符是否为空格、指定标点或表情符号"""
    # 定义需要去除的中英文标点（包括全角/半角）
    punctuation_set = {
        "，",
        ",",  # 中文逗号 + 英文逗号
        "。",
        ".",  # 中文句号 + 英文句号
        "！",
        "!",  # 中文感叹号 + 英文感叹号
        "“",
        "”",
        '"',  # 中文双引号 + 英文引号
        "：",
        ":",  # 中文冒号 + 英文冒号
        "-",
        "－",  # 英文连字符 + 中文全角横线
        "、",  # 中文顿号
        "[",
        "]",  # 方括号
        "【",
        "】",  # 中文方括号
    }
    if char.isspace() or char in punctuation_set:
        return True
    return is_emoji(char)


async def get_emotion(conn: "ConnectionHandler", text):
    """获取文本内的情绪消息"""
    emoji = "🙂"
    emotion = "happy"
    for char in text:
        if char in EMOJI_MAP:
            emoji = char
            emotion = EMOJI_MAP[char]
            break
    try:
        await conn.websocket.send(
            json.dumps(
                {
                    "type": "llm",
                    "text": emoji,
                    "emotion": emotion,
                    "session_id": conn.session_id,
                }
            )
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"发送情绪表情失败，错误:{e}")
    return


def is_emoji(char):
    """检查字符是否为emoji表情"""
    code_point = ord(char)
    return any(start <= code_point <= end for start, end in EMOJI_RANGES)


def check_emoji(text):
    """去除文本中的所有emoji表情"""
    return "".join(char for char in text if not is_emoji(char) and char != "\n")
