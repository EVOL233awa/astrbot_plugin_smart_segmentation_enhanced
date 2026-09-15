"""AstrBot 智能分段插件。

运行时按职责拆分为 ``plugin/`` 包（settings / prepared_store /
follow_up / orchestrator，以 Mixin 组合进插件类），纯切分逻辑位于
``segmentation/`` 包；本文件保留 Star 插件入口、hook 编排，并作为
公开 API 门面再导出既有名字，外部与测试的导入路径保持不变。
"""
from __future__ import annotations
import asyncio
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult
from astrbot.api.event import filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star

from .plugin import (
    _PENDING_EXTRA_KEY,
    _PENDING_FOLLOW_UP_TTL_SECONDS,
    _PENDING_REAPER_INTERVAL_SECONDS,
    _PREPARED_EXTRA_KEY,
    _PREPARED_SEGMENT_TTL_SECONDS,
)
from .plugin.follow_up import FollowUpMixin, PendingFollowUp
from .plugin.orchestrator import OrchestrationMixin
from .plugin.prepared_store import PreparedSegments, PreparedStoreMixin
from .plugin.settings import SegmentationSettings, SettingsMixin

# 门面再导出：切分逻辑的公开名字继续可从 main 导入（v1.4.1 起的兼容承诺）
from .segmentation import (
    build_anchor_masking,
    build_segmentation_prompt,
    calculate_send_delay,
    hash_normalized_text,
    is_action_only_text,
    parse_raw_model_segments,
    parse_segments_from_model_output,
    segment_from_anchored_output,
    strip_thinking_content,
    clean_markdown_by_mode,
    normalize_response_text_for_key,
    normalize_content_key,
    align_prepared_in_outbound,
    mask_pipe_in_text,
    restore_pipe_in_segments,
    # 非自然语言检测与本地逻辑切分工具
    apply_force_split_to_segments,
    apply_no_split_boundaries,
    is_non_natural_language,
    is_markdown_document,
    has_protected_block,
    local_fallback_split,
    merge_short_tail_segment,
    safe_escape_segments,
    stabilize_segment_lengths,
)

__all__ = [
    "SmartSegmentationPlugin",
    "SegmentationSettings",
    "PreparedSegments",
    "PendingFollowUp",
    "_PENDING_EXTRA_KEY",
    "_PENDING_FOLLOW_UP_TTL_SECONDS",
    "_PENDING_REAPER_INTERVAL_SECONDS",
    "_PREPARED_EXTRA_KEY",
    "_PREPARED_SEGMENT_TTL_SECONDS",
    "build_anchor_masking",
    "build_segmentation_prompt",
    "calculate_send_delay",
    "hash_normalized_text",
    "is_action_only_text",
    "parse_raw_model_segments",
    "parse_segments_from_model_output",
    "segment_from_anchored_output",
    "strip_thinking_content",
    "clean_markdown_by_mode",
    "normalize_response_text_for_key",
    "normalize_content_key",
    "align_prepared_in_outbound",
    "mask_pipe_in_text",
    "restore_pipe_in_segments",
    "apply_force_split_to_segments",
    "apply_no_split_boundaries",
    "is_non_natural_language",
    "is_markdown_document",
    "has_protected_block",
    "local_fallback_split",
    "merge_short_tail_segment",
    "safe_escape_segments",
    "stabilize_segment_lengths",
]


