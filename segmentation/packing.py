"""智能分段插件内部模块。"""
from __future__ import annotations

import math

from .core import _LOG, _as_nonneg_int, is_action_only_text, is_ascii_alnum, is_non_natural_language
from .protection import has_protected_block, is_markdown_document
from .boundary import _HARD_SENTENCE_END_CHARS

def compute_target_segment_length(
    text: str,
    max_segments: int | str = 0,
    max_chars_per_segment: int | str = 0,
    sentence_lengths: list[int] | None = None,
) -> int:
    """Compute the code-layer target length for ordinary chat segments."""
    max_segs = _as_nonneg_int(max_segments)
    max_chars = _as_nonneg_int(max_chars_per_segment)

    total = len(str(text or ""))
    if sentence_lengths:
        avg = sum(sentence_lengths) / len(sentence_lengths)
        target = round(avg * 1.5)
    else:
        target = max(90 + round(total * 0.06), 100)
    target = max(target, 20)
    if max_segs > 0:
        target = max(target, math.ceil(total / max_segs))
    if max_chars > 0:
        target = min(target, max_chars)
    return max(1, target)


def _join_adjacent(left: str, right: str, limit: int = 0) -> str:
    """Join two segments without physically gluing ASCII words together."""
    if not left:
        return right
    if not right:
        return left
    if left[-1].isspace() or right[0].isspace():
        return left + right
    if is_ascii_alnum(left[-1]) and is_ascii_alnum(right[0]):
        spaced = left + " " + right
        if limit <= 0 or len(spaced) <= limit:
            return spaced
    return left + right


def _merge_to_global_max(
    segments: list[str],
    max_segments: int,
    max_chars: int,
) -> list[str]:
    """全局合并普通文本段以满足硬段数，同时保护动作段与受保护块不被合并。"""
    if max_segments <= 0 or len(segments) <= max_segments:
        return list(segments)
    result = list(segments)
    while len(result) > max_segments:
        best_index: int | None = None
        best_length: int | None = None
        for index in range(len(result) - 1):
            left = result[index]
            right = result[index + 1]
            if (
                is_action_only_text(left)
                or is_action_only_text(right)
                or _is_unsplittable_segment(left)
                or _is_unsplittable_segment(right)
            ):
                continue
            merged = _join_adjacent(left, right, max_chars if max_chars > 0 else 0)
            if max_chars > 0 and len(merged) > max_chars:
                continue
            if best_length is None or len(merged) < best_length:
                best_index = index
                best_length = len(merged)
        if best_index is None:
            break
        result[best_index] = _join_adjacent(
            result[best_index],
            result[best_index + 1],
            max_chars if max_chars > 0 else 0,
        )
        del result[best_index + 1]
    return result


def _force_join_for_cap(left: str, right: str) -> str:
    """Join adjacent segments for the final hard cap without losing content.

    受保护块（JSON/代码等）之间保留空行分隔以维持结构可读性；
    动作段与普通正文直接连排，避免聊天气泡内出现无意义空行。
    """
    if has_protected_block(left) or has_protected_block(right):
        return left.rstrip() + "\n\n" + right.lstrip()
    return _join_adjacent(left.rstrip(), right.lstrip(), 0)


def _force_merge_to_max_segments(
    segments: list[str],
    max_segments: int,
    max_chars: int = 0,
) -> list[str]:
    """Final fallback that guarantees the segment count hard cap is met."""
    if max_segments <= 0 or len(segments) <= max_segments:
        return list(segments)

    result = list(segments)
    while len(result) > max_segments:
        best_index: int | None = None
        best_length: int | None = None
        for index in range(len(result) - 1):
            merged = _force_join_for_cap(result[index], result[index + 1])
            if best_length is None or len(merged) < best_length:
                best_index = index
                best_length = len(merged)
        if best_index is None:
            break
        result[best_index] = _force_join_for_cap(
            result[best_index],
            result[best_index + 1],
        )
        del result[best_index + 1]
    return result


def pack_segments_to_target(
    segments: list[str],
    target: int,
    max_segments: int | str = 0,
    max_chars_per_segment: int | str = 0,
) -> list[str]:
    """Pack atomic segments by target length without dumping a long tail."""
    max_segs = _as_nonneg_int(max_segments)
    max_chars = _as_nonneg_int(max_chars_per_segment)

    cleaned = [seg.strip() for seg in segments if seg and seg.strip()]
    if not cleaned:
        return []

    packed: list[str] = []
    current = ""
    for segment in cleaned:
        if is_action_only_text(segment):
            if current:
                packed.append(current)
                current = ""
            packed.append(segment)
            continue
        if not current:
            current = segment
            continue
        if _HARD_SENTENCE_END_CHARS.intersection(current.rstrip()[-4:]):
            packed.append(current)
            current = segment
            continue
        candidate = _join_adjacent(current, segment, target)
        if len(candidate) <= target:
            current = candidate
        else:
            packed.append(current)
            current = segment
    if current:
        packed.append(current)

    if max_segs > 0:
        packed = _merge_to_global_max(packed, max_segs, max_chars or target)
        if len(packed) > max_segs:
            _LOG.warning(
                "文本长度超过 max_segments * max_chars_per_segment，"
                "无法同时满足硬段数与硬单条长度；已优先避免超长尾段"
            )
            packed = _force_merge_to_max_segments(
                packed,
                max_segs,
                max_chars or target,
            )
    return packed


def _is_unsplittable_segment(segment: str) -> bool:
    """Return True for protected JSON/Markdown segments that must not be cut."""
    stripped = str(segment or "").strip()
    if not stripped:
        return True
    return (
        is_action_only_text(stripped)
        or is_non_natural_language(stripped)
        or is_markdown_document(stripped)
    )


def _hard_split_overlong(segment: str, max_chars: int) -> list[str]:
    """Split a long ordinary segment at punctuation or whitespace."""
    parts = []
    remaining = segment
    while len(remaining) > max_chars:
        cut = max_chars
        for separator in ("\n", "，", ",", "、", "；", ";", "。", "！", "？", " "):
            idx = remaining.rfind(separator, 0, cut)
            if idx > max_chars // 2:
                cut = idx + len(separator)
                break
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts
