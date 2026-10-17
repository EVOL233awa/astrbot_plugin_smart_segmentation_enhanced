"""智能分段插件内部模块。"""
from __future__ import annotations

import math

from astrbot.api import logger

from .core import _as_nonneg_int, is_action_only_text, is_ascii_alnum, is_non_natural_language
from .protection import has_protected_block, is_markdown_document
from .boundary import (
    _ends_at_hard_sentence,
    _force_split_by_symbols,
    _MIN_FRAGMENT_CHARS,
    _normalize_cut_words,
    _no_split_guard,
    _pick_priority_cut,
)


# 单次打包允许的最大切分次数，纯属防御上限，正常输入远达不到。
_MAX_CUT_ITERATIONS = 4096

# 切点两侧都不短于目标长度的该比例时才算「均衡切点」。
_CUT_BALANCED_RATIO = 0.5

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
    left_body = left.rstrip()
    right_body = right.lstrip()
    left_ws = left[len(left_body):]
    right_ws = right[: len(right) - len(right_body)]
    # 硬上限合并也不能把行/段落边界粘掉：任一侧带换行时保留一个换行。
    if "\n" in left_ws or "\n" in right_ws:
        return left_body + "\n" + right_body
    return _join_adjacent(left_body, right_body, 0)


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


def _split_at_priority_cut(
    text: str,
    max_chars: int,
    no_split_around: list[str] | None = None,
) -> tuple[str, str] | None:
    """按标点优先级在超长 text 内切出一段，返回 (前段, 后段)。

    只服务硬单条长度约束：text 超过 max_chars 时，在标点候选里选离
    单条上限最近的切点，尽量减少段数；找不到可用候选时返回 None，
    由调用方走纯物理拆分兜底。no_split_around 保护的切点不参与选点。
    """
    if not _should_priority_cut(text, max_chars):
        return None
    position = _pick_priority_cut(
        text,
        max_chars,
        max_chars,
        max(_MIN_FRAGMENT_CHARS, round(max_chars * _CUT_BALANCED_RATIO)),
        no_split_around=no_split_around,
    )
    if position is None:
        return None
    head = text[:position].strip()
    tail = text[position:].lstrip()
    if not head or not tail:
        return None
    return head, tail


def _should_priority_cut(text: str, max_chars: int) -> bool:
    """判断片段是否值得按标点切分：仅当超过硬单条长度。

    目标长度只驱动合并（join 到 target 封口），不驱动句内切分——
    段落由完整句子组成是更高优先级，超过目标长度的完整句子保持完整。
    """
    return max_chars > 0 and len(text) > max_chars


def _cut_text_to_max(
    text: str,
    max_chars: int,
    no_split_around: list[str] | None = None,
) -> list[str]:
    """把普通文本按标点优先级切到每段不超过硬单条长度；未超限时原样返回。"""
    current = text.strip()
    if not current:
        return []
    parts: list[str] = []
    iterations = 0
    while max_chars > 0 and len(current) > max_chars and iterations < _MAX_CUT_ITERATIONS:
        split = _split_at_priority_cut(current, max_chars, no_split_around)
        if split is None:
            break
        head, tail = split
        if len(tail) >= len(current):
            break
        parts.append(head)
        current = tail
        iterations += 1
    if current:
        if max_chars > 0 and len(current) > max_chars:
            # 标点切分压不到 max_chars 以内时，交回既有的纯物理超长拆分，
            # 保证单段不超过 max_chars（受保护块与原子段不经过本函数）。
            parts.extend(_hard_split_overlong(current, max_chars))
        else:
            parts.append(current)
    return [part for part in parts if part]


def pack_segments_to_target(
    segments: list[str],
    target: int,
    max_segments: int | str = 0,
    max_chars_per_segment: int | str = 0,
    no_split_around: list[str] | None = None,
) -> list[str]:
    """把原子段按句子边界打包到目标长度附近，段落由完整句子组成。

    合并在完整句子之间进行（join 到 target 封口），不在跨句合并块内部
    做句内切分；句内标点切分只用于单个句子超过硬单条长度的场景。
    动作段与受保护块保持原子性，硬上限仍由既有逻辑兜底。
    no_split_around 非空时，紧跟保护词的标点切点不参与选点（§5.4），
    但硬上限兜底的物理拆分不受其约束。
    """
    max_segs = _as_nonneg_int(max_segments)
    max_chars = _as_nonneg_int(max_chars_per_segment)
    effective_target = max(1, int(target))

    # 不在此处 strip：片段边缘的空白/换行是 ASCII 句间空格与段落分隔的
    # 唯一载体，_join_adjacent 依赖它决定是否补空格或保留换行。只在最终
    # 输出时清理首尾。
    cleaned = [seg for seg in segments if seg and seg.strip()]
    if not cleaned:
        return []

    packed = _pack_with_priority_cuts(
        cleaned, effective_target, max_chars, allow_cut=True,
        no_split_around=no_split_around,
    )
    if max_segs > 0 and len(packed) > max_segs:
        # 标点切分把段数顶过 max_segments 时回退到不切分的原贪心结果：
        # 否则硬上限阶段会强制合并出超过 max_chars_per_segment 的段，属于退步。
        plain = _pack_with_priority_cuts(
            cleaned, effective_target, max_chars, allow_cut=False,
            no_split_around=no_split_around,
        )
        if len(plain) < len(packed):
            packed = plain

    if max_segs > 0:
        packed = _merge_to_global_max(packed, max_segs, max_chars or effective_target)
        if len(packed) > max_segs:
            forced = _force_merge_to_max_segments(
                packed,
                max_segs,
                max_chars or effective_target,
            )
            # 强制合并只有真的产生装不下的段时才尝试拼回重排，
            # 避免重排改写大量本可合规收场的布局。
            if _has_overlong_ordinary_segment(forced, max_chars):
                repacked = _repack_within_caps(
                    packed, effective_target, max_segs, max_chars,
                    no_split_around=no_split_around,
                )
            else:
                repacked = None
            if repacked is None:
                if _has_overlong_ordinary_segment(forced, max_chars):
                    logger.warning(
                        "文本长度超过 max_segments * max_chars_per_segment，"
                        "无法同时满足硬段数与硬单条长度；已优先避免超长尾段"
                    )
                packed = forced
            else:
                packed = repacked
    return [segment.strip() for segment in packed if segment.strip()]


def _has_overlong_ordinary_segment(segments: list[str], max_chars: int) -> bool:
    """是否存在会破坏硬单条长度的普通段（受保护块与原子段豁免）。"""
    if max_chars <= 0:
        return False
    return any(
        len(segment) > max_chars and not _is_unsplittable_segment(segment)
        for segment in segments
    )


def _pack_with_priority_cuts(
    cleaned: list[str],
    target: int,
    max_chars: int,
    *,
    allow_cut: bool,
    no_split_around: list[str] | None = None,
) -> list[str]:
    """按目标长度打包已清洗的原子段；allow_cut 为假时保持原贪心边界。"""

    def _flush(current_text: str) -> list[str]:
        if not current_text:
            return []
        if allow_cut:
            return _cut_text_to_max(current_text, max_chars, no_split_around)
        # 不切分模式只做硬单条长度兜底，不做标点优先选点，
        # 保证该分支输出的是最接近原贪心边界的布局。
        if max_chars > 0 and len(current_text) > max_chars:
            return _hard_split_overlong(current_text, max_chars)
        return [current_text]

    packed: list[str] = []
    current = ""
    for segment in cleaned:
        if is_action_only_text(segment):
            if current:
                packed.extend(_flush(current))
                current = ""
            packed.append(segment)
            continue
        if not current:
            current = segment
            continue
        # 2026-09-13 用户裁定：省略号开头的段是独立停顿起手（「……是老师
        # 画的吗。」），不并入前段，避免「句号+省略号」符号连排在段尾。
        if segment.lstrip().startswith(("…", "...")):
            packed.extend(_flush(current))
            current = segment
            continue
        if _ends_at_hard_sentence(current):
            packed.extend(_flush(current))
            current = segment
            continue
        candidate = _join_adjacent(current, segment, target)
        if len(candidate) <= target:
            current = candidate
            continue
        # 合并块超目标长度时在句边界封口，不做跨句的句内切分：
        # 段落始终由完整句子组成，句内切分只保留给单个超长句子
        # （由 _flush 的 _cut_text_to_max 在该句子内按标点降级处理）。
        packed.extend(_flush(current))
        current = segment
    if current:
        packed.extend(_flush(current))
    return packed


