"""常驻单浏览器会话（design D3/D4 + account-safety 规格）。

职责：
- 进程级单例：以 launch_persistent_context 复用 settings.browser_user_data_dir，
  维持真实可复用的浏览器画像与登录态（Phase 0 的 login.py 已建立登录态）。
- 串行化：所有采集经一个 asyncio.Lock，同一时刻至多一次 Instagram 导航/滚动
  （多 tab/高并发是最明显的机器人特征）。
- 守护：上下文失活/崩溃时自动 stop+start 重启。
- 懒启动：首次 page() 时才拉起浏览器；lifespan 仅注册 stop，未用不占资源。

典型用法（采集层）：
    async with session.page() as page:
        await page.goto(url)
        html = await page.content()
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from patchright.async_api import BrowserContext, Error as PlaywrightError, async_playwright

from collector.config import settings

logger = logging.getLogger(__name__)

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


class BrowserSession:
    """常驻单浏览器上下文，串行化访问。"""

    def __init__(self, user_data_dir: Optional[str] = None, headless: bool = True):
        self._user_data_dir = user_data_dir or str(settings.browser_user_data_dir)
        self._headless = headless
        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._lock = asyncio.Lock()

    # ---- 生命周期（持锁内部版） ----

    async def _start_locked(self) -> None:
        if self._context is not None:
            return
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self._user_data_dir,
            headless=self._headless,
            viewport=DEFAULT_VIEWPORT,
        )
        logger.info("browser session started (user_data_dir=%s)", self._user_data_dir)

    async def _stop_locked(self) -> None:
        context, playwright = self._context, self._playwright
        self._context = None
        self._playwright = None
        if context is not None:
            try:
                await context.close()
            except PlaywrightError as exc:
                logger.warning("context close failed: %s", exc)
        if playwright is not None:
            try:
                await playwright.stop()
            except PlaywrightError as exc:
                logger.warning("playwright stop failed: %s", exc)

    async def _restart_locked(self) -> None:
        logger.warning("browser context lost, restarting")
        await self._stop_locked()
        await self._start_locked()

    async def _ensure_locked(self) -> None:
        """确保上下文可用；失活则重启。"""
        if self._context is None:
            await self._start_locked()
            return
        try:
            _ = self._context.pages  # 活上下文返回 list；失活抛 PlaywrightError
        except PlaywrightError:
            await self._restart_locked()

    # ---- 公开接口 ----

    async def start(self) -> None:
        async with self._lock:
            await self._start_locked()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    @asynccontextmanager
    async def page(self):
        """串行获取一个 page；同一时刻仅一个调用方持有。

        自动懒启动与失活重启。用完自动关闭该 page（持久上下文与登录态保留）。
        """
        async with self._lock:
            await self._ensure_locked()
            try:
                page = await self._context.new_page()
            except PlaywrightError:
                await self._restart_locked()
                page = await self._context.new_page()
            try:
                yield page
            finally:
                try:
                    await page.close()
                except PlaywrightError:
                    pass


session = BrowserSession()
