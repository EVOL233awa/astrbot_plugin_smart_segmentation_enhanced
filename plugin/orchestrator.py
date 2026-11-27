"""切分编排：LLM 分段调用、锚点一次调用路径与 provider 解析。

从 main.py 拆出（行为等价）。实例状态 ``context`` 由
main.SmartSegmentationPlugin.__init__ 初始化，本模块仅作类型声明。
"""
from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from .settings import SegmentationSettings
from ..segmentation import (
    apply_force_split_to_segments,
    apply_no_split_boundaries,
    build_anchor_masking,
    build_segmentation_prompt,
    expand_under_split_natural_segments,
    has_protected_block,
    is_markdown_document,
    local_fallback_split,
    mask_pipe_in_text,
    merge_short_tail_segment,
    parse_raw_model_segments,
    parse_segments_from_model_output,
    restore_pipe_in_segments,
    segment_from_anchored_output,
    split_segments_at_bracket_boundaries,
    stabilize_segment_lengths,
)
from ..segmentation.packing import attach_symbol_only_pieces


def _mask_force_symbols(
    symbols: list[str] | None,
    pipe_placeholder: str,
) -> list[str]:
    """把强制切分符号里的 ``|`` 换成管道占位符。

    LLM 路径在掩码文本上做强制切分，原文的 ``|`` 已被替换为占位符；
    符号本身也要同步替换才能在掩码文本上命中，返回前由
    ``restore_pipe_in_segments`` 统一还原。
    """
    if not symbols:
        return []
    return [str(symbol).replace("|", pipe_placeholder) for symbol in symbols]


