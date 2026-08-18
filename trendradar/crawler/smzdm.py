# coding=utf-8
"""
smzdm（什么值得买）数码好价抓取器

抓取 smzdm 电脑数码频道最近 N 小时内的好价（deal）信息，返回结构化条目。
smzdm 采用 IP 级 + 指纹级 JS 反爬（probev3.js），普通 requests 会被 202 挑战拦截，
因此本模块使用 Playwright（headless=new 模式）渲染真实页面绕过挑战。

设计约束：
- playwright 为可选依赖：未安装时返回空列表并打印提示，不影响 TrendRadar 主流程
- WAF 拦截（页面持续空白）时：自动 reload 重试，超过重试上限返回空列表 + 标记 blocked
- 时间过滤：按卡片时间文本（HH:MM=今日 / MM-DD=今年）解析，仅保留最近 max_age_hours 小时
"""

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# 默认数码频道好价页（电脑数码）
SMZDM_DIGITAL_URL = "https://www.smzdm.com/fenlei/diannaoshuma/h5c1s0f0t0p1/"

# 真实浏览器 UA（通过 WAF 的必要条件）
SMZDM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 启动参数：headless=new（新版 headless 更难被指纹识别）+ 关闭自动化标记
DEFAULT_LAUNCH_ARGS = [
    "--no-sandbox",
    "--headless=new",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
    "--disable-dev-shm-usage",
]

logger = logging.getLogger("trendradar.smzdm")


def _parse_card_time(text: str, now: datetime) -> Optional[datetime]:
    """
    解析 smzdm 卡片时间文本：
      - "08:50" → 今日 08:50
      - "08-17" → 今年 8 月 17 日 00:00
    无法解析返回 None
    """
    t = (text or "").strip()
    if not t:
        return None
    try:
        if re.fullmatch(r"\d{1,2}:\d{2}", t):
            hh, mm = t.split(":")
            return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if re.fullmatch(r"\d{1,2}-\d{1,2}", t):
            mm, dd = t.split("-")
            return now.replace(month=int(mm), day=int(dd), hour=0, minute=0, second=0, microsecond=0)
    except (ValueError, TypeError):
        return None
    return None


