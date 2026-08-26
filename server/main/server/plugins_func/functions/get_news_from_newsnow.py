import random
import sys
from pathlib import Path

import httpx
from markitdown import MarkItDown
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from plugins_func.muse_panel import skill_panel
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

_MUSE_DIR = Path(__file__).resolve().parents[3] / "EV"
if str(_MUSE_DIR) not in sys.path:
    sys.path.insert(0, str(_MUSE_DIR))
from tools import web_reader  # noqa: E402


TAG = __name__
logger = setup_logging()

CHANNEL_MAP = {
    "V2EX": "v2ex-share",
    "知乎": "zhihu",
    "微博": "weibo",
    "联合早报": "zaobao",
    "酷安": "coolapk",
    "MKTNews": "mktnews-flash",
    "华尔街见闻": "wallstreetcn-quick",
    "36氪": "36kr-quick",
    "抖音": "douyin",
    "虎扑": "hupu",
    "百度贴吧": "tieba",
    "今日头条": "toutiao",
    "IT之家": "ithome",
    "澎湃新闻": "thepaper",
    "卫星通讯社": "sputniknewscn",
    "参考消息": "cankaoxiaoxi",
    "远景论坛": "pcbeta-windows11",
    "财联社": "cls-depth",
    "雪球": "xueqiu-hotstock",
    "格隆汇": "gelonghui",
    "法布财经": "fastbull-express",
    "Solidot": "solidot",
    "Hacker News": "hackernews",
    "Product Hunt": "producthunt",
    "Github": "github-trending-today",
    "哔哩哔哩": "bilibili-hot-search",
    "快手": "kuaishou",
    "靠谱新闻": "kaopu",
    "金十数据": "jin10",
    "百度热搜": "baidu",
    "牛客": "nowcoder",
    "少数派": "sspai",
    "稀土掘金": "juejin",
    "凤凰网": "ifeng",
    "虫部落": "chongbuluo-latest",
}

# 默认新闻来源字典，当配置中没有指定时使用
DEFAULT_NEWS_SOURCES = "澎湃新闻;百度热搜;财联社"

def _get_newsnow_config(conn):
    # 从连接配置获取
    plugins = conn.config.get("plugins", {})
    newsnow = plugins.get("get_news_from_newsnow", {})
    sources = newsnow.get("news_sources", "")
    if isinstance(sources, str) and sources.strip():
        return sources

    return ""

def get_news_sources_from_config(conn):
    """从配置中获取新闻源字符串"""
    try:
        result = _get_newsnow_config(conn)
        if result:
            logger.bind(tag=TAG).debug(f"使用配置的新闻源: {result}")
            return result

        logger.bind(tag=TAG).debug("未找到新闻源配置，使用默认配置")
        return DEFAULT_NEWS_SOURCES

    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻源配置失败: {e}，使用默认配置")
        return DEFAULT_NEWS_SOURCES


# 从默认配置获取可用的新闻源名称（运行时由get_news_sources_from_config动态获取）
example_sources_str = DEFAULT_NEWS_SOURCES.replace(";","、")

GET_NEWS_FROM_NEWSNOW_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_news_from_newsnow",
        "description": (
            "查看或收听新闻；获取新闻列表；总结/解读某条新闻正文。"
            "用户说「看看新闻/打开新闻」先拉列表；"
            "「总结/讲讲/解读 第N条/这篇」时设 detail=true，并传 index 或 url。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": f"新闻源中文名，如{example_sources_str}等；拉列表时用",
                },
                "detail": {
                    "type": "boolean",
                    "description": "true=抓取并总结指定新闻正文（需配合 url 或 index）",
                },
                "url": {
                    "type": "string",
                    "description": "要总结或预览的新闻链接（优先于 index）",
                },
                "index": {
                    "type": "integer",
                    "description": "新闻列表中的序号，从1开始，对应最近一次列表",
                },
                "title": {
                    "type": "string",
                    "description": "按标题关键词匹配新闻（如「巴西共产党」），配合 detail=true 使用",
                },
                "lang": {
                    "type": "string",
                    "description": "返回用户使用的语言code，例如zh_CN/zh_HK/en_US/ja_JP等，默认zh_CN",
                },
            },
            "required": ["lang"],
        },
    },
}


