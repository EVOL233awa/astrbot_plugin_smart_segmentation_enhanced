"""智能分段插件内部模块。"""
from __future__ import annotations

import ast
import json
import re
from typing import Any

from .core import _LEADING_PUNCTUATION_RE, _SEGMENT_LEADING_PUNCT_CHARS, _as_nonneg_int, is_action_only_text, is_kaomoji_or_pure_punct, is_leading_punctuation, normalize_content_key, normalize_segments, strip_thinking_content
from .protection import safe_escape_segments
from .boundary import (
    _LEFT_BOUNDARY_CHARS,
    _PAUSE_BREAK_MIN_CHARS,
    _SENTENCE_BOUNDARY_CHARS,
    _top_level_bracket_spans,
    split_segments_at_bracket_boundaries,
)
from .local_split import expand_under_split_natural_segments, local_fallback_split, stabilize_segment_lengths
from .packing import (
    apply_force_split_to_segments,
    apply_no_split_boundaries,
    merge_short_tail_segment,
)

STYLE_GUIDES = {
    "natural": "像和朋友微信聊天一样自然地分条发送。有的消息短有的长，节奏随意。",
    "conservative": "偏沉稳的发消息风格，一条消息说比较完整的内容，不会频繁发短消息。",
    "active": "活泼的发消息风格，喜欢发短消息连击，反应词和正文分开发。",
}


def extract_json_array_text(raw_text: str) -> str:
    """从模型返回中提取 JSON 数组文本。"""
    result_text = str(raw_text or "").strip()
    if "```json" in result_text:
        return result_text.split("```json", 1)[1].split("```", 1)[0].strip()
    if "```" in result_text:
        return result_text.split("```", 1)[1].split("```", 1)[0].strip()
        
    start = result_text.find("[")
    end = result_text.rfind("]")
    if start != -1 and end != -1 and start < end:
        return result_text[start : end + 1]
        
    return result_text


def robust_json_loads(json_str: str) -> Any:
    """针对小模型进行高容错率的 JSON 解析，利用 ast.literal_eval 兼容单引号。"""
    s = json_str.strip()
    if not s:
        raise ValueError("Empty string")
        
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
        
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'")
    
    try:
        return ast.literal_eval(s)
    except Exception:
        pass
        
    s = re.sub(r",\s*]$", "]", s)
    return json.loads(s)


def align_prepared_in_outbound(
    outbound_text: str,
    prepared_text: str,
) -> tuple[str, str, str] | None:
    """在 outbound 文本中按内容签名定位 prepared 文本，返回（头部, prepared原文段, 尾部）。

    Tool Loop / 中间插件改写场景会在 prepared 文本头部或尾部插入其他内容，
    签名（忽略空白、大小写与标点）定位可跨越这些插入。双向签名校验失败或
    prepared 签名过短时返回 None，调用方应保守降级到本地切分。
    """
    outbound = str(outbound_text or "")
    prepared = str(prepared_text or "")
    if not outbound or not prepared:
        return None
    outbound_key = normalize_content_key(outbound)
    prepared_key = normalize_content_key(prepared)
    if not prepared_key or len(prepared_key) < 4:
        return None
    index = outbound_key.find(prepared_key)
    if index == -1:
        return None

    def _pos_after_alnum_count(text: str, start: int, count: int) -> int:
        """返回原文位置：从 start 起消耗 count 个 alnum 字符后的位置，不足返回 -1。"""
        seen = 0
        for pos in range(start, len(text)):
            if text[pos].isalnum():
                seen += 1
                if seen == count:
                    return pos + 1
        return -1

    head_end = 0
    if index > 0:
        head_end = _pos_after_alnum_count(outbound, 0, index)
        if head_end == -1:
            return None
        while head_end < len(outbound) and not outbound[head_end].isalnum():
            head_end += 1
        # 吸收区间（head 末尾标点）若与 prepared 原文开头重叠（如 prepared 以
        # "（"开头），重叠部分归还 middle，避免标点在 head 与 prepared 段中重复。
        prep_lead = prepared.lstrip()
        absorb = outbound[:head_end]
        for k in range(min(len(prep_lead), len(absorb), 8), 0, -1):
            if absorb.endswith(prep_lead[:k]):
                head_end -= k
                break

    middle_end = _pos_after_alnum_count(outbound, head_end, len(prepared_key))
    if middle_end == -1:
        return None
    raw_middle_end = middle_end
    while middle_end < len(outbound) and not outbound[middle_end].isalnum():
        middle_end += 1
    # 吸收区间（prepared 末尾标点）与 prepared 原文结尾重叠的部分保留在
    # middle，避免标点被划给 tail 造成内容重复。
    prep_tail = prepared.rstrip()
    absorb_tail = outbound[raw_middle_end:middle_end]
    for k in range(min(len(prep_tail), len(absorb_tail), 8), 0, -1):
        if absorb_tail.startswith(prep_tail[-k:]):
            middle_end = raw_middle_end + k
            break

    head = outbound[:head_end]
    middle = outbound[head_end:middle_end]
    tail = outbound[middle_end:]

    if normalize_content_key(head) != outbound_key[:index]:
        return None
    if normalize_content_key(middle) != prepared_key:
        return None
    if normalize_content_key(tail) != outbound_key[index + len(prepared_key):]:
        return None
    return head, middle, tail


