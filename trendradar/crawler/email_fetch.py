# coding=utf-8
"""
信息聚合面板 · 电子邮件渠道抓取器（服务端 IMAP，无浏览器依赖）

依据 fenix-desktop 契约（docs/aggregation-email-channel.md）从邮箱账号抓取
最新未读邮件，作为聚合面板的 email 渠道数据源：
- 账号凭据来自面板写入的 COS trendradar/config/settings.json（channels.email.accounts）
- 每账号最多抓取 limit 封未读（默认 10），只读模式 + BODY.PEEK，不标记已读
- 条目契约：{title: 主题, url: "", published_at: ISO8601, extra: {account, account_label, from}}

设计约束（与 crawler/channels.py 一致）：
- 仅用标准库 imaplib/email，无新增依赖
- 单账号失败不影响其他账号（各自 try/except）
- 时间字段统一为 ISO 8601 字符串，供面板展示
"""

import imaplib
import logging
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("trendradar.email_fetch")

# 邮箱服务商 IMAP 预设（provider → (host, port)）
PROVIDER_PRESETS: Dict[str, Tuple[str, int]] = {
    "gmail": ("imap.gmail.com", 993),
    "qq": ("imap.qq.com", 993),
}

# 账号显示名（latest.json 契约 extra.account_label）
ACCOUNT_LABELS: Dict[str, str] = {
    "gmail": "Gmail",
    "qq": "QQ邮箱",
}

# IMAP 连接超时（秒）
IMAP_CONNECT_TIMEOUT = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_header(value: Optional[str]) -> str:
    """解码 MIME 编码（RFC 2047）的主题/发件人；无值返回空串"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return str(value)


def _parse_date_iso(value: Optional[str]) -> str:
    """将 RFC 2822 日期头转为 ISO 8601（含时区）；无/无效返回空串"""
    if not value:
        return ""
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return ""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().isoformat()


def _fetch_unread_from_server(
    host: str,
    port: int,
    address: str,
    pass_: str,
    limit: int,
) -> List[Dict]:
    """
    单账号 IMAP 抓取最新未读邮件（只读，不标记已读）。

    Args:
        host: IMAP 服务器地址
        port: IMAP 端口（SSL）
        address: 邮箱地址
        pass_: 应用专用密码 / 授权码
        limit: 最多返回封数

    Returns:
        items 列表（契约结构），可能为空
    """
    items: List[Dict] = []
    conn = imaplib.IMAP4_SSL(host, port, timeout=IMAP_CONNECT_TIMEOUT)
    try:
        conn.login(address, pass_)
        # readonly=True：INBOX 只读打开，配合 BODY.PEEK 确保不改变邮件状态
        typ, _ = conn.select("INBOX", readonly=True)
        if typ != "OK":
            logger.warning("[email] 选择 INBOX 失败: %s", address)
            return items

        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return items
        # 服务端返回的 message id 按序递增，取末尾 limit 个即最新
        ids = data[0].split()
        latest_ids = ids[-limit:]

        for mid in latest_ids:
            try:
                # BODY.PEEK 不改变 \Seen 标志
                typ, msg_data = conn.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if typ != "OK" or not msg_data or msg_data[0] is None:
                    continue
                raw = msg_data[0][1]
                if not raw:
                    continue
                # 用 email.parser 解析头部
                import email as email_mod
                from email.parser import BytesHeaderParser

                msg = BytesHeaderParser().parsebytes(raw)
                subject = _decode_header(msg.get("Subject", ""))
                frm = _decode_header(msg.get("From", ""))
                published_at = _parse_date_iso(msg.get("Date", ""))
                if not subject:
                    subject = "(无主题)"
                items.append(
                    {
                        "title": subject,
                        "url": "",
                        "published_at": published_at,
                        "extra": {
                            "account": "",
                            "account_label": "",
                            "from": frm,
                        },
                    }
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[email] 解析单封邮件失败（%s）: %s", address, e)
                continue
        return items
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def fetch_unread_emails(
    provider: str,
    address: str,
    pass_: str,
    limit: int = 10,
) -> Dict:
    """
    抓取指定账号最新未读邮件（对外入口，返回契约结构）。

    Args:
        provider: 服务商标识（gmail / qq）
        address: 邮箱地址
        pass_: 应用专用密码 / 授权码
        limit: 最多返回封数（默认 10）

    Returns:
        {"ok": bool, "items": List[Dict], "fetched_at": str, "error": str}
    """
    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        return {
            "ok": False,
            "items": [],
            "fetched_at": _now_iso(),
            "error": f"不支持的邮箱服务商: {provider}",
        }
    host, port = preset
    try:
        items = _fetch_unread_from_server(host, port, address, pass_, limit)
    except imaplib.IMAP4.error as e:
        logger.warning("[email] 登录/抓取失败（%s）: %s", address, e)
        return {"ok": False, "items": [], "fetched_at": _now_iso(), "error": str(e)[:150]}
    except Exception as e:  # noqa: BLE001
        logger.warning("[email] 抓取异常（%s）: %s", address, e)
        return {"ok": False, "items": [], "fetched_at": _now_iso(), "error": str(e)[:150]}

    # 回填账号标识（契约字段）
    label = ACCOUNT_LABELS.get(provider, provider)
    for it in items:
        it["extra"]["account"] = provider
        it["extra"]["account_label"] = label
    return {"ok": True, "items": items, "fetched_at": _now_iso(), "error": ""}
