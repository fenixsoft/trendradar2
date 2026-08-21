# coding=utf-8
"""
信息聚合渠道抓取器（服务端轻量数据源，无浏览器依赖）

提供信息聚合面板所需的扩展渠道：
- RSS 渠道（每个 RSS 订阅源作为面板的一个独立渠道）
- 水木社区每日十大热门话题（官方 rss/topten）
- InfoQ 技术热点（infoq.cn 官方 RSS）
- 电子邮件渠道（IMAP 抓取最新未读邮件，见 email_fetch.py）

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


def _parse_rss_items(xml_text: str) -> List[Dict]:
    """解析 RSS 2.0 条目"""
    items: List[Dict] = []
    root = ET.fromstring(xml_text)
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title and link:
            items.append({"title": title, "url": link, "published_at": pub, "category": "", "extra": {"source": "rss"}})
    return items


# ============================================================
# 1. 通用 RSS 渠道（aggregation.storage_push.rss_channels）
#
# 每个 RSS 订阅源作为信息聚合面板的一个独立渠道。使用 TrendRadar 的
# RSSParser（feedparser），支持 RSS 2.0 / Atom / JSON Feed 三种格式。
# ============================================================
def fetch_rss_feed(url: str, max_items: int = 20) -> Dict:
    """
    抓取单个 RSS/Atom/JSON Feed 源，返回面板渠道格式条目。

    Args:
        url: 订阅地址
        max_items: 最多返回条数

    Returns:
        {"ok": bool, "items": List[Dict], "fetched_at": str, "error": str}
        item: {"title", "url", "published_at"(ISO), "category", "summary", "extra"}
    """
    try:
        from trendradar.crawler.rss import RSSParser
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        parsed = RSSParser().parse(resp.text, url)
    except Exception as e:  # noqa: BLE001
        logger.warning("RSS 渠道抓取失败 (%s): %s", url, e)
        return {"ok": False, "items": [], "fetched_at": _now_iso(), "error": str(e)[:150]}

    items: List[Dict] = []
    for it in parsed[:max_items]:
        items.append({
            "title": it.title,
            "url": it.url,
            "published_at": it.published_at or "",
            "category": "",
            "summary": it.summary or "",
            "extra": {"source": "rss"},
        })
    return {"ok": True, "items": items, "fetched_at": _now_iso(), "error": ""}


# ============================================================
# 2. 水木社区每日十大热门话题
# ============================================================
# 官方十大热门 RSS：含标题、文章链接、时间，一次请求（GB2312 编码）
SMTH_TOPTEN_RSS_URL = "https://www.newsmth.net/nForum/rss/topten"
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
    抓取水木社区每日十大热门话题（官方 rss/topten）。

    RSS 直接含标题、文章链接、发布时间（GB2312 编码），无需额外匹配。

    Returns:
        {"ok": bool, "items": List[Dict], "fetched_at": str, "error": str}
        item: {"title", "url", "published_at"(ISO), "category", "rank", "extra"}
    """
    try:
        resp = requests.get(SMTH_TOPTEN_RSS_URL, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        xml_text = resp.content.decode("gb2312", errors="replace")
        all_items = _parse_rss_items(xml_text)
    except Exception as e:  # noqa: BLE001
        logger.warning("水木每日热门抓取失败: %s", e)
        return {"ok": False, "items": [], "fetched_at": _now_iso(), "error": str(e)[:150]}

    # pubDate(RFC 822) → ISO 8601，供面板统一解析
    from email.utils import parsedate_to_datetime
    items = []
    for i, it in enumerate(all_items[:max_items], 1):
        pub = it.get("published_at", "")
        iso = pub
        if pub:
            try:
                iso = parsedate_to_datetime(pub).astimezone().isoformat()
            except Exception:  # noqa: BLE001
                pass
        items.append({
            "title": it["title"],
            "url": it["url"],
            "published_at": iso,
            "rank": i,
            "category": "水木",
            "extra": {"source": "newsmth"},
        })
    return {"ok": True, "items": items, "fetched_at": _now_iso(), "error": ""}


# ============================================================
# 3. InfoQ 技术热点（infoq.cn RSS feed，服务端可访问）
#
# 注：infoq.cn 的「7日热门」排行 API 对数据中心 IP 返回 451（被拦截），
#     服务端不可直接抓取；此处使用 infoq.cn 官方 RSS feed（最新技术资讯，
#     服务端 200 可访问）作为替代渠道。
# ============================================================
INFOQ_FEED_URL = "https://www.infoq.cn/feed"


def fetch_infoq_hot(max_items: int = 20) -> Dict:
    """
    抓取 InfoQ 技术资讯（infoq.cn RSS feed）。

    Returns:
        {"ok": bool, "items": List[Dict], "fetched_at": str, "error": str}
        item: {"title", "url", "published_at", "category", "extra"}
    """
    try:
        resp = requests.get(INFOQ_FEED_URL, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        all_items = _parse_rss_items(resp.text)
    except Exception as e:  # noqa: BLE001
        logger.warning("InfoQ 抓取失败: %s", e)
        return {"ok": False, "items": [], "fetched_at": _now_iso(), "error": str(e)[:150]}

    items = []
    for it in all_items[:max_items]:
        it["category"] = "InfoQ"
        it["extra"] = {"source": "infoq"}
        items.append(it)
    return {"ok": True, "items": items, "fetched_at": _now_iso(), "error": ""}


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
    max_chars: int = 100,
    chunk_size: int = 20,
) -> List[Dict]:
    """
    当 AI 功能启用时，为条目生成/翻译简体中文摘要（写入 item["summary"]）。

    - source_summary=True：基于条目已有的 summary 字段（英文）翻译成中文（arXiv）
    - fallback_from_title=True：无 summary 时，基于标题撰写中文简介（ai-news 等）
    - max_chars：摘要目标长度（约 100 字），提示词中约束
    - chunk_size：分批调用 LLM 的大小，避免大批量输出超限截断
    摘要生成失败 / AI 未配置 / AI 关闭时，保留原样（不中断发布）。

    Args:
        items: 渠道条目列表
        ai_config: config["AI"]
        source_summary: 是否基于已有 summary 翻译
        fallback_from_title: 无 summary 时是否基于标题生成摘要
        max_chars: 摘要目标字数
        chunk_size: 每批条目数

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

    client_config = dict(ai_config)
    model = client_config.get("MODEL", "")
    if model and "/" not in model:
        client_config["MODEL"] = f"openai/{model}"
    api_base = client_config.get("API_BASE", "") or ""
    if api_base and "volces.com" in api_base and "/v3" not in f"{api_base}/":
        client_config["API_BASE"] = api_base.rstrip("/") + "/v3"
    client = AIClient(client_config)

    import time as _time
    _t0 = _time.time()

    # 汇总表：序号 -> (item, 请求对象)
    entries = []
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        src = (it.get("summary") or "").strip()
        if not src and not fallback_from_title:
            continue
        obj = {"t": title[:200]}
        if src and source_summary:
            obj["s"] = src[:600]  # 提供更充分原文，支撑 100 字摘要
        entries.append((it, obj))
    if not entries:
        return items

    # 系统提示：摘要目标约 max_chars 字
    system = (
        "你是一名信息聚合编辑。对用户给出的每条内容，输出简体中文摘要。"
        f"规则：每条摘要控制在约 {max_chars} 字（最少 90 字、最多 120 字），"
        "信息要具体、避免空话；若提供了原文摘要(s字段)，基于原文概括翻译；"
        "若只有标题(t字段)，基于标题撰写有实质内容的简介。"
        '只输出一个 JSON 对象，键为数字编号，值为摘要字符串，不要输出其他内容。'
    )

    result_map = {}
    for start in range(0, len(entries), chunk_size):
        chunk = entries[start:start + chunk_size]
        numbered = []
        for offset, (it, obj) in enumerate(chunk, 1):
            numbered.append(f'"{offset}": ' + json.dumps(obj, ensure_ascii=False))
        user_content = "{" + ", ".join(numbered) + "}"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]
        try:
            raw = client.chat(messages, temperature=0.3, max_tokens=8192)
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                raise ValueError("响应中未找到 JSON")
            summarized = json.loads(m.group(0))
        except Exception as e:  # noqa: BLE001
            logger.warning("AI 摘要分批 %d 失败（该批保留原文）: %s", start // chunk_size + 1, str(e)[:150])
            continue
        for offset, (it, _obj) in enumerate(chunk, 1):
            zh = (summarized.get(str(offset)) or "").strip()
            if zh:
                result_map[id(it)] = zh

    result = []
    for it, _obj in entries:
        zh = result_map.get(id(it))
        if zh:
            result.append({**it, "summary": zh})
        else:
            result.append(it)
    elapsed = _time.time() - _t0
    logger.info("AI 摘要完成: %d/%d 条，耗时 %.1f 秒（分批 %d 条/批）", len(result_map), len(entries), elapsed, chunk_size)
    return result