def _find_model_segment_start(original: str, raw_start: int) -> int:
    """Locate the true start of a model segment, keeping leading punctuation with it."""
    start = raw_start

    punct_start = start
    while punct_start > 0 and original[punct_start - 1] in _SEGMENT_LEADING_PUNCT_CHARS:
        punct_start -= 1
    leading_punct = original[punct_start:start]
    if (
        leading_punct
        and _LEADING_PUNCTUATION_RE.match(leading_punct)
        and (
            punct_start == 0
            or original[punct_start - 1].isspace()
            or original[punct_start - 1] in _LEFT_BOUNDARY_CHARS
        )
    ):
        start = punct_start

    while start > 0 and original[start - 1].isspace():
        start -= 1

    if start > 0:
        prev = original[start - 1]
        if prev in _LEFT_BOUNDARY_CHARS:
            start -= 1
        elif prev in {'"', "'"}:
            if (
                start == 1
                or original[start - 2].isspace()
                or original[start - 2] in _LEFT_BOUNDARY_CHARS
            ):
                start -= 1
    return start


def reconstruct_segments_from_model_output(
    model_segments: list[str],
    original_text: str,
    max_segments: int | str = 0,
) -> list[str] | None:
    """用模型输出作为切分边界，从原始回复重建带完整标点的分段。"""
    max_segs = _as_nonneg_int(max_segments)

    original = str(original_text or "")
    if not original or not model_segments:
        return None

    orig_chars = [
        (char, index) for index, char in enumerate(original) if char.isalnum()
    ]
    if not orig_chars:
        return None

    orig_sig = "".join(char.lower() for char, _ in orig_chars)
    orig_positions = [index for _, index in orig_chars]
    matches: list[tuple[int, int]] = []
    cursor = 0

    for segment in model_segments:
        seg_sig = normalize_content_key(segment)
        if not seg_sig:
            continue
        pos = orig_sig.find(seg_sig, cursor)
        if pos == -1:
            return None
        matches.append((pos, pos + len(seg_sig)))
        cursor = pos + len(seg_sig)

    if not matches:
        return None

    starts = [0]
    for i in range(1, len(matches)):
        raw_start = orig_positions[matches[i][0]]
        start = _find_model_segment_start(original, raw_start)
        starts.append(start)

    spans = _top_level_bracket_spans(original)
    protected_starts = [0]
    for start in starts[1:]:
        if any(span_start < start < span_end for span_start, span_end in spans):
            continue
        protected_starts.append(start)

    bounds = set(protected_starts)
    for span_start, span_end in spans:
        # 句中括号（如"取件码（3-2-1874）还是"）不是段边界，避免原句被拦腰切断；
        # 位于文本开头或句末标点之后的括号才允许独立成段。
        if span_start > 0:
            prev_text = original[:span_start].rstrip()
            if prev_text and prev_text[-1] not in _SENTENCE_BOUNDARY_CHARS:
                continue
        bounds.add(span_start)
        bounds.add(span_end)
    ordered_bounds = sorted(bounds)

    segments = []
    for i in range(len(ordered_bounds) - 1):
        start = ordered_bounds[i]
        end = ordered_bounds[i + 1]
        if start >= end:
            continue
        part = original[start:end].strip()
        if part:
            segments.append(part)
    if ordered_bounds and ordered_bounds[-1] < len(original):
        part = original[ordered_bounds[-1]:].strip()
        if part:
            segments.append(part)

    if not segments:
        return None
    if max_segs > 0 and len(segments) > max_segs:
        return normalize_segments(segments, max_segs)
    return segments