def _repack_within_caps(
    segments: list[str],
    target: int,
    max_segments: int,
    max_chars: int,
    no_split_around: list[str] | None = None,
) -> list[str] | None:
    """合并死锁且容量可行时的自救：拼回长文本按标点重新切分。

    切分布局可能让「连续划分成 max_segments 个不超过 max_chars 的块」无解，
    此时把同 run 的文本段按原顺序拼回，再按标点优先级与硬单条长度重切。
    每轮的目标长度取 max(target, 剩余均摊 share)：share 更大说明容量紧张，
    必须按均摊收口才能装进剩余段数预算，硬上限优先于目标长度。
    受保护块绝不允许拼回（会被拆坏），出现即放弃；返回 None 表示无法在
    硬上限内重排，由调用方走强制合并兜底。
    """
    if max_segments <= 0 or max_chars <= 0:
        return None
    for segment in segments:
        if (
            has_protected_block(segment)
            or is_markdown_document(segment)
            or is_non_natural_language(segment)
            or is_action_only_text(segment)
        ):
            return None
    total = sum(len(segment) for segment in segments)
    if total > max_segments * max_chars:
        return None
    joined = ""
    for segment in segments:
        joined = _join_adjacent(joined, segment)
    if not joined:
        return None

    parts: list[str] = []
    current = joined
    remaining = max_segments
    while remaining > 1 and len(current) > max_chars:
        share = -(-len(current) // remaining)
        goal = max(target, share)
        # 切点必须给尾部留出剩余段数装得下的空间，否则尾段会越过硬单条长度。
        min_position = len(current) - (remaining - 1) * max_chars
        position = _pick_priority_cut(
            current,
            goal,
            max_chars,
            max(_MIN_FRAGMENT_CHARS, round(share * _CUT_BALANCED_RATIO)),
            min_position,
            no_split_around=no_split_around,
        )
        if position is None:
            # 整块没有任何标点/空白切点：按均摊长度物理切，保证推进。
            chunks = _hard_split_overlong(current, share)
            if len(chunks) <= 1:
                return None
            parts.extend(chunks[:-1])
            current = chunks[-1]
            remaining -= len(chunks) - 1
            if remaining < 1:
                return None
            continue
        head = current[:position].strip()
        tail = current[position:].lstrip()
        if not head or not tail or len(tail) >= len(current):
            return None
        parts.append(head)
        current = tail
        remaining -= 1
    parts.append(current)
    if len(parts) > max_segments:
        return None
    if any(len(part) > max_chars for part in parts):
        return None
    return parts


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


def merge_short_tail_segment(
    segments: list[str],
    max_chars: int,
    threshold: int,
) -> list[str]:
    """短尾合并（§5.3）：所有切分与约束完成后只检查最后一段。

    末段长度 ≤ threshold 且前后两段都不是原子块/受保护块时并入前一段；
    合并后超过 max_chars 则放弃（硬单条长度优先）。单次合并，不递归。
    """
    if threshold <= 0 or len(segments) < 2:
        return list(segments)
    tail = segments[-1].strip()
    previous = segments[-2].strip()
    if not tail or len(tail) > threshold:
        return list(segments)
    if (
        _is_unsplittable_segment(tail)
        or _is_unsplittable_segment(previous)
        or has_protected_block(tail)
        or has_protected_block(previous)
    ):
        return list(segments)
    merged = _join_adjacent(previous, tail, 0)
    if max_chars > 0 and len(merged) > max_chars:
        return list(segments)
    return segments[:-2] + [merged]


def apply_no_split_boundaries(
    segments: list[str],
    no_split_around: list[str] | None,
    max_chars: int = 0,
) -> list[str]:
    """llm 引擎重建后的确定性边界过滤（§5.4）。

    重建边界之后紧跟保护词（允许中间有空白，全半角等价）时移除该边界，
    把相邻两段按原顺序合回。原子块与受保护块不参与合并；合并后超过
    max_chars 时保持原边界（硬上限优先于软约束）。
    """
    guarded_keys, max_key_len = _normalize_cut_words(no_split_around)
    if not guarded_keys or len(segments) < 2:
        return list(segments)
    result = [segments[0]]
    for segment in segments[1:]:
        previous = result[-1]
        guarded = _no_split_guard(segment, 0, guarded_keys, max_key_len)
        mergeable = (
            guarded
            and not _is_unsplittable_segment(previous)
            and not _is_unsplittable_segment(segment)
            and not has_protected_block(previous)
            and not has_protected_block(segment)
        )
        if mergeable:
            merged = _join_adjacent(previous, segment, 0)
            if max_chars <= 0 or len(merged) <= max_chars:
                result[-1] = merged
                continue
        result.append(segment)
    return result


def apply_force_split_to_segments(
    segments: list[str],
    symbols: list[str] | None,
) -> list[str]:
    """llm 引擎重建后的确定性强制再切分（§5.5）。

    对每个非受保护片段按强制符号切开（符号保留在前段末尾）；受保护块
    与原子段保持完整。切分后的段数与长度超限由后续硬上限逻辑兜底。
    """
    if not symbols:
        return list(segments)
    result: list[str] = []
    for segment in segments:
        if _is_unsplittable_segment(segment) or has_protected_block(segment):
            result.append(segment)
            continue
        result.extend(_force_split_by_symbols(segment, symbols))
    return result


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
