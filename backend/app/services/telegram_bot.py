"""Telegram 机器人轮询服务 —— long-polling 收命令 + 单用户白名单鉴权 + 分发执行。

职责:
  - 后台守护线程内跑独立 asyncio loop, 用 getUpdates 长轮询拉取用户消息(出网即可, 零暴露面)。
  - chat_id 白名单鉴权: 只有授权用户的消息才执行; 未授权只回显其 chat_id 供用户填入设置。
  - 分发: `/xxx` 结构化命令走 COMMANDS 表; 其余自由文本走 NL agent 路由。
  - 每条消息独立 asyncio task 处理 —— 慢命令(AI 分析)不阻塞轮询主循环。
  - 单条命令的任何异常都被吞掉并回给用户, 轮询循环永不因单条消息崩溃。

线程模型对齐 quote_service / ext_pull: daemon 线程 + 自持 event loop, start()/stop() 由 lifespan 调用。

配置来源(热读, 不缓存, 便于设置页改动即时生效):
  - token: secrets_store.get_telegram_token()
  - enabled: preferences.get_telegram_enabled()
  - 白名单: preferences.get_telegram_allowed_chat_ids()
"""
from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)


class TelegramBotService:
    """Telegram 长轮询机器人服务(单例, 挂在 app.state)。"""

    # getUpdates 的 long-poll 挂起秒数(Telegram 侧)。
    _POLL_TIMEOUT = 30

    def __init__(self) -> None:
        self._app_state: object | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._offset: int | None = None
        self._lock = threading.Lock()
        # 正在处理中的消息 task, 便于 stop 时不悬挂(弱引用集合)
        self._tasks: set[asyncio.Task] = set()

    # ── 生命周期 ─────────────────────────────────────────

    def set_app_state(self, app_state: object) -> None:
        self._app_state = app_state

    def start(self) -> bool:
        """按配置启动轮询。未启用 / 无 token 时不启动, 返回是否真正启动。

        推送(告警/复盘)不依赖本服务, 故未启动也不影响推送通道。
        """
        from app import secrets_store
        from app.services import preferences

        if not preferences.get_telegram_enabled():
            logger.info("telegram bot: 未启用, 跳过轮询启动")
            return False
        token = secrets_store.get_telegram_token()
        if not token:
            logger.info("telegram bot: 未配置 token, 跳过轮询启动")
            return False

        with self._lock:
            if self._running:
                return True
            self._running = True
            self._thread = threading.Thread(
                target=self._thread_main, name="telegram-bot", daemon=True,
            )
            self._thread.start()
        logger.info("telegram bot: 轮询已启动")
        return True

    def stop(self) -> None:
        """停止轮询(供 lifespan shutdown / 重配时调用)。"""
        with self._lock:
            if not self._running:
                return
            self._running = False
        loop = self._loop
        if loop is not None:
            # 唤醒可能阻塞在 sleep 的 loop, 让它尽快看到 _running=False
            try:
                loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:
                pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=self._POLL_TIMEOUT + 15)
        self._thread = None
        logger.info("telegram bot: 轮询已停止")

    def restart(self) -> bool:
        """配置变更后重启轮询(设置页改 token / 开关后调用)。"""
        self.stop()
        return self.start()

    def is_running(self) -> bool:
        return self._running

    # ── 线程主体 ─────────────────────────────────────────

    def _thread_main(self) -> None:
        """守护线程入口: 建独立 event loop 跑轮询协程。"""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._poll_loop())
        except Exception as e:  # noqa: BLE001
            logger.warning("telegram bot 线程异常退出: %s", e)
        finally:
            try:
                if self._loop is not None:
                    self._loop.close()
            except Exception:  # noqa: BLE001
                pass
            self._loop = None

    async def _poll_loop(self) -> None:
        """长轮询主循环: 拉更新 → 每条消息派发独立 task。"""
        from app import secrets_store
        from app.services import telegram_adapter

        # 启动时先把积压更新的 offset 快进到最新, 避免重放历史消息(如重启前的旧命令)。
        await self._drain_backlog()

        idle_backoff = 3.0  # 连续网络失败时的退避上限起点
        while self._running:
            token = secrets_store.get_telegram_token()
            if not token:
                await asyncio.sleep(5)
                continue
            try:
                updates = await asyncio.to_thread(
                    telegram_adapter.get_updates,
                    token, self._offset, self._POLL_TIMEOUT,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("telegram getUpdates 异常: %s", e)
                updates = None

            if updates is None:
                # 网络/接口异常: 退避后重试, 不打爆日志
                await asyncio.sleep(min(idle_backoff, 15.0))
                idle_backoff = min(idle_backoff * 1.5, 15.0)
                continue
            idle_backoff = 3.0

            for upd in updates:
                try:
                    uid = upd.get("update_id")
                    if isinstance(uid, int):
                        self._offset = uid + 1  # 确认: 下次从该 id 之后拉
                    self._handle_update(upd)
                except Exception as e:  # noqa: BLE001
                    logger.debug("处理 update 失败: %s", e)

        # 退出前尽量等在途消息处理完
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            try:
                await asyncio.wait(pending, timeout=10)
            except Exception:  # noqa: BLE001
                pass

    async def _drain_backlog(self) -> None:
        """快进 offset 到最新, 丢弃启动前积压的旧消息。"""
        from app import secrets_store
        from app.services import telegram_adapter

        token = secrets_store.get_telegram_token()
        if not token:
            return
        try:
            # timeout=0 立即返回当前积压
            updates = await asyncio.to_thread(
                telegram_adapter.get_updates, token, None, 0,
            )
            if updates:
                last = updates[-1].get("update_id")
                if isinstance(last, int):
                    self._offset = last + 1
                    logger.info("telegram bot: 丢弃 %d 条积压消息", len(updates))
        except Exception as e:  # noqa: BLE001
            logger.debug("drain backlog 失败: %s", e)

    def _handle_update(self, upd: dict) -> None:
        """把单条 update 解析出 (chat_id, text), 派发到独立 task 异步处理。"""
        msg = upd.get("message") or upd.get("edited_message")
        if not isinstance(msg, dict):
            return
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text:
            return

        loop = self._loop
        if loop is None:
            return
        task = loop.create_task(self._process_message(str(chat_id), text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── 消息处理 ─────────────────────────────────────────

    async def _process_message(self, chat_id: str, text: str) -> None:
        """鉴权 → 分发 → 回复。任何异常都吞掉并回给用户可读提示。"""
        from app import secrets_store
        from app.services import telegram_adapter

        token = secrets_store.get_telegram_token()
        if not token:
            return

        try:
            reply = await self._route(chat_id, text)
        except Exception as e:  # noqa: BLE001
            logger.warning("telegram 消息处理失败: %s", e)
            reply = f"⚠️ 处理出错: {e}"

        if reply:
            await asyncio.to_thread(
                telegram_adapter.send_telegram, token, chat_id, reply,
            )

    async def _route(self, chat_id: str, text: str) -> str:
        """鉴权 + 分发。返回要回复的消息文本。"""
        from app.services import preferences
        from app.services.telegram_agent import route_and_run
        from app.services.telegram_commands import CommandContext, dispatch_command

        allowed = preferences.get_telegram_allowed_chat_ids()

        # 白名单为空: 引导用户把自己的 chat_id 填进设置(onboarding)。
        if not allowed:
            return (
                "🔒 机器人尚未授权任何用户。\n"
                f"你的 chat_id 是: <code>{chat_id}</code>\n"
                "把它填入「设置 → 通知 → Telegram」的授权列表后即可使用。"
            )

        # 非白名单用户: 拒绝(不泄露任何能力, 单用户安全边界)。
        if chat_id not in allowed:
            logger.info("telegram: 拒绝未授权 chat_id=%s", chat_id)
            return "🚫 未授权。请联系管理员把你的 chat_id 加入白名单。"

        ctx = CommandContext(app_state=self._app_state)

        # 结构化命令: 以 / 开头
        if text.startswith("/"):
            head, _, rest = text[1:].partition(" ")
            # 去掉 @botname 后缀(群里 /cmd@bot 形式)
            head = head.split("@", 1)[0].strip().lower()
            result = await dispatch_command(ctx, head, rest.strip())
            if result is not None:
                return result
            # 未知斜杠命令 → 交给 NL agent 兜底(可能是自然语言误带了 /)
            return await route_and_run(ctx, text.lstrip("/"))

        # 自由文本: 走 NL agent 路由
        return await route_and_run(ctx, text)
