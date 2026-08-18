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

import json
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
    抓取 arXiv 最新提交论文。

    仅关注计算机科学（cs.*）与人工智能：搜索限定 AI 核心分类
    （cs.AI / cs.LG / cs.CL / cs.CV / cs.NE），并按主分类过滤，仅保留
    主分类以 cs. 开头的论文（排除 stat.* / q-fin.* / math.* 等非 CS 类别）。

    Returns:
        {"ok": bool, "items": List[Dict], "fetched_at": str, "error": str}
        item: {"title", "url", "published_at", "category", "authors", "summary", "extra"}
    """
    cats = categories or ["cs.AI", "cs.LG", "cs.CL", "cs.CV", "cs.NE"]
    query = "+OR+".join(f"cat:{c}" for c in cats)
    # 放大抓取量，过滤后仍有足够条数
    url = (
        f"https://export.arxiv.org/api/query"
        f"?search_query={query}&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={max_results * 2}"
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
        cat_el = entry.find("arxiv:primary_category", ARXIV_ATOM_NS)
        category = cat_el.get("term") if cat_el is not None else ""
        # 只保留计算机科学（cs.*）主分类的论文
        if not category.startswith("cs."):
            continue
        title = (entry.findtext("atom:title", "", ARXIV_ATOM_NS) or "").strip()
        title = re.sub(r"\s+", " ", title)
        link = entry.find("atom:link", ARXIV_ATOM_NS)
        url = link.get("href") if link is not None else ""
        published = (entry.findtext("atom:published", "", ARXIV_ATOM_NS) or "").strip()
        summary = (entry.findtext("atom:summary", "", ARXIV_ATOM_NS) or "").strip()
        summary = re.sub(r"\s+", " ", summary)[:300]
        authors = [a.findtext("atom:name", "", ARXIV_ATOM_NS) for a in entry.findall("atom:author", ARXIV_ATOM_NS)]
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
        if len(items) >= max_results:
            break
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


# ============================================================
# 4. 批量中文翻译（AI 功能启用时，将标题翻译为简体中文）
# ============================================================
def translate_titles_to_chinese(
    items: List[Dict],
    ai_config: Dict,
) -> List[Dict]:
    """
    当 AI 功能启用时，将条目标题批量翻译为简体中文。

    使用 TrendRadar 的 AIClient（LiteLLM）一次调用翻译所有标题，
    返回携带 extra.original_title（原文）的新列表。
    翻译失败 / AI 未配置 / AI 关闭时，返回原文列表（不中断发布）。

    Args:
        items: 渠道条目列表（含 title 字段）
        ai_config: config["AI"]（MODEL/API_KEY/API_BASE/ENABLED）

    Returns:
        翻译后的条目列表（失败时等同原文）
    """
    if not items:
        return items

    # 仅当 AI 功能启用且配置了模型/密钥时翻译
    if not ai_config.get("ENABLED"):
        return items
    model = ai_config.get("MODEL", "")
    api_key = ai_config.get("API_KEY", "") or ""
    if not model or not api_key:
        logger.warning("AI 翻译跳过：未配置模型或密钥")
        return items

    try:
        from trendradar.ai.client import AIClient
    except Exception as e:  # noqa: BLE001
        logger.warning("AI 翻译跳过：无法导入 AIClient: %s", e)
        return items

    # 构造批量翻译请求（编号 + 原文标题）
    mapping = {}
    numbered = []
    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").strip()
        if not title:
            continue
        mapping[str(i)] = it
        numbered.append(f'"{i}": "{title}"')
    if not numbered:
        return items

    user_content = "{" + ", ".join(numbered) + "}"
    messages = [
        {
            "role": "system",
            "content": (
                "你是一名专业翻译。将用户提供的英文标题逐条翻译为简体中文。"
                "只输出一个 JSON 对象，键为数字编号，值为对应翻译，不要输出任何其他内容。"
            ),
        },
        {"role": "user", "content": user_content},
    ]

    try:
        # LiteLLM 要求 model 为 provider/model 格式；面板配置可能只有裸模型名
        # （如火山方舟 deepseek-v4-flash，配合 OpenAI 兼容 api_base），
        # 无前缀时按 OpenAI 兼容协议处理
        client_config = dict(ai_config)
        model = client_config.get("MODEL", "")
        if model and "/" not in model:
            client_config["MODEL"] = f"openai/{model}"
        # 火山方舟 OpenAI 兼容端点为 {base}/v3/chat/completions，
        # 面板配置的 base_url 可能省略 /v3（LiteLLM 拼接后 404），此处自动补全
        api_base = client_config.get("API_BASE", "") or ""
        if api_base and "volces.com" in api_base and "/v3" not in f"{api_base}/":
            client_config["API_BASE"] = api_base.rstrip("/") + "/v3"
        client = AIClient(client_config)
        raw = client.chat(messages, temperature=0.2, max_tokens=4096)
        # 提取 JSON（模型可能包裹 ```json ... ``` 或夹杂说明）
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError("响应中未找到 JSON")
        translated = json.loads(m.group(0))
    except Exception as e:  # noqa: BLE001
        logger.warning("AI 批量翻译失败（保留原文）: %s", str(e)[:150])
        return items

    # 回填翻译结果
    result = []
    for key, it in mapping.items():
        zh = (translated.get(key) or "").strip()
        if zh:
            new_item = {**it, "title": zh}
            extra = dict(it.get("extra") or {})
            extra["original_title"] = it["title"]
            new_item["extra"] = extra
            result.append(new_item)
        else:
            result.append(it)  # 该条未翻译成功，保留原文
    logger.info("AI 翻译完成: %d/%d 条", len([x for x in result if x.get("extra", {}).get("original_title")]), len(result))
    return result


def generate_chinese_summaries(
    items: List[Dict],
    ai_config: Dict,
    *,
    source_summary: bool = True,
    fallback_from_title: bool = True,
) -> List[Dict]:
    """
    当 AI 功能启用时，为条目生成/翻译简体中文摘要（写入 item["summary"]）。

    - source_summary=True：基于条目已有的 summary 字段（英文）翻译成中文（arXiv）
    - fallback_from_title=True：无 summary 时，基于标题生成一句话中文摘要（ai-news 等）
    摘要生成失败 / AI 未配置 / AI 关闭时，保留原样（不中断发布）。

    Args:
        items: 渠道条目列表
        ai_config: config["AI"]
        source_summary: 是否基于已有 summary 翻译
        fallback_from_title: 无 summary 时是否基于标题生成一句话摘要

    Returns:
        带中文摘要的条目列表（失败时等同原文）
    """
    if not items:
        return items
    if not ai_config.get("ENABLED"):
        return items
    model = ai_config.get("MODEL", "")
    api_key = ai_config.get("API_KEY", "") or ""
    if not model or not api_key:
        return items

    try:
        from trendradar.ai.client import AIClient
    except Exception as e:  # noqa: BLE001
        logger.warning("AI 摘要跳过：无法导入 AIClient: %s", e)
        return items

    # 构造批量请求：{n: {"t": 标题, "s": 原文摘要(可选)}}
    mapping = {}
    numbered = []
    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").strip()
        if not title:
            continue
        src = (it.get("summary") or "").strip()
        if not src and not fallback_from_title:
            continue
        mapping[str(i)] = it
        obj = {"t": title[:200]}
        if src and source_summary:
            obj["s"] = src[:400]
        numbered.append(f'"{i}": ' + json.dumps(obj, ensure_ascii=False))
    if not numbered:
        return items

    user_content = "{" + ", ".join(numbered) + "}"
    system = (
        "你是一名信息聚合编辑。对用户给出的每条内容，输出简体中文摘要。"
        "规则：若提供了原文摘要(s字段)，将其概括翻译为一段 1-2 句中文；"
        "若只有标题(t字段)，基于标题写一句中文简介。"
        '只输出一个 JSON 对象，键为数字编号，值为摘要字符串，不要输出其他内容。'
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]

    try:
        client_config = dict(ai_config)
        model = client_config.get("MODEL", "")
        if model and "/" not in model:
            client_config["MODEL"] = f"openai/{model}"
        api_base = client_config.get("API_BASE", "") or ""
        if api_base and "volces.com" in api_base and "/v3" not in f"{api_base}/":
            client_config["API_BASE"] = api_base.rstrip("/") + "/v3"
        client = AIClient(client_config)
        raw = client.chat(messages, temperature=0.2, max_tokens=8192)
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError("响应中未找到 JSON")
        summarized = json.loads(m.group(0))
    except Exception as e:  # noqa: BLE001
        logger.warning("AI 摘要生成失败（保留原文）: %s", str(e)[:150])
        return items

    result = []
    for key, it in mapping.items():
        zh = (summarized.get(key) or "").strip()
        if zh:
            new_item = {**it, "summary": zh}
            result.append(new_item)
        else:
            result.append(it)
    logger.info("AI 摘要完成: %d/%d 条", len([x for x in result if x.get("summary") and x.get("extra", {}).get("source") in ("arxiv", "hackernews")]), len(result))
    return result
