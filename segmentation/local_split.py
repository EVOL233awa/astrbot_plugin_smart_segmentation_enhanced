"""智能分段插件内部模块。"""
from __future__ import annotations

import re

from .core import _as_nonneg_int, is_non_natural_language, normalize_segments, strip_thinking_content
from .protection import has_protected_block, is_markdown_document, safe_escape_segments, split_protected_blocks
from .boundary import _PAUSE_BREAK_MIN_CHARS, _force_split_by_symbols, _split_plain_text
from .packing import (
    _force_merge_to_max_segments,
    _hard_split_overlong,
    _is_unsplittable_segment,
    _merge_to_global_max,
    compute_target_segment_length,
    merge_short_tail_segment,
    pack_segments_to_target,
)

def _split_overlong_segment(
    segment: str,
    max_chars: int,
    pause_min_chars: int = _PAUSE_BREAK_MIN_CHARS,
) -> list[str]:
    """将单条超长消息按标点或空白二次拆分，保留原始标点。"""
    sub_segments = local_fallback_split(
        segment,
        0,
        pause_min_chars=pause_min_chars,
    )
    if len(sub_segments) <= 1:
        return _hard_split_overlong(segment, max_chars)

    result: list[str] = []
    for sub_segment in sub_segments:
        if (
            len(sub_segment) > max_chars
            and not _is_unsplittable_segment(sub_segment)
        ):
            result.extend(_hard_split_overlong(sub_segment, max_chars))
        else:
            result.append(sub_segment)
    return result


def _split_overlong_in_run(
    parts: list[str],
    max_chars: int,
    pause_min_chars: int = _PAUSE_BREAK_MIN_CHARS,
) -> list[str]:
    """对同组文本段做超长二次拆分；动作/JSON/Markdown 等受保护段保持完整。"""
    if max_chars <= 0:
        return parts
    split_parts: list[str] = []
    for segment in parts:
        if len(segment) > max_chars and not _is_unsplittable_segment(segment):
            split_parts.extend(
                _split_overlong_segment(segment, max_chars, pause_min_chars)
            )
        else:
            split_parts.append(segment)
    return split_parts


def stabilize_segment_lengths(
    segments: list[str],
    *,
    max_segments: int | str = 0,
    max_chars_per_segment: int | str = 0,
    pause_min_chars: int = _PAUSE_BREAK_MIN_CHARS,
) -> list[str]:
    """拆分超长分段并重新应用最大分段数限制，保留模型已给出的边界。"""
    max_segs = _as_nonneg_int(max_segments)
    max_chars = _as_nonneg_int(max_chars_per_segment)

    normalized = normalize_segments(segments, max_segments=0)
    if not normalized:
        return []

    runs: list[tuple[str, list[str]]] = []
    text_parts: list[str] = []
    for segment in normalized:
        if _is_unsplittable_segment(segment) or has_protected_block(segment):
            if text_parts:
                runs.append(("text", text_parts))
                text_parts = []
            runs.append(("protected", [segment]))
        else:
            text_parts.append(segment)
    if text_parts:
        runs.append(("text", text_parts))

    stabilized: list[str] = []
    for run_kind, parts in runs:
        if run_kind == "protected":
            stabilized.extend(parts)
            continue
        stabilized.extend(_split_overlong_in_run(parts, max_chars, pause_min_chars))
    stabilized = _merge_to_global_max(stabilized, max_segs, max_chars)
    stabilized = _force_merge_to_max_segments(stabilized, max_segs, max_chars)
    return stabilized


