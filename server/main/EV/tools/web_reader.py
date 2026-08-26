# -*- coding: utf-8 -*-
"""Web reader tool used by Muse previews and agent summaries."""
import re
from io import BytesIO
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CONTENT_SELECTORS = [
    "article",
    ".article",
    ".article-content",
    ".article__content",
    ".news_txt",
    ".newscontent",
    ".news_content",
    ".index_content",
    ".index_centent",
    ".detail_content",
    ".left_zw",
    "#content",
    ".content",
    "main",
    '[role="main"]',
]

SKIP_IMG_URL = re.compile(
    r"(logo|icon|favicon|avatar|sprite|spacer|blank|1x1|pixel|tracking|"
    r"qrcode|qr[_-]?code|badge|arrow|share[-_]|weixin|wechat|emoji|"
    r"button[-_]|/ad[s]?[/_.]|advert|adserv|doubleclick|"
    r"/_next/static/|/assets/icons?/|/images/icons?/|placeholder|"
    r"default[_-]?img|no[_-]?pic|loading\.gif|transparent\.gif|"
    r"wx[-_]?code|app[-_]?download|client[-_]?download|"
    r"theme[-_]?default|watermark[-_]?small)",
    re.I,
)

SKIP_IMG_CLASS = re.compile(
    r"(^|[\s_-])(logo|icon|avatar|qrcode|share|toolbar|header|footer|nav|"
    r"comment|sidebar|ad[-_]|banner[-_]?sm|thumb[-_]?user|user[-_]?pic)([\s_-]|$)",
    re.I,
)

SKIP_ALT = re.compile(
    r"^(logo|icon|image|img|photo|pic|banner|封面|图片|海报|头像|二维码|"
    r"分享|返回|下载|客户端|logo)$",
    re.I,
)

MIN_IMG_SCORE = 4
MAX_IMAGES = 4


def is_safe_url(url: str) -> bool:
    try:
        u = urlparse((url or "").strip())
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False


def _abs_url(base: str, src: str) -> str:
    if not src:
        return ""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http://") or src.startswith("https://"):
        return src
    return urljoin(base, src)


def _extract_title(soup: BeautifulSoup) -> str:
    for sel in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        el = soup.select_one(sel)
        if el and el.get("content"):
            t = el["content"].strip()
            if t:
                return re.sub(r"[_\-|].*$", "", t).strip() or t
    h1 = soup.select_one("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t:
            return t
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def _extract_lead(soup: BeautifulSoup) -> str:
    for sel in ('meta[property="og:description"]', 'meta[name="description"]'):
        el = soup.select_one(sel)
        if el and el.get("content"):
            t = el["content"].strip()
            if len(t) > 12:
                return t
    return ""


def _pick_content_root(soup: BeautifulSoup):
    root = None
    best_len = 0
    for sel in CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if not el:
            continue
        n = len(el.get_text(strip=True))
        if n > best_len:
            best_len = n
            root = el
    if not root or best_len < 80:
        root = soup.body or soup
    return root


def _parse_dim(val) -> int:
    if val is None:
        return 0
    try:
        s = str(val).strip().lower().replace("px", "")
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def _img_url_ok(url: str) -> bool:
    if not url or url.startswith("data:"):
        return False
    low = url.lower()
    if low.endswith(".svg") or ".svg?" in low:
        return False
    if SKIP_IMG_URL.search(low):
        return False
    return True


def _img_in_bad_container(img) -> bool:
    for parent in img.parents:
        if parent.name in ("header", "nav", "footer", "aside"):
            return True
        if parent.name in ("body", "[document]"):
            break
        cls = " ".join(parent.get("class") or [])
        pid = parent.get("id") or ""
        blob = f"{cls} {pid}"
        if SKIP_IMG_CLASS.search(blob):
            return True
        if parent.name == "a":
            href = (parent.get("href") or "").strip()
            if href in ("/", "#", "") or re.match(r"^https?://[^/]+/?$", href):
                imgs = parent.find_all("img")
                if len(imgs) == 1 and imgs[0] is img:
                    if len(parent.get_text(strip=True)) < 4:
                        return True
    return False


def _score_image(img, src: str, alt: str, base_url: str, source: str = "body"):
    u = _abs_url(base_url, src)
    if not is_safe_url(u) or not _img_url_ok(u):
        return None

    alt = (alt or "").strip()
    if alt and SKIP_ALT.match(alt):
        return None

    if img is not None:
        if _img_in_bad_container(img):
            return None
        cls = " ".join(img.get("class") or [])
        iid = img.get("id") or ""
        if SKIP_IMG_CLASS.search(f"{cls} {iid}"):
            return None
        if img.get("role") == "presentation" or img.get("aria-hidden") == "true":
            return None

    score = 0
    if source == "og":
        score += 3
    if img is not None:
        if img.find_parent("figure"):
            score += 10
        if img.find_parent("p"):
            score += 6
        w = _parse_dim(img.get("width"))
        h = _parse_dim(img.get("height"))
        if w and h:
            if w < 120 or h < 80:
                return None
            if w >= 480 and h >= 270:
                score += 12
            elif w >= 280 and h >= 160:
                score += 8
            elif w >= 180 and h >= 120:
                score += 4
        style = (img.get("style") or "").lower()
        mw = re.search(r"max-width:\s*(\d+)", style)
        mh = re.search(r"max-height:\s*(\d+)", style)
        if mw and int(mw.group(1)) < 100:
            return None
        if mh and int(mh.group(1)) < 80:
            return None
    else:
        # og:image：路径像正文配图才保留
        if re.search(r"(news|article|content|photo|image|pic|cover|thumb)", u, re.I):
            score += 8
        elif SKIP_IMG_URL.search(u):
            return None

    if alt and len(alt) >= 8 and not SKIP_ALT.match(alt):
        score += 5
    elif alt and len(alt) >= 4:
        score += 2

    if score < MIN_IMG_SCORE:
        return None
    return {"url": u, "alt": alt[:200], "score": score}


def _extract_images(root, base_url: str, soup: BeautifulSoup = None) -> list:
    candidates = []

    if soup:
        og = soup.select_one('meta[property="og:image"]')
        if og and og.get("content"):
            item = _score_image(None, og["content"], "", base_url, source="og")
            if item:
                candidates.append(item)

    if hasattr(root, "find_all"):
        for img in root.find_all("img"):
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-original")
                or img.get("data-lazy-src")
            )
            if not src:
                continue
            cap = ""
            fig = img.find_parent("figure")
            if fig:
                fc = fig.find("figcaption")
                if fc:
                    cap = fc.get_text(strip=True)
            item = _score_image(img, src, cap or img.get("alt") or "", base_url)
            if item:
                candidates.append(item)

    # 高分优先，去重
    candidates.sort(key=lambda x: -x["score"])
    seen = set()
    out = []
    for c in candidates:
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        out.append({"url": c["url"], "alt": c["alt"]})
        if len(out) >= MAX_IMAGES:
            break
    return out