def mask_pipe_in_text(text: str) -> tuple[str, str]:
    """把原文中的 | 替换为无歧义 alnum 占位符，返回 (masked_text, placeholder)。"""
    placeholder = "RAWPIPE"
    while placeholder in text:
        placeholder += "0"
    return text.replace("|", placeholder), placeholder


def restore_pipe_in_segments(segments: list[str], placeholder: str) -> list[str]:
    """把分段结果中的占位符还原为原始 |。"""
    return [seg.replace(placeholder, "|") for seg in segments]


def build_segmentation_prompt(text: str, style: str, max_segments: int) -> str:
    """构建极简分段提示词，保留 3 条 few-shot 示例锁定输出格式。"""
    style_guide = STYLE_GUIDES.get(style, STYLE_GUIDES["natural"])
    return f"""把输入拆成适合聊天分条的消息。不总结、不删字、不改标点，每条用 " | " 分隔。圆括号动作单独一条，括号内即使跨行或超长也禁止插入分隔符。以。！？结尾的完整句子之间必须拆开。最多 {max_segments} 条。风格：{style_guide}

示例：
输入：（开心）今天发工资了，要不要一起去吃大餐？
输出：（开心） | 今天发工资了，要不要一起去吃大餐？

输入：欸？！><
今天天气真好...qwq
输出：欸？！>< | 今天天气真好...qwq

输入：（点头）嗯，我知道了。那我们先按计划执行。
输出：（点头） | 嗯，我知道了。 | 那我们先按计划执行。

输入：
{text}"""


def parse_raw_model_segments(
    raw_text: str,
    *,
    fallback_text: str,
) -> list[str]:
    """把模型原始输出解析为清洗后的分段（缝合颜文字 + 幻觉检测）。

    parse_segments_from_model_output 与锚点路径共用的解析前置；模型输出
    复读 few-shot 示例时抛 ValueError，由调用方决定降级方式。
    """
    cleaned = strip_thinking_content(raw_text).strip()

    if "|" in cleaned:
        raw_segs = re.split(r"\s*\|\s*", cleaned)
        segments = [seg.strip() for seg in raw_segs if seg.strip()]
    elif cleaned.startswith("[") and cleaned.endswith("]"):
        json_text = extract_json_array_text(cleaned)
        segments = robust_json_loads(json_text)
    else:
        segments = [line.strip() for line in cleaned.splitlines() if line.strip()]

    normalized = normalize_segments(segments, max_segments=0)

    # 防御大模型切碎颜文字的后处理缝合
    final_segs = []
    for seg in normalized:
        if final_segs and is_kaomoji_or_pure_punct(seg):
            final_segs[-1] += seg
        else:
            final_segs.append(seg)
    normalized = final_segs

    # 复读 Few-shot 幻觉检测与降级机制
    joined_result = "".join(normalized)
    hallucination_keywords = [
        "今天发工资了",
        "要不要一起去吃大餐",
        "今天天气真好",
        "先按计划执行",
    ]
    if any(kw in joined_result and kw not in fallback_text for kw in hallucination_keywords):
        raise ValueError("Detected few-shot hallucination")
    return normalized


