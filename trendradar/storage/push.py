# coding=utf-8
"""
COS（对象存储）配置同步与存储推送

信息聚合面板与 TrendRadar 通过 COS 中间文件解耦交互，本模块负责 TrendRadar 侧的读写：

- trendradar/config/ai.json      读取：面板写入的 AI 配置（面板写，TrendRadar 读）
- trendradar/config/settings.json 读取：面板写入的 TrendRadar 设置（预留扩展）
- trendradar/push/latest.json    写入：存储推送渠道，将聚合结果发布给面板
- trendradar/push/history/...    写入：每日推送历史

复用 S3 兼容协议（boto3），与 remote storage 后端同一套 COS 配置与凭据。
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from trendradar.storage.remote import RemoteStorageBackend

logger = logging.getLogger("trendradar.storage.push")


def _normalize_boto3_endpoint(endpoint_url: str, bucket: str) -> str:
    """
    归一化 boto3 的 endpoint_url。

    fenix-desktop 共用 COS 配置的 endpoint 为 bucket-in-host 形式
    （如 https://fenix-desktop-1256215811.cos.ap-guangzhou.myqcloud.com），
    而 boto3 virtual 寻址会再前置 bucket，导致双重前缀。
    此处若 hostname 已以 bucket 开头，则剥离 bucket 前缀，交还 boto3 处理。
    """
    parsed = urlparse(endpoint_url)
    host = parsed.hostname or ""
    if bucket and host.startswith(bucket + "."):
        base_host = host[len(bucket) + 1:]
        return parsed._replace(netloc=base_host + (f":{parsed.port}" if parsed.port else "")).geturl()
    return endpoint_url

# 约定路径（与 fenix-desktop 信息聚合面板保持一致，勿单独修改）
AI_CONFIG_KEY = "trendradar/config/ai.json"
SETTINGS_KEY = "trendradar/config/settings.json"
PUSH_LATEST_KEY = "trendradar/push/latest.json"
PUSH_HISTORY_PREFIX = "trendradar/push/history"


class CosClient:
    """基于 S3 兼容协议的轻量 COS 客户端（读文本/写文本）"""

    def __init__(self, bucket: str, access_key_id: str, secret_access_key: str,
                 endpoint_url: str, region: str = ""):
        if not bucket or not access_key_id or not secret_access_key or not endpoint_url:
            raise ValueError("COS 配置不完整，无法创建客户端")
        self.bucket = bucket
        self.region = region
        # 复用 RemoteStorageBackend 的 boto3 客户端创建逻辑（含 COS SigV2 适配）
        self._backend = RemoteStorageBackend(
            bucket_name=bucket,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            endpoint_url=_normalize_boto3_endpoint(endpoint_url, bucket),
            region=region,
            enable_html=False,
            temp_dir=None,
        )
        self.s3_client = self._backend.s3_client

    def read_text(self, key: str) -> Tuple[Optional[str], Optional[str]]:
        """读取对象文本，返回 (text, error)；对象不存在返回 (None, None)"""
        try:
            resp = self.s3_client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read().decode("utf-8", errors="replace"), None
        except Exception as e:  # noqa: BLE001
            if getattr(e, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None, None
            return None, str(e)[:200]

    def read_json(self, key: str) -> Tuple[Optional[Dict], Optional[str]]:
        """读取 JSON 对象，返回 (dict, error)；缺失或解析失败返回默认处理"""
        text, err = self.read_text(key)
        if text is None:
            return None, err
        try:
            return json.loads(text), None
        except json.JSONDecodeError as e:
            return None, f"JSON 解析失败: {e}"

    def write_text(self, key: str, text: str) -> Optional[str]:
        """写入对象文本，成功返回 None，失败返回错误信息"""
        try:
            self.s3_client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=text.encode("utf-8"),
                ContentType="application/json; charset=utf-8",
            )
            return None
        except Exception as e:  # noqa: BLE001
            return str(e)[:200]


def build_cos_client(config: Dict) -> Optional[CosClient]:
    """从 TrendRadar 配置构建 COS 客户端（remote storage 配置或环境变量）"""
    remote = config.get("STORAGE", {}).get("REMOTE", {})
    bucket = remote.get("BUCKET_NAME", "") or ""
    access_key = remote.get("ACCESS_KEY_ID", "") or ""
    secret_key = remote.get("SECRET_ACCESS_KEY", "") or ""
    endpoint = remote.get("ENDPOINT_URL", "") or ""
    region = remote.get("REGION", "") or ""
    if not (bucket and access_key and secret_key and endpoint):
        return None
    try:
        return CosClient(bucket, access_key, secret_key, endpoint, region)
    except Exception as e:  # noqa: BLE001
        logger.warning("构建 COS 客户端失败: %s", e)
        return None


def apply_remote_config_overrides(config: Dict) -> None:
    """
    从 COS 读取面板写入的配置并覆盖 TrendRadar 运行时配置（就地修改 config 字典）。

    - trendradar/config/ai.json → config["AI"]（enabled/api_key/base_url/model）
    - trendradar/config/settings.json → config["AGGREGATION_SETTINGS"]（预留）

    优先级：环境变量（工作流显式设置）> COS 面板配置 > config.yaml
    仅当 remote storage 已配置且文件存在时生效。
    """
    client = build_cos_client(config)
    if client is None:
        return

    # AI 配置
    ai, err = client.read_json(AI_CONFIG_KEY)
    if ai is None:
        if err:
            logger.warning("读取 COS AI 配置失败: %s", err)
        return

    env = __import__("os").environ
    current = config.get("AI", {})
    model = ai.get("model") or current.get("MODEL", "")
    api_key = ai.get("api_key") or current.get("API_KEY", "")
    api_base = ai.get("base_url") or current.get("API_BASE", "")
    enabled = ai.get("enabled", current.get("ENABLED", False))

    config["AI"] = {
        **current,
        "MODEL": env.get("AI_MODEL") or model,
        "API_KEY": env.get("AI_API_KEY") or api_key,
        "API_BASE": env.get("AI_API_BASE") or api_base,
        "ENABLED": env.get("AI_ANALYSIS_ENABLED") if env.get("AI_ANALYSIS_ENABLED") else enabled,
    }
    logger.info("已从 COS 读取 AI 配置（model=%s, base=%s, enabled=%s）", model or "-", api_base or "-", enabled)

    # TrendRadar 设置（预留扩展）
    settings, serr = client.read_json(SETTINGS_KEY)
    if settings is not None:
        config["AGGREGATION_SETTINGS"] = settings
    elif serr:
        logger.warning("读取 COS settings.json 失败: %s", serr)


def publish_latest(config: Dict, payload: Dict) -> Optional[str]:
    """
    存储推送渠道：将聚合结果 payload 写入 COS trendradar/push/latest.json，
    并按天追加写入历史文件。成功返回 None，失败返回错误信息。
    """
    client = build_cos_client(config)
    if client is None:
        return "未配置远程存储（COS），无法写入存储推送"
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as e:
        return f"payload 序列化失败: {e}"

    err = client.write_text(PUSH_LATEST_KEY, text)
    if err:
        return f"写入 latest.json 失败: {err}"

    # 每日历史（合并当日已有文件 + 本次推送）
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    history_key = f"{PUSH_HISTORY_PREFIX}/{day}.json"
    existing, herr = client.read_json(history_key)
    if herr and existing is None and herr != "JSON 解析失败":
        logger.warning("读取历史文件失败: %s", herr)
    # 简单策略：按 channel id 覆盖当日该渠道数据
    if existing is not None:
        try:
            existing_channels = {c["id"]: c for c in existing.get("channels", [])}
        except (TypeError, KeyError, AttributeError):
            existing_channels = {}
        for ch in payload.get("channels", []):
            existing_channels[ch["id"]] = ch
        merged = {"version": 1, "updated_at": payload.get("pushed_at"), "channels": list(existing_channels.values())}
        err = client.write_text(history_key, json.dumps(merged, ensure_ascii=False, indent=2))
    else:
        err = client.write_text(history_key, text)
    if err:
        logger.warning("写入历史文件失败: %s", err)

    logger.info("存储推送完成: %s (%d 条)", PUSH_LATEST_KEY, len(payload.get("channels", [])))
    return None
