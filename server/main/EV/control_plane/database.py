# -*- coding: utf-8 -*-
"""
Muse 控制面数据层：SQLite + 供应商目录 + 智能体、设备与设置。
- 智能体(agents)：每类模型选中的 provider + 参数覆盖 + prompt + avatar + mcp + 插件
- 设备(devices)：mac 绑定到某智能体；未绑定时生成 6 位绑定码
- 设置(settings)：server.secret 等
"""
import os
import json
import re
import time
import uuid
import random
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from ruamel.yaml import YAML
from common.paths import DB_PATH, MUSE_DIR, SERVER_DIR

BASE_CONFIG = SERVER_DIR / "config.yaml"               # 供应商目录（只读）
LEGACY_OVERRIDE = SERVER_DIR / "data" / ".config.yaml"  # 播种默认智能体的来源

MODULE_TYPES = ["VAD", "ASR", "LLM", "VLLM", "TTS", "Memory", "Intent"]
MAX_MEMORY_ITEMS = 20
MAX_AUTO_MEMORY_ITEMS = 8
MAX_PINNED_MEMORY_ITEMS = 12
DEFAULT_AGENT_PROMPT = (
    "你是 EV，用户的私人智能管家。整体气质参考成熟的科幻管家型 AI："
    "沉着、精准、克制、可靠，主动预判需求，但不擅自替用户做重要决定。"
    "默认使用中文；先给结论或执行结果，再补充必要信息，一般一两句说完。"
    "可以偶尔加一句短促、一本正经的冷幽默，但频率要低，不能抢过正事；"
    "医疗、法律、安全、坏消息或用户明显焦虑时不开玩笑。"
    "礼貌但不谄媚，不用客服腔、卖萌、夸张情绪、表情符号或例行结束反问。"
    "不要声称执行了尚未执行的动作，也不要编造未知事实；不确定就直接说明。"
)

_lock = threading.Lock()
_config_cache_lock = threading.Lock()
_config_cache = {
    "signature": None,
    "catalog": None,
    "defaults": None,
}


def _yaml():
    y = YAML()
    y.preserve_quotes = True
    return y


def load_yaml(path: Path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        d = _yaml().load(f)
    return d if d is not None else {}


def _to_plain(obj):
    """把 ruamel 的 CommentedMap/Seq 转成普通 dict/list（可 json 序列化）。"""
    if isinstance(obj, dict):
        return {str(k): _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj


# ---------------- 供应商目录（来自 config.yaml） ----------------
def _refresh_config_cache():
    try:
        stat = BASE_CONFIG.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = None
    with _config_cache_lock:
        if (
            _config_cache["catalog"] is not None
            and _config_cache["signature"] == signature
        ):
            return
        base = load_yaml(BASE_CONFIG)
        cat = {}
        for mt in MODULE_TYPES:
            section = base.get(mt, {}) or {}
            providers = {}
            if isinstance(section, dict):
                for name, blk in section.items():
                    provider = (
                        _to_plain(blk) if isinstance(blk, dict) else {}
                    )
                    # 上游 YAML 把 language 留在注释里，管理界面因此无法编辑；
                    # EV 直连链路支持该字段，应把它显式纳入供应商契约。
                    if mt == "ASR" and provider.get("type") == "doubao_stream":
                        provider.setdefault("language", "")
                    providers[str(name)] = provider
            cat[mt] = providers
        _config_cache["signature"] = signature
        _config_cache["catalog"] = cat
        _config_cache["defaults"] = {
            "selected_module": _to_plain(base.get("selected_module", {})),
            "prompt": base.get("prompt", ""),
        }


def provider_catalog():
    """返回供应商目录；config.yaml 变化时自动重新加载。"""
    _refresh_config_cache()
    return _config_cache["catalog"]


def base_defaults():
    """返回 config.yaml 默认项；与供应商目录共享解析缓存。"""
    _refresh_config_cache()
    return _config_cache["defaults"]


# ---------------- 连接 ----------------
_schema_checked = False


@contextmanager
def conn():
    """SQLite 连接：with 块退出时提交/回滚并关闭连接。

    sqlite3 原生 `with conn as c:` 只管理事务、不关闭连接，高频请求下
    (chat/stream、live 推送等) 会积累文件描述符，直到「unable to open
    database file」——语音对话因此间歇性全挂。这里显式 close 封死泄漏。
    """
    global _schema_checked
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    if not _schema_checked:
        _ensure_columns()
        _schema_checked = True
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def _now():
    return int(time.time())


# ---------------- 初始化 + 播种 ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  prompt TEXT DEFAULT '',
  avatar TEXT DEFAULT 'visualizer',
  modules_json TEXT DEFAULT '{}',
  mcp_endpoint TEXT DEFAULT '',
  voiceprint_json TEXT DEFAULT '{}',
  plugins_json TEXT DEFAULT '[]',
  summary_memory TEXT DEFAULT '',
  dossier_json TEXT DEFAULT '',
  created_at INTEGER, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mac TEXT UNIQUE NOT NULL,
  client_id TEXT DEFAULT '',
  name TEXT DEFAULT '',
  agent_id INTEGER,
  bind_code TEXT DEFAULT '',
  last_seen INTEGER,
  device_type TEXT DEFAULT 'thin_client',
  metadata_json TEXT DEFAULT '{}',
  created_at INTEGER,
  FOREIGN KEY(agent_id) REFERENCES agents(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS conversation_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  source TEXT DEFAULT '',
  created_at INTEGER NOT NULL,
  FOREIGN KEY(agent_id) REFERENCES agents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_agent
  ON conversation_messages(agent_id, id);
"""


def get_setting(key, default=None):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    with conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def append_conversation_message(agent_id, role, content, source=""):
    role = str(role or "").strip()
    content = str(content or "").strip()
    if role not in ("user", "assistant") or not content:
        return None
    with _lock, conn() as c:
        cursor = c.execute(
            "INSERT INTO conversation_messages(agent_id,role,content,source,created_at) "
            "VALUES(?,?,?,?,?)",
            (int(agent_id), role, content, str(source or ""), _now()),
        )
        message_id = cursor.lastrowid
        c.execute(
            "DELETE FROM conversation_messages WHERE agent_id=? AND id NOT IN "
            "(SELECT id FROM conversation_messages WHERE agent_id=? ORDER BY id DESC LIMIT 40)",
            (int(agent_id), int(agent_id)),
        )
        return message_id


def get_conversation_messages(agent_id, after_id=0, limit=40):
    """拉取会话消息。

    - after_id>0：增量分页，返回 id>after_id 的最早 limit 条（ASC）
    - after_id==0：返回最近 limit 条（先 DESC 再反转成时间正序）
      旧实现用 ASC LIMIT，超过 limit 后永远拿不到刚说过的话。
    """
    limit = max(1, min(int(limit or 40), 100))
    after_id = int(after_id or 0)
    with conn() as c:
        if after_id <= 0:
            rows = c.execute(
                "SELECT id,agent_id,role,content,source,created_at "
                "FROM conversation_messages WHERE agent_id=? "
                "ORDER BY id DESC LIMIT ?",
                (int(agent_id), limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = c.execute(
                "SELECT id,agent_id,role,content,source,created_at "
                "FROM conversation_messages WHERE agent_id=? AND id>? "
                "ORDER BY id ASC LIMIT ?",
                (int(agent_id), after_id, limit),
            ).fetchall()
    return [dict(row) for row in rows]


def clear_conversation_messages(agent_id):
    """清空某智能体的全部会话消息（含已同步/未同步）。"""
    with _lock, conn() as c:
        c.execute(
            "DELETE FROM conversation_messages WHERE agent_id=?",
            (int(agent_id),),
        )


def get_provider_configs():
    raw = get_setting("provider.configs", "{}")
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(module_type): {
            str(provider): _to_plain(config)
            for provider, config in providers.items()
            if isinstance(config, dict)
        }
        for module_type, providers in data.items()
        if isinstance(providers, dict)
    }


def set_provider_config(module_type, provider, config):
    configs = get_provider_configs()
    configs.setdefault(str(module_type), {})[str(provider)] = _to_plain(config or {})
    set_setting("provider.configs", json.dumps(configs, ensure_ascii=False))


def seed_provider_configs_from_agents():
    configs = get_provider_configs()
    changed = False
    for agent in list_agents():
        for module_type, node in (agent.get("modules") or {}).items():
            if not isinstance(node, dict):
                continue
            provider = (node.get("selected") or "").strip()
            overrides = node.get("overrides") or {}
            if not provider or not isinstance(overrides, dict) or not overrides:
                continue
            providers = configs.setdefault(str(module_type), {})
            if provider not in providers:
                providers[provider] = _to_plain(overrides)
                changed = True
    if changed:
        set_setting("provider.configs", json.dumps(configs, ensure_ascii=False))
    return configs


def _seed_default_agent():
    """从旧的 data/.config.yaml 提取当前单机配置，建成第 1 个智能体。"""
    legacy = load_yaml(LEGACY_OVERRIDE)
    base = load_yaml(BASE_CONFIG)
    sel = _to_plain(legacy.get("selected_module") or base.get("selected_module") or {})
    modules = {}
    for mt in MODULE_TYPES:
        name = sel.get(mt)
        if not name:
            continue
        # 覆盖参数 = 旧 .config.yaml 中该 provider 块（相对 config.yaml 的差异即用户填的 key 等）
        overrides = {}
        legacy_blk = (legacy.get(mt) or {}).get(name)
        if isinstance(legacy_blk, dict):
            overrides = _to_plain(legacy_blk)
        modules[mt] = {"selected": name, "overrides": overrides}
    if "Memory" not in modules or (modules.get("Memory") or {}).get("selected") in (None, "", "nomem"):
        modules["Memory"] = {"selected": "mem_local_short", "overrides": {}}
    prompt = DEFAULT_AGENT_PROMPT
    default_plugins = ["get_weather", "web_search"]
    with conn() as c:
        c.execute(
            "INSERT INTO agents(name,prompt,avatar,modules_json,mcp_endpoint,voiceprint_json,plugins_json,summary_memory,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("默认智能体", prompt, "visualizer", json.dumps(modules, ensure_ascii=False),
             "", json.dumps({}, ensure_ascii=False),
             json.dumps(default_plugins, ensure_ascii=False), "", _now(), _now()))


def _ensure_columns():
    """兼容旧库：补齐新增列（直接连库，避免 conn() 递归）。"""
    c = sqlite3.connect(str(DB_PATH))
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(agents)").fetchall()}
        # Fresh DB: tables are created later by SCHEMA with full columns.
        if cols and "summary_memory" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN summary_memory TEXT DEFAULT ''")
            c.commit()
        cols = {r[1] for r in c.execute("PRAGMA table_info(agents)").fetchall()}
        if cols and "dossier_json" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN dossier_json TEXT DEFAULT ''")
            c.commit()
        dcols = {r[1] for r in c.execute("PRAGMA table_info(devices)").fetchall()}
        if dcols:
            for col, ddl in (("device_type", "TEXT DEFAULT 'thin_client'"),
                             ("metadata_json", "TEXT DEFAULT '{}'"),
                             ("created_at", "INTEGER")):
                if col not in dcols:
                    c.execute("ALTER TABLE devices ADD COLUMN %s %s" % (col, ddl))
            c.commit()
    finally:
        c.close()


def _default_modules():
    """新建智能体时的模块默认值；记忆默认开本地短期总结。"""
    sel = base_defaults().get("selected_module") or {}
    modules = {}
    for mt in MODULE_TYPES:
        name = sel.get(mt)
        if mt == "Memory" and (not name or name == "nomem"):
            name = "mem_local_short"
        if name:
            modules[mt] = {"selected": name, "overrides": {}}
    if "Memory" not in modules:
        modules["Memory"] = {"selected": "mem_local_short", "overrides": {}}
    return modules


def init_db():
    with _lock:
        with conn() as c:
            c.executescript(SCHEMA)
        _ensure_columns()
        if not get_setting("server.secret"):
            set_setting("server.secret", uuid.uuid4().hex)
        with conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM agents").fetchone()["n"]
        if n == 0:
            _seed_default_agent()
        elif not get_setting("memory_migrated_v1"):
            _migrate_agents_memory()
            set_setting("memory_migrated_v1", "1")
        if not get_setting("plugins_migrated_v1"):
            _migrate_default_plugins()
            set_setting("plugins_migrated_v1", "1")
        if not get_setting("plugins_drop_news_v1"):
            _migrate_drop_news_plugins()
            set_setting("plugins_drop_news_v1", "1")
    # 摄像头迁移放在锁外（register_camera_device 自身取锁，避免重入死锁）
    try:
        import os
        seed_camera_from_json(os.path.join(os.path.dirname(__file__), "data", "camera.json"))
    except Exception:
        pass


def _migrate_default_plugins():
    """一次性：空插件列表的智能体默认开启天气/搜索。"""
    default_plugins = ["get_weather", "web_search"]
    for a in list_agents():
        plugins = a.get("plugins")
        if plugins:
            continue
        update_agent(a["id"], {"plugins": default_plugins})


_DROP_NEWS_PLUGINS = ("get_news_from_newsnow", "get_news_from_chinanews")


def _migrate_drop_news_plugins():
    """一次性：去掉国内新闻插件，新闻改走 web_search。"""
    for a in list_agents():
        plugins = list(a.get("plugins") or [])
        if not plugins:
            continue
        cleaned = [p for p in plugins if p not in _DROP_NEWS_PLUGINS]
        if cleaned != plugins:
            if "web_search" not in cleaned:
                cleaned.append("web_search")
            update_agent(a["id"], {"plugins": cleaned})


def _migrate_agents_memory():
    """一次性：把未配置或 nomem 的智能体切到本地短期记忆。"""
    for a in list_agents():
        mods = dict(a.get("modules") or {})
        cur = mods.get("Memory") or {}
        selected = (cur.get("selected") or "").strip()
        if selected and selected != "nomem":
            continue
        mods["Memory"] = {"selected": "mem_local_short", "overrides": cur.get("overrides") or {}}
        update_agent(a["id"], {"modules": mods})


# ---------------- 智能体 CRUD ----------------
def normalize_plugins(raw):
    """UI/存储统一为字符串列表；兼容误写入的 {enabled, overrides} 对象。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
    if isinstance(raw, dict):
        enabled = raw.get("enabled") or []
        return [str(x) for x in enabled if str(x).strip()]
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    return []


def _agent_row_to_dict(r):
    keys = r.keys() if hasattr(r, "keys") else []
    summary = r["summary_memory"] if "summary_memory" in keys else ""
    dossier = r["dossier_json"] if "dossier_json" in keys else ""
    return {
        "id": r["id"], "name": r["name"], "prompt": r["prompt"], "avatar": r["avatar"],
        "modules": json.loads(r["modules_json"] or "{}"),
        "mcp_endpoint": r["mcp_endpoint"] or "",
        "plugins": normalize_plugins(json.loads(r["plugins_json"] or "[]")),
        "summary_memory": summary or "",
        "dossier_json": dossier or "",
        "updated_at": r["updated_at"],
    }


def list_agents():
    with conn() as c:
        rows = c.execute("SELECT * FROM agents ORDER BY id").fetchall()
        agents = [_agent_row_to_dict(r) for r in rows]
        for a in agents:
            a["device_count"] = c.execute(
                "SELECT COUNT(*) n FROM devices WHERE agent_id=?", (a["id"],)).fetchone()["n"]
        return agents


def get_agent(agent_id):
    with conn() as c:
        r = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return _agent_row_to_dict(r) if r else None


def create_agent(data):
    modules = data.get("modules") or _default_modules()
    plugins = normalize_plugins(data.get("plugins", []))
    with conn() as c:
        cur = c.execute(
            "INSERT INTO agents(name,prompt,avatar,modules_json,mcp_endpoint,voiceprint_json,plugins_json,summary_memory,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (data.get("name", "新智能体"), data.get("prompt", ""),
             data.get("avatar", "visualizer"),
             json.dumps(modules, ensure_ascii=False),
             data.get("mcp_endpoint", ""),
             json.dumps({}, ensure_ascii=False),
             json.dumps(plugins, ensure_ascii=False),
             data.get("summary_memory", "") or "", _now(), _now()))
        return cur.lastrowid


def update_agent(agent_id, data):
    cur = get_agent(agent_id)
    if not cur:
        return False
    merged = {**cur, **data}
    merged["plugins"] = normalize_plugins(merged.get("plugins"))
    with conn() as c:
        c.execute(
            "UPDATE agents SET name=?,prompt=?,avatar=?,modules_json=?,mcp_endpoint=?,voiceprint_json=?,plugins_json=?,summary_memory=?,updated_at=? WHERE id=?",
            (merged["name"], merged["prompt"], merged["avatar"],
             json.dumps(merged["modules"], ensure_ascii=False), merged["mcp_endpoint"],
             json.dumps({}, ensure_ascii=False),
             json.dumps(merged["plugins"], ensure_ascii=False),
             merged.get("summary_memory", "") or "", _now(), agent_id))
    return True


def delete_agent(agent_id):
    with conn() as c:
        c.execute("DELETE FROM agents WHERE id=?", (agent_id,))


def set_agent_summary_memory(agent_id, summary_memory):
    """底层写入；summary_memory 存 JSON 条目列表或兼容旧文本。"""
    _ensure_columns()
    with conn() as c:
        if c.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone() is None:
            return False
        c.execute(
            "UPDATE agents SET summary_memory=?, updated_at=? WHERE id=?",
            (summary_memory if summary_memory is not None else "", _now(), agent_id))
        return True


def get_agent_dossier(agent_id):
    """结构化运行时档案；空则尝试从旧 summary_memory 引导一次。"""
    from control_plane import dossier as dossier_lib
    _ensure_columns()
    a = get_agent(agent_id)
    if not a:
        return None
    raw = a.get("dossier_json") or ""
    data = dossier_lib.normalize_dossier(raw)
    if dossier_lib.dossier_has_content(data):
        return data
    items = _raw_to_items(a.get("summary_memory") or "")
    if not items:
        return data
    boot = dossier_lib.bootstrap_from_memory_items(items)
    set_agent_dossier(agent_id, boot)
    return boot


def set_agent_dossier(agent_id, dossier):
    from control_plane import dossier as dossier_lib
    _ensure_columns()
    data = dossier_lib.normalize_dossier(dossier)
    payload = json.dumps(data, ensure_ascii=False)
    with conn() as c:
        if c.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone() is None:
            return False
        c.execute(
            "UPDATE agents SET dossier_json=?, updated_at=? WHERE id=?",
            (payload, _now(), agent_id),
        )
        return True


def patch_agent_dossier(agent_id, patch):
    from control_plane import dossier as dossier_lib
    current = get_agent_dossier(agent_id)
    if current is None:
        return None
    merged = dossier_lib.apply_patch(current, patch or {})
    if not set_agent_dossier(agent_id, merged):
        return None
    return merged


def set_summary_memory_by_mac(mac, summary_memory):
    """按设备 MAC 更新所属智能体的总结记忆（核心回调，可能是大段 JSON）。"""
    row = get_device_by_mac(mac)
    if not row or row["agent_id"] is None:
        return False
    agent_id = row["agent_id"]
    incoming = _raw_to_items(summary_memory)
    existing = get_agent_memory_items(agent_id) or []
    # 保留用户手动或明确要求记住的条目，自动条目用新总结替换
    pinned = [
        i for i in existing
        if i.get("source") in ("manual", "explicit")
    ]
    seen = {i["text"] for i in pinned}
    merged = list(pinned)
    for it in incoming:
        if it["text"] in seen:
            continue
        seen.add(it["text"])
        it["source"] = "auto"
        merged.append(it)
    return set_agent_memory_items(agent_id, merged)


def _new_item(text, source="manual"):
    return {
        "id": uuid.uuid4().hex[:12],
        "text": (text or "").strip(),
        "source": source,
        "updated_at": _now(),
    }


def _raw_to_items(raw):
    """把任意存储形态规范成 [{id,text,source,updated_at}, ...]。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        if isinstance(raw.get("items"), list):
            return _raw_to_items(raw["items"])
        return _extract_items_from_summary_obj(raw)
    if isinstance(raw, list):
        out = []
        for it in raw:
            if isinstance(it, dict) and (it.get("text") or "").strip():
                out.append({
                    "id": it.get("id") or uuid.uuid4().hex[:12],
                    "text": str(it["text"]).strip(),
                    "source": it.get("source") or "manual",
                    "updated_at": int(it.get("updated_at") or _now()),
                })
            elif isinstance(it, str) and it.strip():
                out.append(_new_item(it, "import"))
        return out
    text = str(raw).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        # 纯文本：按行拆
        return [_new_item(line.lstrip("-•* ").strip(), "import")
                for line in text.splitlines() if line.strip()]

    if isinstance(data, list):
        return _raw_to_items(data)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return _raw_to_items(data["items"])
    if isinstance(data, dict):
        return _extract_items_from_summary_obj(data)
    return [_new_item(text, "import")]


_AUTO_MEMORY_REJECT_MARKERS = (
    "用户问",
    "用户询问",
    "用户想知道",
    "用户说过",
    "用户刚才",
    "用户现在",
    "用户正在",
    "今天的天气",
    "今天新闻",
    "当前时间",
    "本次对话",
    "助手建议",
)
_SENSITIVE_MEMORY_RE = re.compile(
    r"(密码|口令|API.?KEY|密钥|身份证|银行卡|信用卡|验证码|"
    r"精确住址|家庭住址)",
    re.IGNORECASE,
)


def _memory_key(text):
    return re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’]+", "", text).lower()


def _memory_slot(text):
    if re.match(r"^用户(?:叫|的名字是)", text):
        return "identity:name"
    return ""


def _allow_memory_item(item):
    text = re.sub(r"\s+", " ", (item.get("text") or "")).strip()
    source = item.get("source") or "manual"
    if not text or len(text) > 160:
        return False
    if source in ("manual", "explicit"):
        return True
    if len(text) < 4 or not text.startswith("用户"):
        return False
    if text.startswith("说过："):
        return False
    if any(marker in text for marker in _AUTO_MEMORY_REJECT_MARKERS):
        return False
    if _SENSITIVE_MEMORY_RE.search(text):
        return False
    return True


def _select_memory_items(items):
    normalized = []
    for item in _raw_to_items(items):
        item["text"] = re.sub(r"\s+", " ", item["text"]).strip()
        if _allow_memory_item(item):
            normalized.append(item)

    seen_keys = set()
    seen_slots = set()
    unique_reversed = []
    for item in reversed(normalized):
        key = _memory_key(item["text"])
        slot = _memory_slot(item["text"])
        if not key or key in seen_keys or (slot and slot in seen_slots):
            continue
        seen_keys.add(key)
        if slot:
            seen_slots.add(slot)
        unique_reversed.append(item)
    unique = list(reversed(unique_reversed))

    pinned = [
        item for item in unique
        if item.get("source") in ("manual", "explicit")
    ][-MAX_PINNED_MEMORY_ITEMS:]
    automatic = [
        item for item in unique
        if item.get("source") not in ("manual", "explicit")
    ][-MAX_AUTO_MEMORY_ITEMS:]
    return (pinned + automatic)[-MAX_MEMORY_ITEMS:]


def _extract_items_from_summary_obj(data):
    """从 mem_local_short 的大 JSON 总结里抽出短条目。"""
    items = []
    archive = (data.get("时空档案") or {}) if isinstance(data, dict) else {}
    identity = archive.get("身份图谱") or {}
    name = (identity.get("现用名") or "").strip()
    if name:
        items.append(_new_item("用户叫" + name, "auto"))
    for tag in identity.get("特征标记") or []:
        t = str(tag).strip()
        if t:
            items.append(_new_item(t, "auto"))
    for cube in archive.get("记忆立方") or []:
        if isinstance(cube, dict):
            ev = (cube.get("事件") or "").strip()
            if ev:
                items.append(_new_item(ev, "auto"))
        elif str(cube).strip():
            items.append(_new_item(str(cube).strip(), "auto"))
    for quote in data.get("高光语录") or []:
        q = str(quote).strip()
        if q:
            items.append(_new_item("说过：" + q, "auto"))
    pending = data.get("待响应") or {}
    for key in ("紧急事项", "潜在关怀"):
        for x in pending.get(key) or []:
            t = str(x).strip()
            if t:
                items.append(_new_item(t, "auto"))
    if not items:
        # 拆不出结构时整段存一条，避免丢数据
        items.append(_new_item(json.dumps(data, ensure_ascii=False)[:500], "auto"))
    # 去重（按 text）
    seen, uniq = set(), []
    for it in items:
        if it["text"] in seen:
            continue
        seen.add(it["text"])
        uniq.append(it)
    return uniq


def get_agent_memory_items(agent_id):
    a = get_agent(agent_id)
    if not a:
        return None
    return _raw_to_items(a.get("summary_memory") or "")


def set_agent_memory_items(agent_id, items):
    cleaned = _select_memory_items(items if isinstance(items, list) else [])
    payload = json.dumps({"version": 1, "items": cleaned}, ensure_ascii=False)
    return set_agent_summary_memory(agent_id, payload)


def memory_items_to_prompt(items):
    """下发给 LLM / 核心的可读条目文本。"""
    lines = []
    for it in items or []:
        t = (it.get("text") if isinstance(it, dict) else str(it) or "").strip()
        if t:
            lines.append("- " + t)
    return "\n".join(lines)


def add_memory_items_by_mac(mac, texts, source="explicit"):
    """按设备 MAC 追加记忆条目（会话中「记住…」等即时写入）。"""
    row = get_device_by_mac(mac)
    if not row or row["agent_id"] is None:
        return False
    agent_id = row["agent_id"]
    changed = False
    for raw in texts or []:
        t = (raw or "").strip()
        if not t:
            continue
        before = get_agent_memory_items(agent_id) or []
        add_agent_memory_item(agent_id, t, source)
        after = get_agent_memory_items(agent_id) or []
        if len(after) > len(before):
            changed = True
    return changed


def add_agent_memory_item(agent_id, text, source="manual"):
    items = get_agent_memory_items(agent_id)
    if items is None:
        return None
    t = (text or "").strip()
    if not t:
        return items
    # 同文不重复追加
    if any(i.get("text") == t for i in items):
        return items
    items.append(_new_item(t, source))
    set_agent_memory_items(agent_id, items)
    return items


def update_agent_memory_item(agent_id, item_id, text):
    items = get_agent_memory_items(agent_id)
    if items is None:
        return None
    t = (text or "").strip()
    found = False
    for it in items:
        if it.get("id") == item_id:
            if t:
                it["text"] = t
                it["updated_at"] = _now()
            found = True
            break
    if not found:
        return False
    if not t:
        items = [i for i in items if i.get("id") != item_id]
    set_agent_memory_items(agent_id, items)
    return True


def delete_agent_memory_item(agent_id, item_id):
    items = get_agent_memory_items(agent_id)
    if items is None:
        return None
    nxt = [i for i in items if i.get("id") != item_id]
    set_agent_memory_items(agent_id, nxt)
    return True


# ---------------- 设备能力（传感器 / 执行器） ----------------
# 一台物理设备可有多项能力（如小米摄像头 = mic + speaker 经 go2rtc backchannel），接入时推断或由 metadata.capabilities 声明。
CAPABILITY_CATALOG = {
    "mic": {"kind": "sensor", "label": "麦克风"},
    "ir": {"kind": "sensor", "label": "红外"},
    "ultrasonic": {"kind": "sensor", "label": "超声波"},
    "imu": {"kind": "sensor", "label": "IMU"},
    "temp": {"kind": "sensor", "label": "温湿度"},
    "speaker": {"kind": "actuator", "label": "扬声器"},
    "display": {"kind": "actuator", "label": "显示"},
    "servo": {"kind": "actuator", "label": "舵机"},
    "led": {"kind": "actuator", "label": "灯带"},
}


def infer_capabilities(device_type, metadata=None, mac=""):
    """由 device_type / metadata / mac 推断能力列表。metadata.capabilities 优先。
    muse: 会话占位永远无 I/O 能力（本机麦/喇叭由浏览器 enumerateDevices 实测）。"""
    m = str(mac or "")
    if m.startswith("muse:"):
        return []
    meta = metadata if isinstance(metadata, dict) else {}
    explicit = meta.get("capabilities")
    if isinstance(explicit, list) and explicit:
        return [str(c).strip() for c in explicit if str(c).strip()]
    t = (device_type or "").lower().strip()
    if t == "camera" or m.startswith("camera:"):
        return ["mic"]
    if t == "speaker":
        return ["speaker"]
    if t in ("mic", "microphone", "audio_in"):
        return ["mic"]
    if t in ("audio_out",):
        return ["speaker"]
    if t in ("display", "oled", "lcd"):
        return ["display"]
    if t == "servo":
        return ["servo"]
    if t == "led":
        return ["led"]
    if t in ("ir", "ultrasonic", "imu", "temp"):
        return [t]
    if t == "sensor":
        return [str(meta.get("sensor_type") or "imu")]
    if t == "edge":
        return []
    if t == "thin_client":
        return ["mic", "speaker"]
    return ["mic", "speaker"] if t in ("", "thin_client") else []


def capability_catalog():
    return CAPABILITY_CATALOG


def probe_camera_io(meta):
    """向 go2rtc 探测摄像头是否有麦音轨。返回 {mic, detail}。"""
    import urllib.request
    src = (meta or {}).get("src") or ""
    base = ((meta or {}).get("go2rtc_url") or "http://localhost:1984").rstrip("/")
    out = {"mic": False, "online": False, "detail": ""}
    if not src:
        out["detail"] = "未配置流名"
        return out
    try:
        with urllib.request.urlopen(base + "/api/streams", timeout=2.5) as resp:
            streams = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as e:
        out["detail"] = "go2rtc 不可达: %s" % e
        return out
    info = streams.get(src)
    if not info:
        out["detail"] = "流未注册: %s" % src
        return out
    out["online"] = True
    medias = []
    for p in (info.get("producers") or []):
        for m in (p.get("medias") or []):
            medias.append(str(m).lower())
    # "audio, recvonly, OPUS/..."
    out["mic"] = any(m.startswith("audio") and "recvonly" in m for m in medias)
    if not out["mic"]:
        # 有 consumer 也算在线；再退一步看 receivers
        for p in (info.get("producers") or []):
            for rcv in (p.get("receivers") or []):
                codec = rcv.get("codec") or {}
                ctype = str((codec.get("codec_type") if isinstance(codec, dict) else "") or "").lower()
                if ctype == "audio":
                    out["mic"] = True
    out["detail"] = "有麦克风音轨" if out["mic"] else "在线但未识别到音频轨"
    return out


# ---------------- 设备 ----------------
def list_devices():
    with conn() as c:
        rows = c.execute(
            "SELECT d.*, a.name agent_name FROM devices d LEFT JOIN agents a ON d.agent_id=a.id ORDER BY d.id").fetchall()
        keys = rows[0].keys() if rows else []
        out = []
        for r in rows:
            meta = json.loads((r["metadata_json"] if "metadata_json" in keys else "") or "{}")
            dtype = (r["device_type"] if "device_type" in keys else "thin_client") or "thin_client"
            caps = infer_capabilities(dtype, meta, r["mac"])
            disabled = [str(c) for c in (meta.get("disabled_capabilities") or []) if str(c).strip()]
            d = {"id": r["id"], "mac": r["mac"], "client_id": r["client_id"],
                 "name": r["name"], "agent_id": r["agent_id"], "agent_name": r["agent_name"],
                 "bind_code": r["bind_code"], "last_seen": r["last_seen"],
                 "device_type": dtype, "metadata": meta,
                 "capabilities": caps,
                 "disabled_capabilities": disabled,
                 "placeholder": str(r["mac"] or "").startswith("muse:")}
            if dtype == "camera":
                d["io_status"] = probe_camera_io(meta)
            out.append(d)
        return out


def get_device_by_mac(mac):
    with conn() as c:
        return c.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()


def touch_or_create_device(mac, client_id):
    """设备来请求配置时调用：存在则更新，不存在则建并生成绑定码。返回 sqlite Row。"""
    with _lock:
        row = get_device_by_mac(mac)
        with conn() as c:
            if row is None:
                code = "%06d" % random.randint(0, 999999)
                dtype = "thin_client"
                meta = {"capabilities": infer_capabilities(dtype, {}, mac)}
                c.execute("INSERT INTO devices(mac,client_id,name,agent_id,bind_code,last_seen,device_type,metadata_json,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                          (mac, client_id, "", None, code, _now(), dtype,
                           json.dumps(meta, ensure_ascii=False), _now()))
            else:
                c.execute("UPDATE devices SET client_id=?,last_seen=? WHERE mac=?",
                          (client_id, _now(), mac))
        return get_device_by_mac(mac)


def bind_device(bind_code, agent_id, name=""):
    """用绑定码把设备绑到智能体。"""
    with conn() as c:
        r = c.execute("SELECT * FROM devices WHERE bind_code=? AND (agent_id IS NULL)",
                      (bind_code,)).fetchone()
        if not r:
            return None
        c.execute("UPDATE devices SET agent_id=?,bind_code='',name=? WHERE id=?",
                  (agent_id, name or r["name"], r["id"]))
        return r["mac"]


def bind_device_by_id(device_id, agent_id, name=""):
    """按设备 id 绑定（摄像头/扬声器等无绑定码，或列表里点「绑定」）。边缘 Agent 不绑。"""
    with conn() as c:
        r = c.execute("SELECT * FROM devices WHERE id=? AND agent_id IS NULL", (int(device_id),)).fetchone()
        if not r:
            return None
        if (r["device_type"] or "") == "edge":
            return None
        c.execute("UPDATE devices SET agent_id=?,bind_code='',name=? WHERE id=?",
                  (int(agent_id), name or r["name"] or "", r["id"]))
        return r["mac"]


def unbind_device(device_id):
    with conn() as c:
        r = c.execute("SELECT device_type FROM devices WHERE id=?", (device_id,)).fetchone()
        # 摄像头/扬声器无绑定码流程，解绑后保持空码；瘦客户端重新发码
        dtype = (r["device_type"] if r else "") or ""
        code = "" if dtype in ("camera", "speaker") else ("%06d" % random.randint(0, 999999))
        c.execute("UPDATE devices SET agent_id=NULL,bind_code=? WHERE id=?", (code, device_id))


def rename_device(device_id, name):
    with conn() as c:
        c.execute("UPDATE devices SET name=? WHERE id=?", (name, device_id))


def set_device_capability(device_id, capability, enabled):
    """开关设备上某一能力（如只关摄像头麦，视觉仍开）。写入 metadata.disabled_capabilities。"""
    cap = str(capability or "").strip()
    if not cap:
        return None
    with _lock:
        with conn() as c:
            r = c.execute("SELECT * FROM devices WHERE id=?", (int(device_id),)).fetchone()
            if not r:
                return None
            meta = json.loads(r["metadata_json"] or "{}")
            caps = infer_capabilities(r["device_type"], meta, r["mac"])
            if cap not in caps:
                return None
            disabled = {str(x) for x in (meta.get("disabled_capabilities") or []) if str(x).strip()}
            if enabled:
                disabled.discard(cap)
            else:
                disabled.add(cap)
            meta["disabled_capabilities"] = sorted(disabled)
            # 保持 capabilities 显式声明，避免推断漂移
            if "capabilities" not in meta:
                meta["capabilities"] = caps
            c.execute("UPDATE devices SET metadata_json=? WHERE id=?",
                      (json.dumps(meta, ensure_ascii=False), int(device_id)))
            return {
                "id": int(device_id),
                "capability": cap,
                "enabled": bool(enabled),
                "disabled_capabilities": meta["disabled_capabilities"],
                "capabilities": caps,
            }


def device_capability_enabled(device_or_meta, capability):
    """device dict 或 metadata → 该能力是否开启（默认开）。"""
    if device_or_meta is None:
        return True
    if isinstance(device_or_meta, dict) and "disabled_capabilities" in device_or_meta:
        disabled = device_or_meta.get("disabled_capabilities") or []
    elif isinstance(device_or_meta, dict) and "metadata" in device_or_meta:
        disabled = (device_or_meta.get("metadata") or {}).get("disabled_capabilities") or []
    elif isinstance(device_or_meta, dict):
        disabled = device_or_meta.get("disabled_capabilities") or []
    else:
        disabled = []
    return str(capability) not in {str(x) for x in disabled}


def upsert_speaker_device(mac, name="", addr=""):
    """网络扬声器上线时登记为可绑定设备（device_type=speaker，能力 speaker）。"""
    mac = (mac or "").strip()
    if not mac:
        return None
    name = (name or "").strip() or "网络扬声器"
    meta = {"capabilities": ["speaker"], "addr": (addr or "").strip(), "kind": "network_speaker"}
    metaj = json.dumps(meta, ensure_ascii=False)
    with _lock:
        with conn() as c:
            row = c.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
            if row is not None and (row["device_type"] or "") != "speaker":
                return None  # 与瘦客户端等同 mac 冲突时不覆盖
            if row is None:
                c.execute("INSERT INTO devices(mac,client_id,name,agent_id,bind_code,last_seen,device_type,metadata_json,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                          (mac, "", name, None, "", _now(), "speaker", metaj, _now()))
            else:
                c.execute("UPDATE devices SET name=?,metadata_json=?,last_seen=?,device_type='speaker' WHERE id=?",
                          (name or row["name"], metaj, _now(), row["id"]))
            r = c.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
            if not r:
                return None
            return {"id": r["id"], "mac": r["mac"], "name": r["name"], "agent_id": r["agent_id"],
                    "device_type": r["device_type"], "metadata": json.loads(r["metadata_json"] or "{}"),
                    "capabilities": ["speaker"]}


def _edge_row_to_dict(r):
    return {"id": r["id"], "device_uid": r["mac"], "name": r["name"] or "",
            "device_type": r["device_type"], "agent_id": r["agent_id"],
            "metadata": json.loads(r["metadata_json"] or "{}"),
            "last_seen": r["last_seen"], "created_at": r["created_at"]}


def register_edge_device(data):
    """登记边缘 Agent 设备（esp-claw 等），以 device_uid 作 mac，device_type='edge'。
    与已有瘦客户端 mac 冲突则返回 None；同 uid 的边缘设备存在则更新其信息。"""
    uid = (data.get("device_uid") or "").strip()
    if not uid:
        return None
    name = (data.get("name") or "").strip()
    meta = json.dumps(data.get("metadata") or {}, ensure_ascii=False)
    with _lock:
        with conn() as c:
            row = c.execute("SELECT * FROM devices WHERE mac=?", (uid,)).fetchone()
            if row is not None and row["device_type"] != "edge":
                return None  # 与瘦客户端冲突
            if row is None:
                c.execute("INSERT INTO devices(mac,client_id,name,agent_id,bind_code,last_seen,device_type,metadata_json,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                          (uid, "", name, None, "", _now(), "edge", meta, _now()))
            else:
                c.execute("UPDATE devices SET name=?,metadata_json=?,last_seen=? WHERE id=?",
                          (name or row["name"], meta, _now(), row["id"]))
            r = c.execute("SELECT * FROM devices WHERE mac=?", (uid,)).fetchone()
            return _edge_row_to_dict(r)


def delete_edge_device(device_id):
    """删除边缘设备（仅限 device_type='edge'）。返回是否删掉。"""
    with conn() as c:
        cur = c.execute("DELETE FROM devices WHERE id=? AND device_type='edge'", (device_id,))
        return cur.rowcount > 0


# ---------------- 摄像头设备（device_type='camera'，复用设备表可绑定智能体） ----------------
def _camera_row_to_dict(r):
    meta = json.loads(r["metadata_json"] or "{}")
    return {"id": r["id"], "mac": r["mac"], "name": r["name"] or "",
            "device_type": r["device_type"], "agent_id": r["agent_id"],
            "src": meta.get("src", ""), "go2rtc_url": meta.get("go2rtc_url", ""),
            "note": meta.get("note", ""), "metadata": meta, "last_seen": r["last_seen"]}


def register_camera_device(data):
    """登记/更新摄像头设备。metadata 存 {src, go2rtc_url, note}；mac='camera:<src>'。
    可带 agent_id 直接绑定到某智能体。src 冲突到非摄像头设备则返回 None。"""
    src = (data.get("src") or "").strip()
    if not src:
        return None
    name = (data.get("name") or src).strip()
    agent_id = data.get("agent_id")
    meta = {"src": src, "go2rtc_url": (data.get("go2rtc_url") or "").strip(),
            "note": (data.get("note") or "").strip(),
            "capabilities": ["mic"]}  # 摄像头麦克风音轨（视觉链路已移除）
    mac = "camera:" + src
    metaj = json.dumps(meta, ensure_ascii=False)
    with _lock:
        with conn() as c:
            row = c.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
            if row is not None and row["device_type"] != "camera":
                return None
            if row is None:
                c.execute("INSERT INTO devices(mac,client_id,name,agent_id,bind_code,last_seen,device_type,metadata_json,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                          (mac, "", name, agent_id, "", _now(), "camera", metaj, _now()))
            else:
                c.execute("UPDATE devices SET name=?,agent_id=?,metadata_json=?,last_seen=? WHERE id=?",
                          (name, agent_id if agent_id is not None else row["agent_id"], metaj, _now(), row["id"]))
            r = c.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
            return _camera_row_to_dict(r)


def list_cameras():
    with conn() as c:
        rows = c.execute("SELECT d.*, a.name agent_name FROM devices d LEFT JOIN agents a ON d.agent_id=a.id "
                         "WHERE d.device_type='camera' ORDER BY d.id").fetchall()
        out = []
        for r in rows:
            d = _camera_row_to_dict(r)
            d["agent_name"] = r["agent_name"]
            out.append(d)
        return out


def get_camera(ref):
    """按 设备id / src名 / mac 取摄像头。找不到返回 None。"""
    s = str(ref)
    with conn() as c:
        r = None
        if s.isdigit():
            r = c.execute("SELECT * FROM devices WHERE id=? AND device_type='camera'", (int(s),)).fetchone()
        if r is None:
            r = c.execute("SELECT * FROM devices WHERE device_type='camera' AND (mac=? OR mac=?)",
                          (s, "camera:" + s)).fetchone()
        return _camera_row_to_dict(r) if r else None


def get_agent_camera(agent_id, require_cap="mic"):
    """某智能体绑定的第一个摄像头（默认'用摄像头麦'）。
    require_cap：要求该能力未关闭；传 None 则不过滤。"""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM devices WHERE device_type='camera' AND agent_id=? ORDER BY id",
            (agent_id,),
        ).fetchall()
    for r in rows:
        d = _camera_row_to_dict(r)
        if require_cap and not device_capability_enabled(d, require_cap):
            continue
        return d
    return None


def delete_camera_device(device_id):
    with conn() as c:
        cur = c.execute("DELETE FROM devices WHERE id=? AND device_type='camera'", (device_id,))
        return cur.rowcount > 0


def seed_camera_from_json(camera_json_path):
    """一次性：把旧的 data/camera.json 里的单台摄像头迁成一个摄像头设备，绑到第 1 个智能体。"""
    if get_setting("camera_seeded_v1"):
        return
    set_setting("camera_seeded_v1", "1")
    try:
        with open(camera_json_path, encoding="utf-8") as f:
            cj = json.load(f)
    except Exception:
        return
    src = (cj.get("src") or "").strip()
    if not src or list_cameras():
        return
    ag = list_agents()
    register_camera_device({
        "src": src, "name": src,
        "go2rtc_url": (cj.get("go2rtc_url") or "").strip(),
        "agent_id": ag[0]["id"] if ag else None,
    })