class OrchestrationMixin:
    """LLM 切分路径编排：由 main.SmartSegmentationPlugin 组合使用。"""

    # 以下实例属性由 main.__init__ 统一初始化，此处仅作类型声明。
    context: Context

    async def _resolve_provider_id(
        self,
        event: AstrMessageEvent,
        settings: SegmentationSettings,
    ) -> str:
        """获取当前消息事件对应会话的底层 LLM 提供商 ID。"""
        if settings.provider_id:
            return settings.provider_id

        get_current = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(get_current):
            try:
                provider_id = await get_current(event.unified_msg_origin)
                if provider_id:
                    return str(provider_id).strip()
            except Exception as exc:
                logger.debug("获取当前会话 provider_id 失败: %s", exc)

        get_using = getattr(self.context, "get_using_provider", None)
        if callable(get_using):
            try:
                provider = get_using(event.unified_msg_origin)
                meta = provider.meta() if provider and hasattr(provider, "meta") else None
                provider_id = getattr(meta, "id", "") if meta else ""
                return str(provider_id or "").strip()
            except Exception as exc:
                logger.debug("回退获取 provider_id 失败: %s", exc)

        return ""

    async def _segment_text(
        self,
        text: str,
        *,
        provider_id: str,
        settings: SegmentationSettings,
    ) -> list[str]:
        """调用大模型并传入针对性提示词，执行智能分段流程。"""
        if has_protected_block(text) and not is_markdown_document(text):
            return await self._segment_text_with_anchors(
                text,
                provider_id=provider_id,
                settings=settings,
            )
        masked_text, pipe_placeholder = mask_pipe_in_text(text)
        prompt = build_segmentation_prompt(
            masked_text,
            settings.style,
            settings.max_segments,
        )
        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
        )
        raw_text = str(getattr(response, "completion_text", "") or "").strip()
        if not raw_text:
            return [text]

        options = self._split_option_kwargs(settings)
        options["force_split_symbols"] = _mask_force_symbols(
            settings.force_split_symbols,
            pipe_placeholder,
        )
        segments = parse_segments_from_model_output(
            raw_text,
            fallback_text=masked_text,
            max_segments=settings.max_segments,
            max_chars_per_segment=settings.max_chars_per_segment,
            pause_min_chars=settings.ellipsis_pause_min_chars,
            **options,
        )
        return restore_pipe_in_segments(segments, pipe_placeholder)

    @staticmethod
    def _repair_expanded_segments(
        segments: list[str],
        pause_min_chars: int,
    ) -> list[str]:
        """锚点还原后的文本段补齐本地切分，与普通 LLM 路径同口径。

        受保护块段原样保留；普通文本段补切漏切的句子与动作括号，
        避免“同一段话有没有代码块”导致分段结果不同。
        """
        repaired: list[str] = []
        for segment in segments:
            if has_protected_block(segment):
                repaired.append(segment)
                continue
            for part in expand_under_split_natural_segments(
                [segment],
                pause_min_chars=pause_min_chars,
            ):
                repaired.extend(
                    split_segments_at_bracket_boundaries([part], max_segments=0)
                )
        return repaired

    async def _segment_text_with_anchors(
        self,
        text: str,
        *,
        provider_id: str,
        settings: SegmentationSettings,
    ) -> list[str]:
        """含受保护块消息的锚点一次调用路径（§5.6）。

        块原位替换为唯一 alnum 锚点后只调用一次 LLM（段数预算扣除块数）；
        重建或锚点校验失败、LLM 异常时对原文执行本地切分——效果等于
        v1.4.1 的整条降级，不倒退。锚点还原在返回前完成，缓存、outbound
        与补发队列只见真实分段。
        """
        def _local_fallback() -> list[str]:
            return local_fallback_split(
                text,
                settings.max_segments,
                settings.max_chars_per_segment,
                pause_min_chars=settings.ellipsis_pause_min_chars,
                **self._split_option_kwargs(settings),
            )

        try:
            masking = build_anchor_masking(text)
            if masking is None:
                return _local_fallback()
            if not masking.has_text:
                # 只有受保护块没有文本区间：不调用 LLM（§5.6 步骤 8）。
                return _local_fallback()
            masked_text, pipe_placeholder = mask_pipe_in_text(masking.masked_text)
            budget = max(1, settings.max_segments - len(masking.anchors))
            prompt = build_segmentation_prompt(masked_text, settings.style, budget)
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
            )
            raw_text = str(getattr(response, "completion_text", "") or "").strip()
            if not raw_text:
                raise ValueError("分段模型返回为空")
            llm_segments = parse_raw_model_segments(raw_text, fallback_text=masked_text)
            expanded = segment_from_anchored_output(
                llm_segments,
                masked_text,
                masking,
                pipe_placeholder,
            )
            if not expanded:
                raise ValueError("锚点校验未通过或重建失败")
        except Exception as exc:
            logger.warning("智能分段锚点路径失败，已降级本地切分: %s", exc)
            return _local_fallback()

        # 还原后的普通文本段补齐本地切分，口径与普通 LLM 路径一致。
        expanded = self._repair_expanded_segments(
            expanded,
            settings.ellipsis_pause_min_chars,
        )
        # 还原域后处理：与普通 LLM 路径语义一致（软约束在硬上限之前）。
        final = apply_no_split_boundaries(
            expanded,
            settings.no_split_around,
            settings.max_chars_per_segment,
        )
        final = apply_force_split_to_segments(final, settings.force_split_symbols)
        final = attach_symbol_only_pieces(final, settings.force_split_symbols)
        # 与本地/普通 LLM 路径同序：先落硬上限，再处理短尾，
        # 避免 stabilize 新切出的短尾逃过合并。
        final = stabilize_segment_lengths(
            final,
            max_segments=settings.max_segments,
            max_chars_per_segment=settings.max_chars_per_segment,
            pause_min_chars=settings.ellipsis_pause_min_chars,
        )
        if settings.merge_short_tail:
            final = merge_short_tail_segment(
                final,
                settings.max_chars_per_segment,
                settings.short_tail_threshold,
                force_split_symbols=settings.force_split_symbols,
            )
        return final
