"""预处理分段缓存：存储、自愈拼合与事件直取。

从 main.py 拆出（行为等价）。实例状态 ``_prepared_segments`` 由
main.SmartSegmentationPlugin.__init__ 初始化，本模块仅作类型声明。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from . import _PREPARED_EXTRA_KEY, _PREPARED_SEGMENT_TTL_SECONDS
from .settings import SegmentationSettings
from ..segmentation import (
    align_prepared_in_outbound,
    hash_normalized_text,
    is_action_only_text,
    local_fallback_split,
    normalize_content_key,
    normalize_response_text_for_key,
    stabilize_segment_lengths,
)


@dataclass(slots=True)
class PreparedSegments:
    segments: list[str]
    raw_norm_text: str  # 缓存此分段对应的原始文本规范化表示，用作自愈拼合时的唯一基准
    expires_at: float
    match_key: str = ""


def _merge_non_alnum_edges(
    segments: list[str],
    outbound_text: str,
    prepared_text: str,
) -> list[str] | None:
    """outbound 只在头/尾多了非字母数字内容时，把差异贴回首/末段。

    内容签名忽略标点与 emoji，所以这类差异会被签名判成「同一文本」；直接
    返回 prepared 分段会让新增的 emoji/标点从消息里消失。
    """
    if not segments or not outbound_text or not prepared_text:
        return None
    index = outbound_text.find(prepared_text)
    if index < 0:
        return None
    prefix = outbound_text[:index]
    suffix = outbound_text[index + len(prepared_text):]
    if normalize_content_key(prefix) or normalize_content_key(suffix):
        return None
    merged = list(segments)
    if prefix.strip():
        merged[0] = prefix + merged[0]
    if suffix.strip():
        merged[-1] = merged[-1] + suffix
    return merged


class PreparedStoreMixin:
    """预处理分段缓存池：由 main.SmartSegmentationPlugin 组合使用。"""

    # 以下实例属性由 main.__init__ 统一初始化，此处仅作类型声明。
    _prepared_segments: dict[tuple[str, str], PreparedSegments]

    def _store_prepared_segments(
        self,
        session: str,
        response_text: str,
        segments: list[str],
    ) -> None:
        """将预处理的分段结果及原始文本的特征摘要写入高速缓存。"""
        self._prune_expired_prepared_segments()
        text_hash = hash_normalized_text(response_text)
        normalized_session = str(session or "").strip()
        if not normalized_session or not text_hash:
            return

        # 缓存分段结果的同时，缓存其原始文本的规范化特征，作为未来恢复拼接拼合的基准
        self._prepared_segments[(normalized_session, text_hash)] = PreparedSegments(
            segments=list(segments),
            raw_norm_text=normalize_response_text_for_key(response_text),
            expires_at=time.monotonic() + _PREPARED_SEGMENT_TTL_SECONDS,
            match_key=normalize_content_key(response_text),
        )

    def _store_skip_segment(self, session: str, response_text: str) -> None:
        """缓存本次不分段的负向结果，避免装饰阶段被本地兜底二次拆分。"""
        self._store_prepared_segments(session, response_text, [])

    def _take_prepared_segments_from_event(
        self,
        event: AstrMessageEvent,
        outbound_text: str,
        *,
        settings: SegmentationSettings,
    ) -> list[str] | None:
        """Use segments bound to the current event before falling back to hashing."""
        extra = event.get_extra(_PREPARED_EXTRA_KEY, None)
        if not isinstance(extra, dict):
            return None

        prepared_text = str(extra.get("text") or "")
        prepared_segments = extra.get("segments")
        if not prepared_text or not isinstance(prepared_segments, list):
            return None
        if not prepared_segments:
            return []

        if outbound_text == prepared_text:
            return list(prepared_segments)

        merged_edges = _merge_non_alnum_edges(
            list(prepared_segments),
            outbound_text,
            prepared_text,
        )
        if merged_edges is not None:
            return merged_edges

        outbound_key = normalize_content_key(outbound_text)
        prepared_key = normalize_content_key(prepared_text)
        if outbound_key == prepared_key:
            return list(prepared_segments)
        if outbound_key.startswith(prepared_key) and outbound_text.startswith(
            prepared_text
        ):
            segments = list(prepared_segments)
            suffix = outbound_text[len(prepared_text) :]
            # 补切预算与已有段数对齐，避免叠加后超出 max_segments
            # 触发装饰阶段的强制合并、破坏预处理分段边界。
            budget = (
                self._aligned_split_budget(segments, settings)
                if settings.max_segments > 0
                else 0
            )
            suffix_segments = local_fallback_split(
                suffix,
                budget,
                settings.max_chars_per_segment,
                pause_min_chars=settings.ellipsis_pause_min_chars,
                **self._split_option_kwargs(settings),
            )
            segments.extend(suffix_segments)
            return segments
        # Tool Loop / 中间插件改写会在 prepared 文本头部或尾部插入内容，
        # 按内容签名在 outbound 中定位 prepared，头部/尾部只补切插入部分，
        # 预处理的分段边界全部保留。
        aligned = align_prepared_in_outbound(outbound_text, prepared_text)
        if aligned is not None:
            head, _middle, tail = aligned
            segments = list(prepared_segments)
            segments = self._prepend_aligned_parts(
                segments,
                head,
                settings,
            )
            segments = self._append_aligned_parts(
                segments,
                tail,
                settings,
            )
            # 补切预算「至少保留 1 段以不丢内容」可能让总段数超出
            # max_segments（池内已占满时），此处强制合并兜底保证段数达标。
            if settings.max_segments > 0 and len(segments) > settings.max_segments:
                segments = stabilize_segment_lengths(
                    segments,
                    max_segments=settings.max_segments,
                    max_chars_per_segment=settings.max_chars_per_segment,
                    pause_min_chars=settings.ellipsis_pause_min_chars,
                )
            return segments
        return None

    def _aligned_split_budget(self, segments: list[str], settings: SegmentationSettings) -> int:
        """计算对齐补切的段数预算：尽量不超出 max_segments，但至少保留 1 段以不丢内容。"""
        max_segments = settings.max_segments
        if max_segments <= 0:
            return 0
        return max(1, max_segments - len(segments))

    def _prepend_aligned_parts(
        self,
        segments: list[str],
        head_text: str,
        settings: SegmentationSettings,
    ) -> list[str]:
        """对 prepared 之前的插入文本按剩余段数预算本地补切并前置拼接。"""
        if not str(head_text or "").strip():
            return segments
        if segments and not normalize_content_key(head_text):
            # 纯符号/emoji 的前导插入跟着首段走，不单独占一条气泡。
            merged = list(segments)
            merged[0] = head_text + merged[0]
            return merged
        head_segments = local_fallback_split(
            head_text,
            self._aligned_split_budget(segments, settings),
            settings.max_chars_per_segment,
            pause_min_chars=settings.ellipsis_pause_min_chars,
            **self._split_option_kwargs(settings),
        )
        return head_segments + segments

    def _append_aligned_parts(
        self,
        segments: list[str],
        tail_text: str,
        settings: SegmentationSettings,
    ) -> list[str]:
        """对 prepared 之后的插入文本本地补切并后置拼接。"""
        if not str(tail_text or "").strip():
            return segments
        if segments and not normalize_content_key(tail_text):
            # 纯符号/emoji 的尾部插入跟着末段走，不单独占一条气泡。
            merged = list(segments)
            merged[-1] = merged[-1] + tail_text
            return merged
        budget = self._aligned_split_budget(segments, settings)
        tail_segments = local_fallback_split(
            tail_text,
            budget,
            settings.max_chars_per_segment,
            pause_min_chars=settings.ellipsis_pause_min_chars,
            **self._split_option_kwargs(settings),
        )
        return segments + tail_segments

    def _pop_prepared_segments(
        self,
        session: str,
        outbound_text: str,
        *,
        min_length: int = 15,
        max_segments: int = 0,
        max_chars_per_segment: int = 0,
        ellipsis_pause_min_chars: int = 4,
        force_split_symbols: list[str] | None = None,
        no_split_around: list[str] | None = None,
        merge_short_tail: bool = False,
        short_tail_threshold: int = 2,
        remove_thinking: bool = True,
    ) -> list[str] | None:
        """从预存库中获取分段结果（精确匹配 -> 顺序贪婪组合自愈 -> 本地高精分段兜底）。"""
        self._prune_expired_prepared_segments()
        normalized_session = str(session or "").strip()
        norm_outbound = normalize_response_text_for_key(outbound_text)
        if not normalized_session or not norm_outbound:
            return None

        # 1. 精确哈希匹配
        text_hash = hash_normalized_text(outbound_text)
        entry = self._prepared_segments.pop((normalized_session, text_hash), None)
        if entry is not None:
            return list(entry.segments)

        # 2. 顺序自愈：先用内容签名精确匹配，再按内容签名前缀贪婪拼接。
        # 内容签名忽略空白、大小写与标点，可覆盖 Markdown 清洗和 Tool Loop 合并带来的文本差异。
        session_entries = {
            key: val
            for key, val in self._prepared_segments.items()
            if key[0] == normalized_session
        }
        if session_entries:
            outbound_match_key = normalize_content_key(outbound_text)
            negative_key: tuple[str, str] | None = None
            for key, entry in session_entries.items():
                if entry.match_key and entry.match_key == outbound_match_key:
                    # 同一内容签名下正向条目优先，负向条目（segments 为空）
                    # 只在没有任何正向条目命中时才生效，避免结果依赖插入顺序。
                    if not entry.segments:
                        if negative_key is None:
                            negative_key = key
                        continue
                    self._prepared_segments.pop(key, None)
                    return list(entry.segments)
            if negative_key is not None:
                self._prepared_segments.pop(negative_key, None)
                return []

            remaining = outbound_match_key
            assembled_segs = []
            matched_keys = []
            while remaining:
                best_match_len = 0
                best_match_segs = None
                best_match_key = None
                for key, val in session_entries.items():
                    piece = val.match_key
                    # 负向缓存条目（segments 为空，表示"该文本不分段"）不参与拼合，
                    # 否则其覆盖的中间文本会被空分段吞掉。
                    if not val.segments:
                        continue
                    if piece and remaining.startswith(piece):
                        piece_length = len(piece)
                        if piece_length > best_match_len:
                            best_match_len = piece_length
                            best_match_segs = val.segments
                            best_match_key = key
                if best_match_segs is not None and best_match_key:
                    assembled_segs.extend(best_match_segs)
                    remaining = remaining[best_match_len:]
                    matched_keys.append(best_match_key)
                else:
                    break

            if not remaining and assembled_segs:
                for m_key in matched_keys:
                    self._prepared_segments.pop(m_key, None)
                return assembled_segs

            # 2.5 contains 对齐：prepared 文本出现在 outbound 中间
            # （Tool Loop 头部插入等场景，前缀拼合无法命中）。
            best_aligned_key: str | None = None
            best_aligned_entry: Any = None
            best_aligned_parts: tuple[str, str, str] | None = None
            for key, val in session_entries.items():
                # 负向缓存条目（segments 为空）不参与 contains 对齐，
                # 否则对齐命中的正文会被空分段吞掉。
                if not val.segments:
                    continue
                if not val.match_key or len(val.match_key) < 8:
                    continue
                if val.match_key == outbound_match_key:
                    continue
                aligned = align_prepared_in_outbound(
                    outbound_text, val.raw_norm_text
                )
                if aligned is None:
                    continue
                if (
                    best_aligned_parts is None
                    or len(val.match_key) > len(
                        session_entries[best_aligned_key].match_key or ""
                    )
                ):
                    best_aligned_key = key
                    best_aligned_entry = val
                    best_aligned_parts = aligned
            if (
                best_aligned_key
                and best_aligned_entry is not None
                and best_aligned_parts is not None
            ):
                self._prepared_segments.pop(best_aligned_key, None)
                head, _middle, tail = best_aligned_parts
                pooled_settings = SegmentationSettings(
                    max_segments=max_segments,
                    max_chars_per_segment=max_chars_per_segment,
                    ellipsis_pause_min_chars=ellipsis_pause_min_chars,
                    force_split_symbols=list(force_split_symbols or []),
                    no_split_around=list(no_split_around or []),
                    merge_short_tail=merge_short_tail,
                    short_tail_threshold=short_tail_threshold,
                    strip_thinking_tags=remove_thinking,
                )
                segments = list(best_aligned_entry.segments)
                segments = self._prepend_aligned_parts(
                    segments,
                    head,
                    pooled_settings,
                )
                segments = self._append_aligned_parts(
                    segments,
                    tail,
                    pooled_settings,
                )
                # 同 _pop 前缀/对齐分支：补切预算「至少保留 1 段」可能
                # 让总段数超出 max_segments，强制合并兜底保证段数达标。
                if max_segments > 0 and len(segments) > max_segments:
                    segments = stabilize_segment_lengths(
                        segments,
                        max_segments=max_segments,
                        max_chars_per_segment=max_chars_per_segment,
                        pause_min_chars=ellipsis_pause_min_chars,
                    )
                return segments

        # 3. 终极自愈兜底：本地纯逻辑高精切分
        if len(outbound_text) >= min_length and not is_action_only_text(outbound_text):
            # 直接复用 segmentation.py 中封装的高精度切分函数
            local_segs = local_fallback_split(
                outbound_text,
                max_segments,
                max_chars_per_segment,
                pause_min_chars=ellipsis_pause_min_chars,
                force_split_symbols=list(force_split_symbols or []),
                no_split_around=list(no_split_around or []),
                merge_short_tail=merge_short_tail,
                short_tail_threshold=short_tail_threshold,
                remove_thinking=remove_thinking,
            )
            if local_segs and len(local_segs) > 1:
                logger.info("智能分段精准哈希/拼合未中，已自动降级启动本地高精切分兜底")
                return local_segs

        return None

    def _prune_expired_prepared_segments(self) -> None:
        """清理缓存池中生命周期已结束的预存分段条目，防止内存溢出。"""
        if not self._prepared_segments:
            return
        now = time.monotonic()
        expired_keys = [
            key
            for key, entry in self._prepared_segments.items()
            if entry.expires_at <= now
        ]
        for key in expired_keys:
            self._prepared_segments.pop(key, None)