def expand_under_split_natural_segments(
    segments: list[str],
    *,
    max_segments: int | str = 0,
    pause_min_chars: int = _PAUSE_BREAK_MIN_CHARS,
) -> list[str]:
    """把模型漏切的自然语言长段按本地句末边界补齐，避免多个句子粘连。"""
    expanded = []
    for segment in segments:
        if (
            not segment
            or is_non_natural_language(segment)
            or is_markdown_document(segment)
            or has_protected_block(segment)
        ):
            expanded.append(segment)
            continue
        paragraphs = re.split(r"\n\s*\n", segment)
        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            local_segments = local_fallback_split(
                paragraph,
                0,
                pause_min_chars=pause_min_chars,
            )
            if len(local_segments) > 1:
                expanded.extend(local_segments)
            else:
                expanded.append(paragraph)
    return normalize_segments(expanded, max_segments=0)


def local_fallback_split(
    raw_text: str,
    max_segs: int,
    max_chars_per_segment: int | str = 0,
    pause_min_chars: int = _PAUSE_BREAK_MIN_CHARS,
    *,
    force_split_symbols: list[str] | None = None,
    no_split_around: list[str] | None = None,
    merge_short_tail: bool = False,
    short_tail_threshold: int = 2,
) -> list[str]:
    """终极自愈兜底：本地纯逻辑高精切分，保护 JSON/Markdown 块并平衡长度。

    force_split_symbols 非空时先按符号强制预切，每个片段独立执行正常
    切分（§5.5）；no_split_around 非空时紧跟保护词的标点切点不参与选点
    （§5.4）；merge_short_tail 开启时末段过短并入前一段（§5.3）。
    """
    max_segments = _as_nonneg_int(max_segs)
    max_chars = _as_nonneg_int(max_chars_per_segment)

    cleaned = strip_thinking_content(raw_text)
    if not cleaned:
        return []

    # 纯 JSON/代码等非自然语言整体发送；若同时含受保护块，则按块解析而非整条直发。
    if is_non_natural_language(cleaned) and not has_protected_block(cleaned):
        return safe_escape_segments([cleaned.strip()])

    # 保留 v1.3.0 Markdown 区域保护：整篇 Markdown 整体放行，
    # 避免拆成多条后耗尽分段预算，把剩余内容全部挤到末尾。
    if is_markdown_document(cleaned):
        return safe_escape_segments([cleaned.strip()])

    runs: list[tuple[str, list[str]]] = []
    text_parts: list[str] = []
    all_text_parts: list[str] = []
    for kind, chunk in split_protected_blocks(cleaned):
        if kind == "text":
            if force_split_symbols:
                # 强制预切的片段各自独立成 run 打包，防止被打包逻辑
                # 按目标长度合并回去；合并只留给硬上限兜底（§5.5）。
                if text_parts:
                    runs.append(("text", text_parts))
                    text_parts = []
                for piece in _force_split_by_symbols(chunk, force_split_symbols):
                    parts = _split_plain_text(piece, pause_min_chars)
                    if parts:
                        runs.append(("text", parts))
                        all_text_parts.extend(parts)
                continue
            parts = _split_plain_text(chunk, pause_min_chars)
            text_parts.extend(parts)
            all_text_parts.extend(parts)
        else:
            if text_parts:
                runs.append(("text", text_parts))
                text_parts = []
            runs.append(("protected", [chunk.strip()]))
    if text_parts:
        runs.append(("text", text_parts))

    sentence_lengths = [len(part) for part in all_text_parts if part.strip()]
    target = compute_target_segment_length(
        cleaned,
        max_segments,
        max_chars,
        sentence_lengths=sentence_lengths,
    )
    packed: list[str] = []
    for run_kind, parts in runs:
        if run_kind == "protected":
            packed.extend(parts)
            continue
        packed.extend(
            pack_segments_to_target(
                _split_overlong_in_run(parts, max_chars, pause_min_chars),
                target,
                max_segments=max_segments,
                max_chars_per_segment=max_chars,
                no_split_around=no_split_around,
            )
        )
    if max_segments > 0:
        packed = _merge_to_global_max(packed, max_segments, max_chars)
        packed = _force_merge_to_max_segments(packed, max_segments, max_chars)
    if merge_short_tail:
        packed = merge_short_tail_segment(packed, max_chars, short_tail_threshold)
    return safe_escape_segments(packed)
