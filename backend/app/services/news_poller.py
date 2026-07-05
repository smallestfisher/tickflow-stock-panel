"""市场快讯轮询服务 —— 独立 daemon 线程持续抓财联社电报入库。

线程模型对齐 telegram_bot / quote_service: daemon 线程 + 自持 event loop,
start()/stop()/restart() 由 lifespan 与设置页调用。

配置热读 (不缓存, 便于设置页改动即时生效):
  - enabled:  preferences.get_news_poll_enabled()  (默认关)
  - interval: preferences.get_news_poll_interval()  (默认 300s)

设计: 单轮抓取失败静默退避, 循环永不崩; 未启用时 start() 内部跳过不影响主启动。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class NewsPollerService:
    """快讯轮询服务 (单例, 挂 app.state)。"""

    def __init__(self) -> None:
        self._db_path: Path | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._lock = threading.Lock()

    def set_db_path(self, db_path: Path) -> None:
        self._db_path = db_path

    # ── 生命周期 ─────────────────────────────────────────

    def start(self) -> bool:
        """按配置启动轮询。未启用时跳过, 返回是否真正启动。"""
        from app.services import news_store, preferences

        if not preferences.get_news_poll_enabled():
            logger.info("news poller: 未启用, 跳过")
            return False
        if self._db_path is None:
            logger.warning("news poller: db_path 未设置, 跳过")
            return False

        with self._lock:
            if self._running:
                return True
            self._running = True
            news_store.init_db(self._db_path)
            self._thread = threading.Thread(
                target=self._thread_main, name="news-poller", daemon=True,
            )
            self._thread.start()
        logger.info("news poller: 已启动")
        return True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:
                pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=20)
        self._thread = None
        logger.info("news poller: 已停止")

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def is_running(self) -> bool:
        return self._running

    # ── 线程主体 ─────────────────────────────────────────

    def _thread_main(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._poll_loop())
        except Exception as e:  # noqa: BLE001
            logger.warning("news poller 线程异常退出: %s", e)
        finally:
            try:
                if self._loop is not None:
                    self._loop.close()
            except Exception:  # noqa: BLE001
                pass
            self._loop = None

    async def _poll_loop(self) -> None:
        from app.services import news_source, news_store, preferences

        backoff = 5.0
        while self._running:
            try:
                items = await asyncio.to_thread(news_source.fetch_telegraph)
                if items and self._db_path is not None:
                    n = await asyncio.to_thread(
                        news_store.insert_telegraphs, self._db_path, items,
                    )
                    if n:
                        logger.info("news poller: 新增 %d 条快讯", n)
                backoff = 5.0
            except Exception as e:  # noqa: BLE001
                logger.debug("news poller 单轮失败: %s", e)
                backoff = min(backoff * 1.5, 60.0)

            interval = preferences.get_news_poll_interval()
            # 分片 sleep, 便于 stop 时快速响应
            slept = 0.0
            step = 2.0
            target = max(interval, backoff)
            while self._running and slept < target:
                await asyncio.sleep(step)
                slept += step
