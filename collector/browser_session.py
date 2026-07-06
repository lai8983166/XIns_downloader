"""常驻单浏览器会话（design D3/D4 + account-safety 规格）。

职责：
- 进程级单例：以 launch_persistent_context 复用 settings.browser_user_data_dir，
  维持真实可复用的浏览器画像与登录态（Phase 0 的 login.py 已建立登录态）。
- 串行化：所有采集经一个 asyncio.Lock，同一时刻至多一次 Instagram 导航/滚动
  （多 tab/高并发是最明显的机器人特征）。
- 守护：上下文失活/崩溃时自动 stop+start 重启。
- 懒启动：首次 page() 时才拉起浏览器；lifespan 仅注册 stop，未用不占资源。
- Profile 持久 page 池：跨请求复用同一 tab 续传滚动（加载更多只滚一页）。

典型用法（采集层）：
    async with session.page() as page:           # 临时 page，用完 close
        await page.goto(url)
    async with session.profile_page(username):   # 持久 page，用完不 close（续传）
        ...
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Optional

from patchright.async_api import BrowserContext, Error as PlaywrightError, Page, async_playwright

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
        # Profile 持久 page 池（username → page，LRU）；跨请求复用同一 tab 续传滚动
        self._profile_pages: "OrderedDict[str, Page]" = OrderedDict()
        self._profile_pool_max = settings.profile_page_pool_max

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

    async def _close_profile_pages_locked(self) -> None:
        """关闭所有持久 profile page（shutdown / 浏览器重启时调用）。"""
        while self._profile_pages:
            _, p = self._profile_pages.popitem()
            try:
                await p.close()
            except PlaywrightError:
                pass

    async def _stop_locked(self) -> None:
        await self._close_profile_pages_locked()
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
        await self._stop_locked()  # 连带清空持久 page 池
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
        """临时 page：串行获取，用完 close（持久上下文与登录态保留）。"""
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

    async def close_profile_page(self, username: str) -> None:
        """主动废弃某 username 的持久 page（TTL 失效 / 失活降级时调用）。"""
        async with self._lock:
            page = self._profile_pages.pop(username, None)
            if page is not None:
                try:
                    await page.close()
                except PlaywrightError:
                    pass

    @asynccontextmanager
    async def profile_page(self, username: str):
        """获取/复用一个停在 profile 页的持久 page（跨请求续传滚动用）。

        - 复用 self._lock（与临时 page() 互斥，串行不变）。
        - 失活检测（is_closed / url 探活）→ 新建；浏览器重启则清池。
        - LRU：move_to_end + 超上限淘汰最久未用并 close。
        - 用完**不 close**（持久保留），只释放锁。
        """
        async with self._lock:
            await self._ensure_locked()
            page = self._profile_pages.get(username)
            if page is not None:
                try:
                    if page.is_closed():
                        page = None
                    else:
                        _ = page.url  # 探活
                except PlaywrightError:
                    page = None
            if page is None:
                self._profile_pages.pop(username, None)
                try:
                    page = await self._context.new_page()
                except PlaywrightError:
                    await self._restart_locked()  # 重启会清空整个池
                    page = await self._context.new_page()
                self._profile_pages[username] = page
            # LRU：标记最近使用 + 淘汰超上限
            self._profile_pages.move_to_end(username)
            while len(self._profile_pages) > self._profile_pool_max:
                _, old_page = self._profile_pages.popitem(last=False)
                try:
                    await old_page.close()
                except PlaywrightError:
                    pass
            yield page
            # 不 close —— 持久保留，下次同 username 续传复用


session = BrowserSession()