def parse_segments_from_model_output(
    raw_text: str,
    *,
    fallback_text: str,
    max_segments: int | str,
    max_chars_per_segment: int | str = 0,
    pause_min_chars: int = _PAUSE_BREAK_MIN_CHARS,
    force_split_symbols: list[str] | None = None,
    no_split_around: list[str] | None = None,
    merge_short_tail: bool = False,
    short_tail_threshold: int = 2,
) -> list[str]:
    """解析模型分段输出。强类型防御机制包装。"""
    max_segs = _as_nonneg_int(max_segments)

    local_kwargs: dict[str, Any] = {
        "force_split_symbols": force_split_symbols,
        "no_split_around": no_split_around,
        "merge_short_tail": merge_short_tail,
        "short_tail_threshold": short_tail_threshold,
    }

    try:
        normalized = parse_raw_model_segments(
            raw_text,
            fallback_text=fallback_text,
        )

        reconstructed = reconstruct_segments_from_model_output(
            normalized,
            fallback_text,
            max_segments=0,
        )
        if reconstructed is None:
            return local_fallback_split(
                fallback_text,
                max_segs,
                max_chars_per_segment,
                pause_min_chars=pause_min_chars,
                **local_kwargs,
            )
        if len(reconstructed) == 1 and len(normalized) > 1:
            return local_fallback_split(
                fallback_text,
                max_segs,
                max_chars_per_segment,
                pause_min_chars=pause_min_chars,
                **local_kwargs,
            )
        normalized = expand_under_split_natural_segments(
            reconstructed,
            max_segments=0,
            pause_min_chars=pause_min_chars,
        )

    except Exception:
        fallback = strip_thinking_content(fallback_text).strip()
        return local_fallback_split(
            fallback,
            max_segs,
            max_chars_per_segment,
            pause_min_chars=pause_min_chars,
            **local_kwargs,
        )

    split_result = split_segments_at_bracket_boundaries(
        normalized,
        max_segments=0,
    )

    # 防止大模型或括号切分将颜文字剥离；动作段后的纯标点先缓存，
    # 再归属下一个内容段，避免粘回动作后被后续打包与说话内容重新合并。
    final_merged = []
    pending_leading = ""
    for seg in split_result:
        if pending_leading:
            seg = pending_leading + seg
            pending_leading = ""
        if final_merged and is_leading_punctuation(seg):
            pending_leading = seg
            continue
        if final_merged and is_kaomoji_or_pure_punct(seg):
            if is_action_only_text(final_merged[-1]):
                pending_leading = seg
                continue
            final_merged[-1] += seg
        else:
            final_merged.append(seg)
    if pending_leading:
        if final_merged:
            final_merged[-1] += pending_leading
        else:
            final_merged.append(pending_leading)

    # §5.4：重建边界后紧跟保护词的软约束过滤，先于强制再切分执行，
    # 保证强制切点（更高优先级）不会被边界过滤合回。
    final_merged = apply_no_split_boundaries(
        final_merged,
        no_split_around,
        max_chars=_as_nonneg_int(max_chars_per_segment),
    )
    # §5.5：对非受保护片段按强制符号做一次确定性再切分（仍只调用一次 LLM）。
    final_merged = apply_force_split_to_segments(final_merged, force_split_symbols)

    # 段数硬上限无条件兜底：max_chars=0 时 stabilize 只做段数合并，
    # 不做超长拆分，保证 max_segments 在任何参数组合下都生效。
    final_merged = stabilize_segment_lengths(
        final_merged,
        max_segments=max_segs,
        max_chars_per_segment=max_chars_per_segment,
        pause_min_chars=pause_min_chars,
    )
    if merge_short_tail:
        final_merged = merge_short_tail_segment(
            final_merged,
            _as_nonneg_int(max_chars_per_segment),
            short_tail_threshold,
        )

    return safe_escape_segments(final_merged)
