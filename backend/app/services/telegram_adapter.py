"""Telegram 推送 / 收发适配器 — 把后端事件推送到 Telegram, 并读取用户指令。

职责: 与 Telegram Bot API 的最薄封装 (纯 httpx, 无第三方 SDK, 对齐 webhook_adapter 的风格)。
     只做「发消息 / 收消息 / 校验 token」三件事, 不含业务逻辑与轮询循环 (那在 telegram_bot.py)。

为什么用 long-polling (getUpdates) 而非 webhook:
  - 自托管单容器, 通常无公网域名 / HTTPS 证书, 配 webhook 门槛高。
  - getUpdates 主动拉取, 出网即可, 零暴露面。与项目「本机 / 内网优先」的定位一致。

设计: 推送失败静默降级, 绝不因推送失败阻断主流程 (与 webhook_adapter / notify_adapter 一致)。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Telegram 单条消息上限 4096 字符 (UTF-16 码元, 这里按 Python 字符保守切分)。
_MAX_LEN = 4000

_API_BASE = "https://api.telegram.org"
_TELEGRAM_BOT_URL_RE = re.compile(r"(https://api\.telegram\.org/bot)(\d+):([^\s/?]+)")


def _method_url(token: str, method: str) -> str:
    return f"{_API_BASE}/bot{token}/{method}"


def mask_telegram_token(text: str) -> str:
    """Redact Telegram bot tokens that appear inside Bot API URLs."""
    if not text:
        return text
    return _TELEGRAM_BOT_URL_RE.sub(r"\1\2:***", text)


class TelegramTokenMaskingFilter(logging.Filter):
    """Sanitize Telegram bot tokens before records are formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        masked = mask_telegram_token(message)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


def is_valid_token_shape(token: str) -> bool:
    """粗校验 token 形状 (<digits>:<alnum...>)。真正有效性由 get_me 联网确认。"""
    if not token or ":" not in token:
        return False
    head, _, tail = token.partition(":")
    return head.isdigit() and len(tail) >= 20


def split_message(text: str, limit: int = _MAX_LEN) -> list[str]:
    """把长文本切成 <=limit 的多段, 优先在换行处断开, 避免撑破 Telegram 上限。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        # 优先在最后一个换行处断开, 其次硬切
        cut = window.rfind("\n")
        if cut < limit // 2:  # 换行太靠前, 宁可硬切保证每段够满
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def get_me(token: str, timeout: float = 8.0) -> dict | None:
    """校验 token 并返回机器人信息 (username 等)。失败返回 None。"""
    if not is_valid_token_shape(token):
        return None
    try:
        import httpx

        resp = httpx.get(_method_url(token, "getMe"), timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("ok"):
                return data.get("result") or {}
        logger.debug("telegram getMe HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:  # noqa: BLE001
        logger.debug("telegram getMe 失败: %s", e)
    return None


def get_updates(token: str, offset: int | None = None, timeout: int = 30) -> list[dict] | None:
    """long-polling 拉取更新。返回 update 列表 (可能为空), 网络/接口异常返回 None。

    timeout 为 Telegram 侧的 long-poll 挂起秒数; httpx 读超时取其 + 余量。
    """
    if not token:
        return None
    params: dict = {"timeout": timeout, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
    try:
        import httpx

        resp = httpx.get(
            _method_url(token, "getUpdates"),
            params=params,
            timeout=timeout + 10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("ok"):
                result = data.get("result")
                return result if isinstance(result, list) else []
        logger.debug("telegram getUpdates HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:  # noqa: BLE001
        logger.debug("telegram getUpdates 失败: %s", e)
    return None


def send_telegram(
    token: str,
    chat_id: str | int,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    timeout: float = 8.0,
) -> bool:
    """推送一条文本消息到指定 chat。超长自动分段发送。

    Args:
        token:      Bot token
        chat_id:    目标会话 id (用户 / 群)
        text:       消息正文 (parse_mode=HTML 时可含少量 HTML 标签)
        parse_mode: "HTML" / "Markdown" / None (纯文本)。默认 HTML。

    Returns:
        True=全部分段成功送达, False=任一段失败或参数非法。
        失败静默, 不抛异常 (推送是辅助通道, 不能阻断主流程)。
    """
    if not token or chat_id in (None, ""):
        return False
    segments = split_message(text)
    if not segments:
        return False

    ok_all = True
    for seg in segments:
        if not _post_message(token, chat_id, seg, parse_mode, timeout):
            ok_all = False
    return ok_all


def broadcast(text: str, *, parse_mode: str | None = "HTML") -> int:
    """把一条消息推送给所有已授权 chat_id(复用监控/复盘的统一推送入口)。

    从 secrets_store 读 token、preferences 读白名单; token 或白名单缺失则跳过。
    返回成功送达的 chat 数。失败静默(推送是辅助通道, 不阻断主流程)。
    """
    try:
        from app import secrets_store
        from app.services import preferences

        token = secrets_store.get_telegram_token()
        if not token:
            return 0
        chat_ids = preferences.get_telegram_allowed_chat_ids()
        if not chat_ids:
            return 0
        sent = 0
        for cid in chat_ids:
            if send_telegram(token, cid, text, parse_mode=parse_mode):
                sent += 1
        return sent
    except Exception as e:  # noqa: BLE001
        logger.debug("telegram broadcast 失败: %s", e)
        return 0


def _post_message(
    token: str,
    chat_id: str | int,
    text: str,
    parse_mode: str | None,
    timeout: float,
) -> bool:
    """发送单条消息。HTML 解析失败时自动降级为纯文本重试一次。"""
    try:
        import httpx

        payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        resp = httpx.post(_method_url(token, "sendMessage"), json=payload, timeout=timeout)
        if resp.status_code == 200:
            return True
        # 400 多半是 HTML/Markdown 标签不合法 → 退回纯文本再试一次
        if resp.status_code == 400 and parse_mode:
            payload.pop("parse_mode", None)
            retry = httpx.post(_method_url(token, "sendMessage"), json=payload, timeout=timeout)
            return retry.status_code == 200
        logger.debug("telegram sendMessage HTTP %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:  # noqa: BLE001
        logger.debug("telegram sendMessage 失败: %s", e)
        return False
