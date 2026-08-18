# coding=utf-8
"""
信息聚合渠道抓取器（服务端轻量数据源，无浏览器依赖）

提供信息聚合面板所需的扩展渠道：
- arXiv 热点论文（官方 Atom API，cs.AI/cs.LG/cs.CL 最新提交）
- 水木社区每日十大热门话题（BBSLists 版自动发帖，GBK 编码解析）
- AI 圈新概念/新技术/新架构（Hacker News frontpage RSS + AI 关键词过滤）

设计约束：
- 全部使用标准库 + requests，无 Playwright/Chrome 依赖
- 任一渠道抓取失败不影响其他渠道（各自 try/except）
- 时间字段统一为 ISO 字符串，供面板展示
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("trendradar.channels")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/atom+xml, application/rss+xml, application/xml, text/html, */*",
}

TIMEOUT = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 1. arXiv 热点论文
# ============================================================
ARXIV_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def fetch_arxiv_papers(
    max_results: int = 20,
    categories: Optional[List[str]] = None,
) -> Dict:
    """
    抓取 arXiv 最新提交论文（默认 cs.AI / cs.LG / cs.CL）。

    Returns:
        {"ok": bool, "items": List[Dict], "fetched_at": str, "error": str}
        item: {"title", "url", "published_at", "category", "authors", "summary", "extra"}
    """
    cats = categories or ["cs.AI", "cs.LG", "cs.CL"]
    query = "+OR+".join(f"cat:{c}" for c in cats)
    url = (
        f"https://export.arxiv.org/api/query"
        f"?search_query={query}&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={max_results}"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as e:  # noqa: BLE001
        logger.warning("arXiv 抓取失败: %s", e)
        return {"ok": False, "items": [], "fetched_at": _now_iso(), "error": str(e)[:150]}

    items: List[Dict] = []
    for entry in root.findall("atom:entry", ARXIV_ATOM_NS):
        title = (entry.findtext("atom:title", "", ARXIV_ATOM_NS) or "").strip()
        title = re.sub(r"\s+", " ", title)
        link = entry.find("atom:link", ARXIV_ATOM_NS)
        url = link.get("href") if link is not None else ""
        published = (entry.findtext("atom:published", "", ARXIV_ATOM_NS) or "").strip()
        summary = (entry.findtext("atom:summary", "", ARXIV_ATOM_NS) or "").strip()
        summary = re.sub(r"\s+", " ", summary)[:300]
        authors = [a.findtext("atom:name", "", ARXIV_ATOM_NS) for a in entry.findall("atom:author", ARXIV_ATOM_NS)]
        cat_el = entry.find("arxiv:primary_category", ARXIV_ATOM_NS)
        category = cat_el.get("term") if cat_el is not None else ""
        if title and url:
            items.append({
                "title": title,
                "url": url,
                "published_at": published,
                "category": category,
                "authors": authors[:3],
                "summary": summary,
                "extra": {"source": "arxiv"},
            })
    return {"ok": True, "items": items, "fetched_at": _now_iso(), "error": ""}


# ============================================================
# 2. 水木社区每日十大热门话题
# ============================================================
SMTH_BBSLISTS_URL = "https://www.newsmth.net/nForum/board/BBSLists"
SMTH_ARTICLE_URL = "https://www.newsmth.net/nForum/article/BBSLists/{aid}"


def _smth_fetch(url: str) -> Optional[str]:
    """抓取水木页面并解码 GBK"""
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.content.decode("gbk", errors="replace")


def _parse_smth_top10(html: str) -> List[Dict]:
    """解析『本日十大热门话题』文章正文，提取前十话题"""
    # 先替换 HTML 实体（&nbsp; 等），再去除全部标签并压缩空白，便于正则匹配
    html = re.sub(r"&nbsp;?", " ", html)
    html = re.sub(r"&[a-z]+;", " ", html)
    text = re.sub(r"<[^>]+>", "|", html)
    text = re.sub(r"[|\s]+", " ", text)

    items: List[Dict] = []
    # 匹配形如：第 1 名 信区 : FamilyLife 标题 : xxx
    # 条目之间以「第 N 名」分隔；最后一名（第 10 名）标题后是页面尾部
    # 文案（阅读文章 / 返回顶部 / 体验模式），作为结束标记，避免吞掉整个尾部
    pattern = re.compile(
        r"第\s*(\d+)\s*名\s+信区\s*:\s*([A-Za-z0-9_]+).*?标题\s*:\s*([^|]{4,80}?)"
        r"(?=\s*第\s*\d+\s*名|阅读文章|返回顶部|体验模式|$)",
        re.S,
    )
    for m in pattern.finditer(text):
        rank = int(m.group(1))
        section = m.group(2)
        title = re.sub(r"\s+", " ", m.group(3)).strip()
        if not title:
            continue
        items.append({
            "title": title,
            "rank": rank,
            # 十大榜单本身不含帖子直链，指向该话题所属板块（可正常打开浏览）
            "url": f"https://www.newsmth.net/nForum/board/{section}",
            "category": section,
            "published_at": "",
            "extra": {"section": section, "source": "newsmth"},
        })
    return items


def fetch_smth_daily_top(max_items: int = 10) -> Dict:
    """
    抓取水木社区每日十大热门话题（BBSLists 版自动发帖）。

    Returns:
        {"ok": bool, "items": List[Dict], "fetched_at": str, "error": str}
    """
    try:
        board_html = _smth_fetch(SMTH_BBSLISTS_URL)
        # 找最新一篇「十大热门话题」文章
        arts = re.findall(r"/nForum/article/BBSLists/(\d+)\"[^>]*>([^<]{4,40})<", board_html)
        target_aid = None
        for aid, text in arts:
            text = text.strip()
            if "十大热门话题" in text and "祝福" not in text:
                target_aid = aid
                break
        if not target_aid:
            return {"ok": False, "items": [], "fetched_at": _now_iso(), "error": "未找到十大热门话题文章"}
        article_html = _smth_fetch(SMTH_ARTICLE_URL.format(aid=target_aid))
        items = _parse_smth_top10(article_html)[:max_items]
        return {"ok": True, "items": items, "fetched_at": _now_iso(), "error": ""}
    except Exception as e:  # noqa: BLE001
        logger.warning("水木每日热门抓取失败: %s", e)
        return {"ok": False, "items": [], "fetched_at": _now_iso(), "error": str(e)[:150]}


# ============================================================
# 3. AI 圈新概念 / 新技术 / 新架构（Hacker News + 关键词过滤）
# ============================================================
HN_RSS_URL = "https://hnrss.org/frontpage"

# AI 相关关键词（标题命中即视为 AI 圈内容）
AI_KEYWORDS = [
    "ai", "llm", "gpt", "openai", "anthropic", "claude", "gemini", "deepseek",
    "machine learning", "neural", "transformer", "agent", "diffusion", "model",
    "inference", "training", "fine-tun", "rag", "embedding", "token",
    "cuda", "gpu", "tpu", "quantiz", "moe", "reasoning", "autonomous",
]


def _parse_rss_items(xml_text: str) -> List[Dict]:
    """解析 RSS 2.0 条目"""
    items: List[Dict] = []
    root = ET.fromstring(xml_text)
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title and link:
            items.append({"title": title, "url": link, "published_at": pub, "category": "AI", "extra": {"source": "hackernews"}})
    return items


def fetch_ai_news(max_items: int = 20) -> Dict:
    """
    抓取 Hacker News 首页，过滤 AI 圈相关内容作为『AI 圈新概念/新技术』渠道。

    Returns:
        {"ok": bool, "items": List[Dict], "fetched_at": str, "error": str}
    """
    try:
        resp = requests.get(HN_RSS_URL, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        all_items = _parse_rss_items(resp.text)
    except Exception as e:  # noqa: BLE001
        logger.warning("HN 抓取失败: %s", e)
        return {"ok": False, "items": [], "fetched_at": _now_iso(), "error": str(e)[:150]}

    matched = []
    for it in all_items:
        title_lower = it["title"].lower()
        if any(kw in title_lower for kw in AI_KEYWORDS):
            matched.append(it)
            if len(matched) >= max_items:
                break
    return {"ok": True, "items": matched, "fetched_at": _now_iso(), "error": ""}