class SmartSegmentationPlugin(
    SettingsMixin,
    PreparedStoreMixin,
    OrchestrationMixin,
    FollowUpMixin,
    Star,
):
    """使用 LLM 对 AstrBot 主回复进行自然分段。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        """初始化智能分段插件并设置初始缓存状态。"""
        super().__init__(context)
        self.config = config if config is not None else {}
        self._prepared_segments: dict[tuple[str, str], PreparedSegments] = {}
        self._pending_follow_ups: dict[str, PendingFollowUp] = {}
        self._active_follow_up_tasks: set[asyncio.Task[Any]] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._lock_reaper_task: asyncio.Task[Any] | None = None
        self._smart_delay_warned: set[str] = set()

    @filter.on_llm_response()
    async def on_llm_response(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """在主 LLM 返回后预先计算智能分段。"""
        settings = self._get_settings()
        if settings is None:
            return

        text = clean_markdown_by_mode(
            self._extract_response_plain_text(response),
            settings.markdown_clean_mode,
        )
        if not self._should_segment_text(text, settings):
            return

        # 仅在 local 引擎、纯 JSON/代码、整篇 Markdown 时跳过分段模型；
        # 含受保护块的普通消息走锚点路径（§5.6），不再整条降级本地。
        if (
            settings.segmenter == "local"
            or is_non_natural_language(text)
            or is_markdown_document(text)
        ):
            logger.info(
                "智能分段已启用本地/受保护块直发模式，跳过分段模型调用"
            )
            segments = local_fallback_split(
                text,
                settings.max_segments,
                settings.max_chars_per_segment,
                pause_min_chars=settings.ellipsis_pause_min_chars,
                **self._split_option_kwargs(settings),
            )
        else:
            provider_id = await self._resolve_provider_id(event, settings)
            if not provider_id:
                logger.warning("智能分段未找到可用 provider_id，跳过本次分段")
                self._store_skip_segment(event.unified_msg_origin, text)
                return

            try:
                segments = await asyncio.wait_for(
                    self._segment_text(
                        text,
                        provider_id=provider_id,
                        settings=settings,
                    ),
                    timeout=settings.timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "智能分段 LLM 调用超时（> %.2fs），已跳过本次回复",
                    settings.timeout_seconds,
                )
                self._store_skip_segment(event.unified_msg_origin, text)
                return
            except Exception as exc:
                logger.error("智能分段 LLM 调用失败: %s", exc, exc_info=True)
                self._store_skip_segment(event.unified_msg_origin, text)
                return

        if not segments:
            return

        self._store_prepared_segments(event.unified_msg_origin, text, segments)
        event.set_extra(
            _PREPARED_EXTRA_KEY,
            {"text": text, "segments": list(segments)},
        )
        logger.info("智能分段预处理完成，共 %s 段", len(segments))

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """发送前把完整回复替换为首段，并登记剩余段。"""
        settings = self._get_settings()
        if settings is None:
            return

        result = event.get_result()
        if result is None or not self._is_model_text_result(result):
            return

        raw_outbound = self._extract_plain_text_chain(result)
        if not raw_outbound:
            return
        outbound_text = clean_markdown_by_mode(
            raw_outbound,
            settings.markdown_clean_mode,
        )
        if not outbound_text:
            return
        if outbound_text != raw_outbound:
            result.chain = [Plain(outbound_text)]

        session = event.unified_msg_origin
        self._prune_expired_pending_follow_ups()
        self._ensure_lock_reaper()
        lock = self._session_locks.get(session)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session] = lock
        await lock.acquire()
        follow_up_holds_lock = False
        try:
            # 传入最大分段数与最小长度参数，以支持自愈拼接决策及本地高精兜底切分
            segments = self._take_prepared_segments_from_event(
                event,
                outbound_text,
                settings=settings,
            )
            if segments is None:
                segments = self._pop_prepared_segments(
                    event.unified_msg_origin,
                    outbound_text,
                    min_length=settings.min_length,
                    max_segments=settings.max_segments,
                    max_chars_per_segment=settings.max_chars_per_segment,
                    ellipsis_pause_min_chars=settings.ellipsis_pause_min_chars,
                    **self._split_option_kwargs(settings),
                )
            if not segments or len(segments) <= 1:
                return

            segments = stabilize_segment_lengths(
                segments,
                max_segments=settings.max_segments,
                max_chars_per_segment=settings.max_chars_per_segment,
                pause_min_chars=settings.ellipsis_pause_min_chars,
            )
            if not segments or len(segments) <= 1:
                return

            first_segment = segments[0]
            follow_up_segments = segments[1:]
            if not follow_up_segments:
                return

            result.chain = [Plain(first_segment)]
            pending_id = self._register_pending_follow_up(
                session=event.unified_msg_origin,
                segments=follow_up_segments,
                settings=settings,
                first_segment=first_segment,
            )
            event.set_extra(_PENDING_EXTRA_KEY, pending_id)
            pending = self._pending_follow_ups.get(pending_id)
            if pending is not None:
                pending.lock = lock
                follow_up_holds_lock = True
            logger.info("智能分段首段已替换，登记 %s 条补发消息", len(follow_up_segments))
        finally:
            if not follow_up_holds_lock:
                lock.release()

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """首段发送后后台补发剩余分段。"""
        pending_id = str(event.get_extra(_PENDING_EXTRA_KEY, "") or "").strip()
        if not pending_id:
            return

        pending = self._pop_pending_follow_up(pending_id)
        if pending is None or not pending.segments:
            return

        try:
            task = asyncio.create_task(self._run_follow_up_segments(pending))
        except Exception:
            self._release_pending_lock(pending)
            raise
        self._track_follow_up_task(task)

    async def terminate(self) -> None:
        """插件卸载时取消尚未完成的补发任务并清空缓存。"""
        if self._lock_reaper_task is not None and not self._lock_reaper_task.done():
            self._lock_reaper_task.cancel()
            try:
                await self._lock_reaper_task
            except asyncio.CancelledError:
                pass
            self._lock_reaper_task = None
        for task in list(self._active_follow_up_tasks):
            if not task.done():
                task.cancel()
        await self._drain_tasks()
        self._active_follow_up_tasks.clear()
        self._prepared_segments.clear()
        for pending in list(self._pending_follow_ups.values()):
            self._release_pending_lock(pending)
        self._pending_follow_ups.clear()
        self._session_locks.clear()

    @staticmethod
    def _extract_response_plain_text(response: LLMResponse) -> str:
        """从大语言模型响应结构中提取纯文本，自动过滤思考标签。"""
        role = str(getattr(response, "role", "") or "").strip().lower()
        if role and role not in {"assistant", "ai"}:
            return ""

        result_chain = getattr(response, "result_chain", None)
        if result_chain is not None and not SmartSegmentationPlugin._is_plain_chain(
            result_chain,
        ):
            return ""

        text = str(getattr(response, "completion_text", "") or "")
        return strip_thinking_content(text)

    @staticmethod
    def _is_plain_chain(message_chain: MessageChain) -> bool:
        """判断消息链是否为纯文本组成的轻量级消息链。"""
        chain = getattr(message_chain, "chain", None)
        return isinstance(chain, list) and bool(chain) and all(
            isinstance(component, Plain) for component in chain
        )

    @classmethod
    def _extract_plain_text_chain(cls, message_chain: MessageChain) -> str:
        """从消息事件结果中提取并整合干净的纯文本内容。"""
        if not cls._is_plain_chain(message_chain):
            return ""
        texts = [component.text for component in message_chain.chain]
        return strip_thinking_content(" ".join(texts))

    @classmethod
    def _is_model_text_result(cls, result: MessageEventResult) -> bool:
        """判断当前消息装饰结果是否为大模型生成的纯文本响应。"""
        is_model_result = getattr(result, "is_model_result", None)
        if callable(is_model_result):
            try:
                if not is_model_result():
                    return False
            except Exception:
                return False
        return cls._is_plain_chain(result)

    @staticmethod
    def _should_segment_text(text: str, settings: SegmentationSettings) -> bool:
        """根据文本长度及特征，判定当前文本是否需要执行分段。"""
        if not text:
            return False
        if len(text) < settings.min_length:
            return False
        return not is_action_only_text(text)