def _extract_cards(page) -> List[Dict]:
    """从渲染后的页面提取好价卡片"""
    items: List[Dict] = []
    for card in page.query_selector_all(".haojia-card-container"):
        try:
            link_el = card.query_selector('a[href*="smzdm.com/p/"]')
            title_el = card.query_selector(".title-normal-box")
            price_el = card.query_selector(".sub-title-box")
            img_el = card.query_selector(".product-img")
            time_el = card.query_selector('[class*="time"]')
            tags = [
                t.inner_text().strip()
                for t in card.query_selector_all(".tags-item")
            ]

            # 价格：优先 priceshow 属性（如 "3909.15元"），回退子标题文本
            priceshow = card.get_attribute("priceshow") or ""
            price_match = re.search(r"[\d.]+", priceshow.split("（")[0])
            price = price_match.group(0) if price_match else ""

            title = title_el.inner_text().strip() if title_el else ""
            url = link_el.get_attribute("href") if link_el else ""
            if not title or not url:
                continue

            items.append({
                "title": title,
                "url": url,
                "price": price,
                "price_text": price_el.inner_text().strip() if price_el else "",
                "image": img_el.get_attribute("src") if img_el else "",
                "time_text": time_el.inner_text().strip() if time_el else "",
                "tags": tags,
                "historical_low": any("历史低价" in tag for tag in tags),
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("提取 smzdm 卡片失败: %s", e)
            continue
    return items


def _warmup(page, homepage: str = "https://www.smzdm.com/") -> None:
    """
    预热：先访问首页建立会话（WAF 探测针对首访，首页通过后频道页通常直接可用）。
    失败不抛出，由后续主页面等待兜底。
    """
    try:
        page.goto(homepage, timeout=30000, wait_until="domcontentloaded")
        # 等待 probe 自行完成（它可能触发 reload），但只给有限时间
        for _ in range(6):
            page.wait_for_timeout(1500)
            try:
                if len(page.inner_text("body") or "") > 100:
                    break
            except Exception:  # noqa: BLE001
                break
    except Exception as e:  # noqa: BLE001
        logger.warning("smzdm 首页预热失败（忽略）: %s", str(e)[:100])


def _robust_goto(page, url: str, timeout_s: int = 120) -> bool:
    """
    访问 smzdm 页面并等待内容出现。

    WAF（probev3.js）探测阶段页面为空白，probe 执行完成后会自行 reload 到真实内容。
    策略：
      1. 前 20 秒不主动 reload（给 probe 时间自行完成）
      2. 之后若仍空白，每 8 秒主动 reload 一次
      3. 内容出现（好价卡片 / Tab 栏）即返回 True
    """
    page.goto(url, timeout=45000, wait_until="domcontentloaded")
    deadline = time.time() + timeout_s
    last_reload = 0.0
    first_pass = 20.0
    while time.time() < deadline:
        # 捕获页面被 reload 后 context 失效的异常
        try:
            body_len = len(page.inner_text("body") or "")
        except Exception:  # noqa: BLE001
            body_len = 0
        if page.query_selector(".haojia-card-container, .first-single-tab, .first-tabs"):
            return True
        elapsed = time.time() - deadline + timeout_s
        if body_len == 0 and elapsed > first_pass and time.time() - last_reload > 8:
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                last_reload = time.time()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(2)
    return False


def fetch_digital_deals(
    url: Optional[str] = None,
    max_age_hours: int = 24,
    max_items: int = 50,
    headless: bool = True,
    scroll_rounds: int = 6,
) -> Dict:
    """
    抓取 smzdm 电脑数码频道最近 max_age_hours 小时内的好价条目。

    Returns:
        {
            "ok": bool,            # 页面是否成功渲染（False 表示被 WAF 拦截）
            "items": List[Dict],   # 过滤后的条目
            "total_found": int,    # 页面实际渲染的卡片数
            "fetched_at": str,     # ISO 时间
            "channel_id": "smzdm-digital",
            "channel_name": "什么值得买·数码低价",
        }
    """
    url = url or SMZDM_DIGITAL_URL
    now = datetime.now()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("未安装 playwright，跳过 smzdm 抓取。安装: pip install playwright && playwright install chromium")
        return {
            "ok": False, "items": [], "total_found": 0,
            "fetched_at": now.isoformat(),
            "channel_id": "smzdm-digital", "channel_name": "什么值得买·数码低价",
            "error": "playwright 未安装",
        }

    # 外层重试：GitHub Actions 每次 run 可能落到不同出口 IP 段，
    # 部分 IP 段未被 smzdm 标记，重试可提高命中率
    max_attempts = 2
    cards: List[Dict] = []
    last_error = "smzdm 反爬拦截"
    for attempt in range(1, max_attempts + 1):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=headless,
                    args=DEFAULT_LAUNCH_ARGS,
                )
                ctx = browser.new_context(
                    user_agent=SMZDM_UA,
                    viewport={"width": 1280, "height": 2200},
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                )
                page = ctx.new_page()

                # 预热首页建立会话（首访探测在首页完成，频道页成功率更高）
                _warmup(page)

                loaded = _robust_goto(page, url)
                if not loaded:
                    logger.warning("smzdm 页面被 WAF 拦截（尝试 %d/%d），稍后重试", attempt, max_attempts)
                    browser.close()
                    if attempt < max_attempts:
                        time.sleep(3)
                        continue
                    return {
                        "ok": False, "items": [], "total_found": 0,
                        "fetched_at": now.isoformat(),
                        "channel_id": "smzdm-digital", "channel_name": "什么值得买·数码低价",
                        "error": "smzdm 反爬拦截",
                    }

                # 切换到「相关好价」Tab（好价卡片所在列表）
                try:
                    for tab in page.query_selector_all(".first-single-tab"):
                        if tab.inner_text().strip() == "相关好价":
                            tab.click()
                            break
                    page.wait_for_selector(".haojia-card-container", timeout=15000)
                except Exception:  # noqa: BLE001
                    pass

                # 滚动加载更多
                for _ in range(scroll_rounds):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(700)

                cards = _extract_cards(page)
                browser.close()
                break  # 成功，跳出重试
        except Exception as e:  # noqa: BLE001
            last_error = str(e)[:200]
            logger.warning("smzdm 抓取异常（尝试 %d/%d）: %s", attempt, max_attempts, last_error)
            if attempt < max_attempts:
                time.sleep(3)
                continue
            return {
                "ok": False, "items": [], "total_found": 0,
                "fetched_at": now.isoformat(),
                "channel_id": "smzdm-digital", "channel_name": "什么值得买·数码低价",
                "error": last_error,
            }

    # 时间过滤（最近 max_age_hours 小时）
    cutoff = now - timedelta(hours=max_age_hours)
    items: List[Dict] = []
    for card in cards:
        dt = _parse_card_time(card.get("time_text", ""), now)
        if dt is None or dt < cutoff:
            continue
        published_at = dt.isoformat()
        item = {
            "title": card["title"],
            "url": card["url"],
            "price": card["price"],
            "original_price": "",
            "discount": "",
            "image": card.get("image", ""),
            "published_at": published_at,
            "category": "数码",
            "historical_low": card.get("historical_low", False),
            "tags": card.get("tags", []),
            "extra": {},
        }
        items.append(item)
        if max_items > 0 and len(items) >= max_items:
            break

    return {
        "ok": True,
        "items": items,
        "total_found": len(cards),
        "fetched_at": now.isoformat(),
        "channel_id": "smzdm-digital",
        "channel_name": "什么值得买·数码低价",
    }