async def fetch_news_from_api(conn: "ConnectionHandler", source="thepaper"):
    """从API获取新闻列表"""
    try:
        api_url = f"https://newsnow.busiyi.world/api/s?id={source}"

        news_config = conn.config.get("plugins", {}).get("get_news_from_newsnow", {})
        if news_config.get("url"):
            api_url = news_config["url"] + source

        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.get(api_url, headers=headers)

        data = response.json()

        if "items" in data:
            return data["items"]
        else:
            logger.bind(tag=TAG).error(f"获取新闻API响应格式错误: {data}")
            return []

    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻API失败: {e}")
        return []


async def fetch_news_detail(url):
    """获取新闻详情正文（优先 web_reader，回退 MarkItDown）"""
    try:
        article = web_reader.extract_reader(url)
        if article.get("ok") and article.get("full_text"):
            return article["full_text"], article
    except Exception as e:
        logger.bind(tag=TAG).debug(f"web_reader 失败，回退 MarkItDown: {e}")

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=3.0)) as client:
            response = await client.get(url, headers=headers)

        md = MarkItDown(enable_plugins=False)
        result = md.convert(response)
        clean_text = (result.text_content or "").strip()

        if not clean_text:
            logger.bind(tag=TAG).warning(f"清理后的新闻内容为空: {url}")
            return "无法解析新闻详情内容，可能是网站结构特殊或内容受限。", None

        return clean_text, None
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻详情失败: {e}")
        return "无法获取详细内容", None


def _match_title(query: str, candidate: str) -> bool:
    q = (query or "").strip().lower()
    c = (candidate or "").strip().lower()
    if not q or not c:
        return False
    if q in c or c in q:
        return True
    # 简单关键词：query 中连续 2+ 字的片段出现在标题里
    for i in range(len(q) - 1):
        frag = q[i : i + 2]
        if frag in c:
            return True
    return False


def _resolve_news_url(conn, url=None, index=None, title=None):
    """按 url / index / title / 上次播报解析目标链接。"""
    if url and str(url).strip() not in ("", "#"):
        return str(url).strip(), None

    items = getattr(conn, "last_newsnow_items", None) or []
    if index is not None:
        try:
            idx = int(index) - 1
            if 0 <= idx < len(items):
                it = items[idx]
                return (it.get("url") or "").strip(), it.get("title")
        except (TypeError, ValueError):
            pass

    if title and str(title).strip():
        q = str(title).strip()
        for it in items:
            t = it.get("title") or ""
            if _match_title(q, t):
                return (it.get("url") or "").strip(), t
        link = getattr(conn, "last_newsnow_link", None) or {}
        lt = link.get("title") or ""
        if _match_title(q, lt):
            return (link.get("url") or "").strip(), lt

    link = getattr(conn, "last_newsnow_link", None) or {}
    return (link.get("url") or "").strip(), link.get("title")


def _reader_from_text(title, url, text):
    paras = [
        {"tag": "p", "text": line.strip()}
        for line in (text or "").split("\n")
        if line.strip()
    ]
    return {
        "ok": True,
        "title": title,
        "url": url,
        "paragraphs": paras,
        "full_text": text,
        "images": [],
    }


@register_function(
    "get_news_from_newsnow",
    GET_NEWS_FROM_NEWSNOW_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
async def get_news_from_newsnow(
    conn: "ConnectionHandler",
    source: str = "澎湃新闻",
    detail: bool = False,
    lang: str = "zh_CN",
    url: str = "",
    index: int = None,
    title: str = "",
):
    """获取新闻并随机选择一条进行播报，或获取指定新闻的详细内容"""
    try:
        news_sources = get_news_sources_from_config(conn)
        detail = str(detail).lower() == "true"

        if detail:
            target_url, picked_title = _resolve_news_url(
                conn, url=url, index=index, title=title
            )
            if not target_url or target_url == "#":
                return ActionResponse(
                    Action.REQLLM,
                    "抱歉，没有找到要总结的新闻链接。请先说「看看新闻」拉列表，或说明第几条。",
                    None,
                )

            title = picked_title or "新闻"
            logger.bind(tag=TAG).info(f"获取新闻详情: {title}, URL={target_url}")

            detail_content, article = await fetch_news_detail(target_url)
            if not detail_content or detail_content == "无法获取详细内容":
                return ActionResponse(
                    Action.REQLLM,
                    f"抱歉，无法获取《{title}》的详细内容，可能是链接已失效或网站限制访问。",
                    None,
                )

            if article and article.get("title"):
                title = article["title"]

            conn.last_newsnow_link = {
                "url": target_url,
                "title": title,
                "source_id": (conn.last_newsnow_link or {}).get("source_id", "thepaper"),
            }

            detail_report = (
                f"根据下列新闻正文，用{lang}向用户做简洁总结（3-6句口语），提取关键事实，不要念网址：\n\n"
                f"标题: {title}\n"
                f"正文:\n{detail_content[:8000]}\n\n"
                f"(像讲述新闻一样自然说出来，不要说是「总结」或「正文显示」)"
            )

            panel = None
            if article and article.get("ok"):
                panel = skill_panel(
                    "web",
                    title[:40],
                    url=target_url,
                    width=680,
                    height=540,
                    data=article,
                )
            elif detail_content:
                reader = _reader_from_text(title, target_url, detail_content)
                panel = skill_panel(
                    "web",
                    title[:40],
                    url=target_url,
                    width=680,
                    height=540,
                    data=reader,
                )
            elif web_reader.is_safe_url(target_url):
                panel = skill_panel(
                    "web",
                    title[:40],
                    url=target_url,
                    width=680,
                    height=540,
                )

            return ActionResponse(Action.REQLLM, detail_report, None, panel=panel)

        # 否则，获取新闻列表并随机选择一条
        # 将中文名称转换为英文ID
        english_source_id = None

        # 检查输入的中文名称是否在配置的新闻源中
        news_sources_list = [
            name.strip() for name in news_sources.split(";") if name.strip()
        ]
        if source in news_sources_list:
            # 如果输入的中文名称在配置的新闻源中，在 CHANNEL_MAP 中查找对应的英文ID
            english_source_id = CHANNEL_MAP.get(source)

        # 如果找不到对应的英文ID，使用默认源
        if not english_source_id:
            logger.bind(tag=TAG).warning(f"无效的新闻源: {source}，使用默认源澎湃新闻")
            english_source_id = "thepaper"
            source = "澎湃新闻"

        logger.bind(tag=TAG).info(f"获取新闻: 新闻源={source}({english_source_id})")

        # 获取新闻列表
        news_items = await fetch_news_from_api(conn, english_source_id)

        if not news_items:
            return ActionResponse(
                Action.REQLLM,
                f"抱歉，未能从{source}获取到新闻信息，请稍后再试或尝试其他新闻源。",
                None,
            )

        # 随机选择一条新闻
        selected_news = random.choice(news_items)

        # 保存当前新闻链接到连接对象，以便后续查询详情
        if not hasattr(conn, "last_newsnow_link"):
            conn.last_newsnow_link = {}
        conn.last_newsnow_link = {
            "url": selected_news.get("url", "#"),
            "title": selected_news.get("title", "未知标题"),
            "source_id": english_source_id,
        }

        list_items = []
        for it in news_items[:12]:
            list_items.append(
                {
                    "title": it.get("title") or "无标题",
                    "url": it.get("url") or "",
                    "source": source,
                }
            )

        list_lines = []
        for i, it in enumerate(list_items[:10], 1):
            line = f"{i}. {it['title']}"
            if it.get("url"):
                line += f" | {it['url']}"
            list_lines.append(line)

        conn.last_newsnow_items = list_items

        news_report = (
            f"根据下列数据，用{lang}回应用户的新闻查询请求：\n\n"
            f"新闻列表：\n"
            + "\n".join(list_lines)
            + "\n\n"
            f"规则：用户说「预览第N条」→ muse_ui_open_panel panel=web 传该条 url；"
            f"用户说「总结/讲讲/详细内容/这篇」→ 本工具 detail=true，传 index、url 或 title 关键词。\n"
            f"默认口头播报第1条标题即可，提示可预览或让我总结。"
        )

        panel = skill_panel(
            "news",
            f"{source} · 热点",
            data={"source": source, "items": list_items},
            width=460,
            height=440,
        )

        return ActionResponse(Action.REQLLM, news_report, None, panel=panel)

    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻出错: {e}")
        return ActionResponse(
            Action.REQLLM, "抱歉，获取新闻时发生错误，请稍后再试。", None
        )