def _extract_paragraphs(root) -> list:
    paras = []
    for node in root.find_all(["p", "h2", "h3", "h4", "blockquote"]):
        text = node.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        if len(text) < 8:
            continue
        if len(text) > 3000:
            text = text[:3000] + "…"
        paras.append({"tag": node.name, "text": text})
    return paras


def _markitdown_text(html: str, url: str) -> str:
    try:
        from markitdown import MarkItDown

        md = MarkItDown(enable_plugins=False)
        result = md.convert(BytesIO(html.encode("utf-8", errors="ignore")))
        text = (result.text_content or "").strip()
    except Exception:
        try:
            import requests as _rq
            from markitdown import MarkItDown

            r = _rq.Response()
            r._content = html.encode("utf-8", errors="ignore")
            r.headers["content-type"] = "text/html; charset=utf-8"
            r.url = url
            md = MarkItDown(enable_plugins=False)
            text = (md.convert(r).text_content or "").strip()
        except Exception:
            return ""

    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or len(s) < 4:
            continue
        if re.match(
            r"^(首页|登录|注册|分享到|相关阅读|推荐阅读|责任编辑|来源[:：]|下载客户端|返回|评论|收藏)",
            s,
        ):
            continue
        if s.startswith("[") and s.endswith(")") and len(s) < 80:
            continue
        lines.append(s)
    return "\n".join(lines).strip()


def _paragraphs_from_text(text: str) -> list:
    if not text:
        return []
    blocks = re.split(r"\n{2,}", text)
    flat = []
    for block in blocks:
        s = re.sub(r"\s+", " ", block).strip()
        if len(s) < 12:
            continue
        if len(s) > 500:
            parts = re.split(r"(?<=[。！？；])", s)
            buf = ""
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if len(buf) + len(part) < 320:
                    buf += part
                else:
                    if len(buf) >= 12:
                        flat.append(buf)
                    buf = part
            if len(buf) >= 12:
                flat.append(buf)
        else:
            flat.append(s)
    paras = []
    for s in flat:
        if len(s) < 12:
            continue
        tag = "h3" if len(s) < 90 and not s.endswith(("。", "！", "？")) else "p"
        paras.append({"tag": tag, "text": s[:3000]})
    return paras


def extract_reader(url: str, timeout: int = 20, include_images: bool = False) -> dict:
    if not is_safe_url(url):
        return {"ok": False, "error": "无效的网页地址"}

    try:
        resp = requests.get(
            url.strip(),
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as e:
        return {"ok": False, "error": "无法访问网页: %s" % (str(e)[:120])}

    ctype = (resp.headers.get("content-type") or "").lower()
    if "html" not in ctype and "text" not in ctype:
        return {"ok": False, "error": "该链接不是网页内容"}

    resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
    html = resp.text or ""
    final_url = resp.url or url

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "form", "svg"]):
        tag.decompose()

    title = _extract_title(soup)
    lead = _extract_lead(soup)
    site = urlparse(final_url).hostname or ""
    root = _pick_content_root(soup)

    paragraphs = _extract_paragraphs(root)
    md_text = _markitdown_text(html, final_url)
    md_paras = _paragraphs_from_text(md_text)

    # MarkItDown 通常更完整，优先采用更长的版本
    plain_len = sum(len(p["text"]) for p in paragraphs)
    md_len = sum(len(p["text"]) for p in md_paras)
    if md_len > plain_len + 80:
        paragraphs = md_paras
        full_text = md_text
    else:
        full_text = "\n\n".join(p["text"] for p in paragraphs)

    seen = set()
    unique = []
    for p in paragraphs:
        if p["text"] in seen:
            continue
        seen.add(p["text"])
        unique.append(p)

    if lead and (not unique or unique[0]["text"] != lead):
        unique.insert(0, {"tag": "p", "text": lead})

    images = []
    if include_images:
        try:
            images = _extract_images(root, final_url, soup=soup)[:MAX_IMAGES]
        except Exception:
            images = []

    if not unique and md_text:
        unique = md_paras[:48]
        full_text = md_text

    if not unique:
        return {
            "ok": False,
            "error": "未能提取正文",
            "title": title,
            "site": site,
            "url": final_url,
        }

    return {
        "ok": True,
        "title": title,
        "lead": lead,
        "site": site,
        "url": final_url,
        "image": (images[0]["url"] if images else ""),
        "images": images,
        "paragraphs": unique[:60],
        "full_text": full_text[:12000],
    }
