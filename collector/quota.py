"""动作调度器（auto-download-mode design D1-D3 + specs/action-scheduler）。

取代旧 QuotaGuard：每小时窗口是"预算桶"而非墙钟周期——
- 开窗：首个动作到来时（anchor=now）；自动任务名额用尽立即以
  max(now, last_finish+min_gap) 为新 anchor 开下一窗（"执行完就进入下一个小时"）。
- 手动路径 acquire_now()：立即放行；窗口满且名义时长未过 → 拒绝（429 语义），
  名义时长已过 → 允许开新窗。手动消耗最远端日程点，近端日程不受影响。
- 日程（jittered grid，D2）：窗口等分为 N 格、每格内随机偏移、强制相邻 ≥ min_gap。
  不用纯随机（会扎堆），不用等间隔（机器人特征）。
- 日程点是"最早可执行时间"（D3）：next = max(slot, last_finish + min_gap)；
  失败重试/下载耗时把后续日程自然顺延（窗口拉伸），不追赶。

消费路径：
- 自动任务：await scheduler.wait_for_slot()（睡到点 + 扣名额）
- 手动操作：scheduler.acquire_now()，采集结束后 scheduler.report_finish()
- 冷却/熔断：scheduler.invalidate() 作废当前窗口；下次消费重新开窗、重新生成日程

前置约束：至多一个自动任务在跑（AutoJobManager 全局单活跃），
wait_for_slot 的"取点→睡眠→提交"两阶段假定单消费者（手动从尾端消费，不冲突）。

线程安全：全部状态变更持 threading.Lock（纯内存操作，无 await 持锁）。
"""
from __future__ import annotations

import asyncio
import random
import threading
import time
from typing import List, Optional

from collector.config import settings


class QuotaExceeded(Exception):
    """当前窗口预算已满（手动路径立即执行被拒）。kind 恒为 "session"。"""

    def __init__(self, message: str):
        self.kind = "session"
        super().__init__(message)


class ActionScheduler:
    """每小时窗口预算 + jittered-grid 动作日程。"""

    def __init__(self, window_seconds: float, limit: int, min_gap_sec: float, jitter: float):
        self._window = float(window_seconds)
        self._limit = int(limit)
        self._min_gap = float(min_gap_sec)
        self._jitter = float(jitter)
        self._lock = threading.Lock()
        self._anchor: Optional[float] = None  # 当前窗口起点
        self._slots: List[float] = []          # 剩余日程点（升序）
        self._used = 0                          # 当前窗口已消耗名额
        self._last_finish = 0.0                 # 最近一次动作完成时刻
        self._generation = 0                    # 窗口代号（睡眠期间作废检测）

    # ---- 日程生成（持锁内部） ----

    def _generate_slots(self, anchor: float) -> List[float]:
        """jittered grid：N 点铺满窗口、随机偏移、强制相邻 ≥ min_gap。

        min_gap 钳制可能把尾部日程推出名义窗口之外——预算桶允许（D2/D3）。
        """
        n = self._limit
        if n <= 0:
            return []
        base = self._window / n
        offsets = sorted(
            i * base + random.uniform(-self._jitter * base, self._jitter * base)
            for i in range(n)
        )
        slots: List[float] = []
        for off in offsets:
            t = anchor + off
            if not slots:
                t = max(t, anchor)  # 首点不早于 anchor（抖动可能为负）
            else:
                t = max(t, slots[-1] + self._min_gap)
            slots.append(t)
        return slots

    def _open_window_locked(self, now: float) -> None:
        anchor = max(now, self._last_finish + self._min_gap)
        self._anchor = anchor
        self._slots = self._generate_slots(anchor)
        self._used = 0
        self._generation += 1

    # ---- 公开接口 ----

    async def wait_for_slot(self) -> None:
        """睡到下一个日程点并扣一个名额（自动任务专用）。

        窗口耗尽自动开新窗；睡眠期间窗口被作废（冷却/熔断）则丢弃本点重新取点。
        睡眠可被任务取消（CancelledError 正常传播）。
        """
        while True:
            with self._lock:
                now = time.time()
                if self._anchor is None or not self._slots:
                    self._open_window_locked(now)
                # 日程点 = 最早可执行时间：被上一动作完成时刻 + min_gap 抬升（D3）
                floor = max(self._last_finish + self._min_gap, now)
                slot = max(self._slots[0], floor)
                gen = self._generation
            delay = slot - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            with self._lock:
                if self._generation != gen:
                    continue  # 睡眠期间窗口被作废 → 重新取点（名额未消耗）
                if self._slots:
                    self._slots.pop(0)
                self._used += 1
            return

    def acquire_now(self) -> None:
        """手动路径：立即放行并扣一个名额。

        窗口满：名义时长已过 → 开新窗放行；未过 → 抛 QuotaExceeded（路由映射 429）。
        消耗最远端日程点（近期日程不受影响），并使自动任务下一动作不早于
        report_finish() 上报的完成时刻 + min_gap。
        """
        with self._lock:
            now = time.time()
            if self._anchor is None:
                self._open_window_locked(now)
            if not self._slots:
                if now >= self._anchor + self._window:
                    self._open_window_locked(now)  # 名义时长已过 → 手动可开新窗
                else:
                    raise QuotaExceeded(
                        f"当前窗口配额已满（{int(self._window // 60)} 分钟内 ≤ {self._limit} 次），请稍后再试。"
                    )
            self._slots.pop()
            self._used += 1

    def report_finish(self) -> None:
        """上报一次动作完成时刻（手动路由在采集结束后调用；自动任务每动作后调用）。"""
        with self._lock:
            self._last_finish = max(self._last_finish, time.time())

    def invalidate(self) -> None:
        """作废当前窗口（冷却/熔断）：剩余日程与名额丢弃，下次消费重新开窗。"""
        with self._lock:
            self._anchor = None
            self._slots = []
            self._used = 0
            self._generation += 1

    def status(self) -> dict:
        """当前调度状态（job 状态接口 / 调试出口）。"""
        with self._lock:
            return {
                "window_anchor": self._anchor,
                "window_seconds": int(self._window),
                "window_limit": self._limit,
                "window_used": self._used,
                "window_remaining": self._limit - self._used if self._anchor is not None else self._limit,
                "min_gap_sec": self._min_gap,
                "next_slot_at": self._slots[0] if self._slots else None,
                "last_finish": self._last_finish or None,
                "generation": self._generation,
            }


scheduler = ActionScheduler(
    settings.session_window_minutes * 60,
    settings.session_action_limit,
    settings.min_action_gap_sec,
    settings.slot_jitter,
)
