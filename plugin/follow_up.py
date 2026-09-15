"""补发引擎：分段队列登记、后台逐条补发与会话串行锁。

从 main.py 拆出（行为等价）。实例状态 ``_pending_follow_ups``、
``_active_follow_up_tasks``、``_session_locks``、``_lock_reaper_task``
与 ``context`` 由 main.SmartSegmentationPlugin.__init__ 初始化，
本模块仅作类型声明。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context

from . import _PENDING_FOLLOW_UP_TTL_SECONDS, _PENDING_REAPER_INTERVAL_SECONDS
from .settings import SegmentationSettings
from ..segmentation import calculate_send_delay


@dataclass(slots=True)
class PendingFollowUp:
    session: str
    segments: list[str]
    delay_base: float
    delay_per_char: float
    delay_max: float
    expires_at: float
    lock: asyncio.Lock | None = None
    # 延迟策略与 smart 参数在注册时快照：补发是后台任务，
    # 不受中途配置变更影响；previous_segment 是已发送的上一段，
    # smart 模式按其结尾符号修正与下一段之间的间隔。
    delay_strategy: str = "linear"
    previous_segment: str | None = None
    jitter_max_seconds: float = 0.15
    smart_increase_symbols: list[str] = field(default_factory=list)
    smart_increase_seconds: float = 0.25
    smart_decrease_symbols: list[str] = field(default_factory=list)
    smart_decrease_seconds: float = 0.25


class FollowUpMixin:
    """补发引擎与会话锁管理：由 main.SmartSegmentationPlugin 组合使用。"""

    # 以下实例属性由 main.__init__ 统一初始化，此处仅作类型声明。
    context: Context
    _pending_follow_ups: dict[str, PendingFollowUp]
    _active_follow_up_tasks: set[asyncio.Task[Any]]
    _session_locks: dict[str, asyncio.Lock]
    _lock_reaper_task: asyncio.Task[Any] | None

    def _ensure_lock_reaper(self) -> None:
        """Ensure expired pending locks are released even without after_message_sent."""
        if self._lock_reaper_task is None or self._lock_reaper_task.done():
            self._lock_reaper_task = asyncio.create_task(self._run_lock_reaper())

    async def _run_lock_reaper(self) -> None:
        try:
            while True:
                await asyncio.sleep(_PENDING_REAPER_INTERVAL_SECONDS)
                self._prune_expired_pending_follow_ups()
        except asyncio.CancelledError:
            raise

    async def _send_follow_up_with_retry(
        self,
        session: str,
        segment: str,
        attempts: int = 3,
    ) -> bool:
        for attempt in range(1, attempts + 1):
            try:
                sent = await self.context.send_message(
                    session,
                    MessageChain([Plain(segment)]),
                )
            except Exception as exc:
                logger.warning("智能分段补发第 %s 次异常: %s", attempt, exc)
                sent = False
            if sent:
                return True
            if attempt < attempts:
                await asyncio.sleep(min(0.5 * attempt, 2.0))
        return False

    def _register_pending_follow_up(
        self,
        *,
        session: str,
        segments: list[str],
        settings: SegmentationSettings,
        first_segment: str | None = None,
    ) -> str:
        """登记当前会话中等待后台补发的所有后续分段消息。"""
        self._prune_expired_pending_follow_ups()
        pending_id = uuid4().hex
        self._pending_follow_ups[pending_id] = PendingFollowUp(
            session=session,
            segments=list(segments),
            delay_base=settings.delay_base,
            delay_per_char=settings.delay_per_char,
            delay_max=settings.delay_max,
            expires_at=time.monotonic() + _PENDING_FOLLOW_UP_TTL_SECONDS,
            delay_strategy=settings.delay_strategy,
            # 首条补发的「上一段」就是已经发送出去的首段。
            previous_segment=first_segment,
            smart_increase_symbols=list(settings.smart_increase_symbols),
            smart_increase_seconds=settings.smart_increase_seconds,
            jitter_max_seconds=settings.jitter_max_seconds,
            smart_decrease_symbols=list(settings.smart_decrease_symbols),
            smart_decrease_seconds=settings.smart_decrease_seconds,
        )
        return pending_id

    def _pop_pending_follow_up(self, pending_id: str) -> PendingFollowUp | None:
        """检索并提取处于挂起状态下的延迟补发分段上下文。"""
        self._prune_expired_pending_follow_ups()
        return self._pending_follow_ups.pop(pending_id, None)

    def _release_pending_lock(self, pending: PendingFollowUp) -> None:
        """释放 pending 持有的会话串行锁，避免补发任务异常后锁泄漏。"""
        if pending.lock is not None and pending.lock.locked():
            pending.lock.release()
        pending.lock = None

    def _prune_expired_pending_follow_ups(self) -> None:
        """扫描并清理所有因超时失效的延迟补发上下文缓存。"""
        if not self._pending_follow_ups:
            return
        now = time.monotonic()
        expired_keys = [
            key for key, entry in self._pending_follow_ups.items() if entry.expires_at <= now
        ]
        for key in expired_keys:
            entry = self._pending_follow_ups.pop(key, None)
            if entry is not None:
                self._release_pending_lock(entry)

    def _track_follow_up_task(self, task: asyncio.Task[Any]) -> None:
        """追踪正在后台运行的异步补发任务，并在其完成后清理任务句柄。"""
        self._active_follow_up_tasks.add(task)
        task.add_done_callback(self._active_follow_up_tasks.discard)

    async def _drain_tasks(self) -> None:
        """强制等待或取消所有正在进行的后台补发异步任务，防止插件卸载时出现孤儿任务。"""
        tasks = [task for task in list(self._active_follow_up_tasks) if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_follow_up_segments(self, pending: PendingFollowUp) -> None:
        """后台异步主循环，按照时间延迟算法逐条补发余下的消息分段。"""
        try:
            # smart 延迟的符号修正看「上一段结尾」：首条补发的上一段是
            # 已发送的首段，后续补发的上一段是循环中的前一条。
            previous_segment = pending.previous_segment
            for segment in pending.segments:
                delay = calculate_send_delay(
                    segment,
                    pending.delay_base,
                    pending.delay_per_char,
                    pending.delay_max,
                    strategy=pending.delay_strategy,
                    previous_segment=previous_segment,
                    smart_increase_symbols=pending.smart_increase_symbols,
                    smart_increase_seconds=pending.smart_increase_seconds,
                    jitter_max_seconds=pending.jitter_max_seconds,
                    smart_decrease_symbols=pending.smart_decrease_symbols,
                    smart_decrease_seconds=pending.smart_decrease_seconds,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

                sent = await self._send_follow_up_with_retry(
                    pending.session,
                    segment,
                )
                previous_segment = segment
                if not sent:
                    logger.error("智能分段补发最终失败，会话: %s", pending.session)
        except asyncio.CancelledError:
            logger.warning("智能分段后台补发任务被取消，会话: %s", pending.session)
            raise
        except Exception as exc:
            logger.error("智能分段后台补发任务异常: %s", exc, exc_info=True)
        finally:
            self._release_pending_lock(pending)
