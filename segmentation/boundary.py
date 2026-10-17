"""智能分段插件内部模块。"""
from __future__ import annotations

import re
import unicodedata

from .core import BRACKET_PAIRS, _as_nonneg_int, is_action_only_text, is_ascii_alnum, is_kaomoji_or_pure_punct
from .protection import _INLINE_CODE_RE, _LINK_IMAGE_RE

# 打包冲刷的句末边界：出现即把累积文本封口，保证完整句子独立成段的优先级。
# 省略号是明确的停顿标记，……之后的文本不再与省略号前的内容同段。
_HARD_SENTENCE_END_CHARS = frozenset("！？!?…")

# 段尾允许出现在终止标点之后的收尾符号，判断句末边界时向后跳过。
_CLOSING_TRAIL_CHARS = frozenset('"\')》】」』＞>】"\'）)')

# 成对引号：引号内部的句末标点不产生切点，避免长引语被拦腰切断。
# 只在后方确实存在闭合引号时才进入引号深度，孤立引号按普通字符处理。
_QUOTE_PAIRS: dict[str, str] = {
    "「": "」",
    "『": "』",
    "“": "”",
    "‘": "’",
    "《": "》",
}

# 强制切分符号命中后允许并吸收的标点簇字符：连续的「！！！」「……」
# 必须留在同一段末尾，不能从标点簇中间切开。
_CLUSTER_EXTEND_CHARS = frozenset("。！？!?…~.,，、;；:：")


def _ends_at_hard_sentence(text: str) -> bool:
    """结尾是否落在终止标点上（允许省略号簇与引号、括号等收尾符）。

    不用窗口交集：以省略号开头的短段（「……嗯。」）开头的 … 会落进
    末尾窗口造成误判；句中的「！」同样不该被当成段尾边界。
    """
    stripped = text.rstrip()
    index = len(stripped) - 1
    while index >= 0 and stripped[index] in _CLOSING_TRAIL_CHARS:
        index -= 1
    return index >= 0 and stripped[index] in _HARD_SENTENCE_END_CHARS


_SENTENCE_BOUNDARY_CHARS = frozenset("。！？!?~…")


_LEFT_BOUNDARY_CHARS = frozenset("（([【“‘「『《")


# 标点优先切分：一级为终止标点，二级为分号，三级为逗号顿号。
# 连续的终止标点与点号视为一个整体（？！、！！！、。。。、……、... 不得从中间切开）。
_PRIMARY_CUT_CHARS = frozenset("。！？!?…~")
_SECONDARY_CUT_CHARS = frozenset("；;")
_TERTIARY_CUT_CHARS = frozenset("，,、")
# 终止标点簇之后紧跟的停顿符跟随前一段，避免切出以标点开头的碎片段。
_TRAILING_PAUSE_CHARS = frozenset("，,、；;")
# 非末尾片段长度小于等于该值视为过短段，降为最低优先级。
_MIN_FRAGMENT_CHARS = 2

# 省略号停顿段独立成段的最小长度：低于该值（「嗯…」这类 ≤2 字符语气
# 碎片）时省略号留作句中停顿不打断；「啊……」「就是…」等 ≥3 字符的
# 停顿段照常独立。与 _conf_schema.json 的 ellipsis_pause_min_chars 同值，
# 运行时经 _split_plain_text 的参数下传。
_PAUSE_BREAK_MIN_CHARS = 4

# no_split_around 的切点后探测窗口：切点右侧跳过空白后与保护词前缀
# 比对所需的额外余量（覆盖全角空白等多字节字符）。
_NO_SPLIT_PROBE_PAD = 8


def _normalize_cut_words(words: list[str] | None) -> tuple[list[str], int]:
    """归一化保护词列表：NFKC、去空白项与去重，返回 (列表, 最长词长度)。"""
    if not words:
        return [], 0
    normalized: list[str] = []
    for word in words:
        key = unicodedata.normalize("NFKC", word).strip()
        if key and key not in normalized:
            normalized.append(key)
    return normalized, max((len(key) for key in normalized), default=0)


def _no_split_guard(
    text: str,
    position: int,
    guarded_keys: list[str],
    max_key_len: int,
) -> bool:
    """切点 position 是否受 no_split_around 保护（命中则不切）。

    切点右侧先跳过空白，再检查是否以任一保护词开头（归一化比对，
    与 smart 延迟的符号匹配一致：全半角等价）。
    """
    if not guarded_keys:
        return False
    probe = unicodedata.normalize(
        "NFKC", text[position : position + max_key_len + _NO_SPLIT_PROBE_PAD]
    ).lstrip()
    if not probe:
        return False
    return any(probe.startswith(key) for key in guarded_keys)


def _nfkc_index_map(text: str) -> tuple[str, list[int]]:
    """逐字符 NFKC 归一化并建立位置映射。

    返回 (归一化文本, mapping)，mapping[i] 是归一化文本第 i 个字符在
    原文本中的下标；归一化膨胀（…→...）时多个位置映射到同一原下标。
    """
    norm_chars: list[str] = []
    mapping: list[int] = []
    for index, char in enumerate(text):
        normalized = unicodedata.normalize("NFKC", char)
        norm_chars.append(normalized)
        mapping.extend([index] * len(normalized))
    return "".join(norm_chars), mapping


def _has_matching_quote_close(text: str, index: int) -> bool:
    """判断 index 处的开引号后方是否存在闭合引号。"""
    close = _QUOTE_PAIRS.get(text[index])
    return bool(close) and text.find(close, index + 1) != -1


def _force_split_by_symbols(text: str, symbols: list[str] | None) -> list[str]:
    """按强制切分符号预切文本，符号保留在前段末尾；无命中返回原文本。

    符号与文本均做 NFKC 归一化后匹配（全半角等价，与 no_split_around、
    smart 延迟符号一致），切分位置映射回原文本。多符号同时收集命中
    区间，重叠时保留先出现的区间；切出的片段各自独立走后续切分，不
    在此处做长度平衡（§5.5：强制切点优先于过短段软规则，即使产生
    ≤2 字符片段也按强制切点执行）。
    """
    if not symbols:
        return [text]
    keys: list[str] = []
    for symbol in symbols:
        key = unicodedata.normalize("NFKC", str(symbol))
        if key and key not in keys:
            keys.append(key)
    if not keys:
        return [text]

    norm_text, mapping = _nfkc_index_map(text)
    hits: list[tuple[int, int]] = []
    for key in keys:
        # 标点类符号（！、？、……、逗号等）命中后吸收整个标点簇与收尾引号，
        # 避免把「！！！」切成「！」「！」「！」；分隔符类符号（| 等）保持
        # 逐次切分语义。
        is_cluster_symbol = bool(key) and all(
            char in _CLUSTER_EXTEND_CHARS for char in key
        )
        start = norm_text.find(key)
        while start >= 0:
            end = start + len(key)
            if is_cluster_symbol:
                while end < len(norm_text) and norm_text[end] in _CLUSTER_EXTEND_CHARS:
                    end += 1
                while end < len(norm_text) and norm_text[end] in _CLOSING_TRAIL_CHARS:
                    end += 1
            if not any(hit_start < end and start < hit_end for hit_start, hit_end in hits):
                hits.append((start, end))
            start = norm_text.find(key, start + 1)
    if not hits:
        return [text]
    hits.sort()

    pieces: list[str] = []
    cursor = 0
    for start, end in hits:
        orig_start = mapping[start]
        orig_end = mapping[end - 1] + 1
        if orig_start < cursor:
            continue
        piece = text[cursor:orig_end]
        # 纯标点前缀不单独成段：开头的「……」「！！！」应跟随后面的正文。
        if piece.strip() and not is_kaomoji_or_pure_punct(piece):
            pieces.append(piece)
            cursor = orig_end
    tail = text[cursor:]
    if tail.strip():
        pieces.append(tail)
    return pieces if pieces else [text]


def _terminator_cluster_end(text: str, index: int) -> int:
    """返回从 index 起的终止标点簇结束位置（独占索引）。

    ASCII 点号只有在后接空白或文本结尾时才计入，因此 `3.2`、`example.com`、
    文件名与版本号里的点号不会被当作句末切点。
    """
    length = len(text)
    end = index
    while end < length:
        char = text[end]
        if char in _PRIMARY_CUT_CHARS:
            end += 1
            continue
        if char == ".":
            probe = end
            while probe < length and text[probe] == ".":
                probe += 1
            if probe >= length or text[probe].isspace():
                end = probe
                continue
        break
    while end < length and text[end] in _TRAILING_PAUSE_CHARS:
        end += 1
    return end


def _tier_cut_positions(text: str, tier: int) -> list[int]:
    """返回指定优先级下的候选切点（值为切点右侧在原字符串中的索引）。

    行内代码与链接/图片先做掩码，切点不会落在行内原子内部；括号深度大于 0 的
    位置同样跳过，保证句中括号与动作括号不被拦腰切断。
    """
    masked, atoms = _mask_inline_atoms(text)
    mapping = _masked_position_map(masked, atoms)
    positions: list[int] = []
    depth = 0
    quote_stack: list[str] = []
    index = 0
    length = len(masked)
    while index < length:
        char = masked[index]
        if quote_stack:
            if char == quote_stack[-1]:
                quote_stack.pop()
            elif char in _QUOTE_PAIRS and _has_matching_quote_close(masked, index):
                quote_stack.append(_QUOTE_PAIRS[char])
            index += 1
            continue
        if char in _QUOTE_PAIRS and _has_matching_quote_close(masked, index):
            quote_stack.append(_QUOTE_PAIRS[char])
            index += 1
            continue
        if char in "（(【[":
            depth += 1
            index += 1
            continue
        if char in "）)】]":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth > 0:
            index += 1
            continue

        end = index
        if tier == 1:
            if char == "\n":
                while end < length and masked[end] == "\n":
                    end += 1
            elif char in _PRIMARY_CUT_CHARS or char == ".":
                end = _terminator_cluster_end(masked, index)
        elif tier == 2:
            if char in _SECONDARY_CUT_CHARS:
                while end < length and masked[end] in _SECONDARY_CUT_CHARS:
                    end += 1
        elif char in _TERTIARY_CUT_CHARS:
            while end < length and masked[end] in _TERTIARY_CUT_CHARS:
                end += 1

        if end > index:
            positions.append(mapping[end - 1] + 1)
            index = end
            continue
        index += 1
    return positions


def _pick_priority_cut(
    text: str,
    target: int,
    max_chars: int = 0,
    min_piece: int = 0,
    min_position: int = 0,
    no_split_around: list[str] | None = None,
) -> int | None:
    """按标点优先级挑选切点索引；没有可用候选时返回 None。

    规则（对应计划 §5.1）：

    1. 一级候选里取离目标长度最近的切点；一级没有「均衡」候选时降级到二级，
       再降级到三级。距离相同时取更靠前的候选，避免把尾巴切得过短。
    2. 会越过 max_chars 的候选直接排除；三级仍无解时返回 None，交给调用方沿用
       原有边界，保证不倒退。
    3. 切点两侧任一侧短于 min_piece 的候选不算「均衡」，降级到下一级标点；
       所有级别都没有均衡候选时，退而使用第一个够长的普通候选。
    4. 会造成非末尾片段不超过 2 字符的碎段候选降为最低优先级，只有全部级别都
       拿不出合法候选时才使用。
    5. min_position 为切点下界：调用方需要给尾部预留装得进剩余段数预算的
       空间时传入，早于该位置的候选一律不选。
    6. no_split_around 非空时，切点之后紧跟保护词（允许中间有空白，全半角
       等价）的候选一律不选；保护词在所有标点优先级之上（§5.4）。
    """
    total = len(text)
    limit = max_chars if max_chars > 0 else total
    if total <= 1:
        return None

    guarded_keys, max_key_len = _normalize_cut_words(no_split_around)

    def _allowed(position: int) -> bool:
        return not _no_split_guard(text, position, guarded_keys, max_key_len)

    balanced_floor = max(_MIN_FRAGMENT_CHARS, int(min_piece))
    relaxed: int | None = None
    short_fallback: int | None = None
    for tier in (1, 2, 3):
        usable = [
            position
            for position in _tier_cut_positions(text, tier)
            if 0 < position < total and position <= limit and position >= min_position
        ]
        if guarded_keys:
            usable = [position for position in usable if _allowed(position)]
        if not usable:
            continue
        balanced = [
            position
            for position in usable
            if position >= balanced_floor and total - position >= balanced_floor
        ]
        if balanced:
            return min(balanced, key=lambda position: (abs(position - target), position))
        normal = [position for position in usable if position > _MIN_FRAGMENT_CHARS]
        if not normal:
            if short_fallback is None:
                short_fallback = min(usable)
            continue
        if relaxed is None:
            relaxed = min(
                normal, key=lambda position: (abs(position - target), position)
            )
    if relaxed is not None:
        return relaxed
    return short_fallback


def _is_flushable_sentence_part(text: str) -> bool:
    """纯标点/颜文字不作为独立句段：开头的「！！！」应跟随后面的正文。"""
    return bool(text.strip()) and not is_kaomoji_or_pure_punct(text)


def _sentence_split_line(
    line: str,
    pause_min_chars: int = _PAUSE_BREAK_MIN_CHARS,
) -> list[str]:
    """Split ordinary text at sentence-ending punctuation clusters.

    原文空白与换行原样保留在片段边缘（由打包阶段决定是否合并），避免
    ASCII 句子粘连（「Hello. World.」不得变成「Hello.World.」）与段落
    换行丢失。引号内部不产生切点，长引语保持完整。
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote_stack: list[str] = []
    index = 0
    length = len(line)
    while index < length:
        char = line[index]

        if quote_stack:
            current.append(char)
            if char == quote_stack[-1]:
                quote_stack.pop()
            elif char in _QUOTE_PAIRS and _has_matching_quote_close(line, index):
                quote_stack.append(_QUOTE_PAIRS[char])
            index += 1
            continue

        if char in _QUOTE_PAIRS and _has_matching_quote_close(line, index):
            quote_stack.append(_QUOTE_PAIRS[char])
            current.append(char)
            index += 1
            continue

        if char in "\r\n":
            run = ""
            while index < length and line[index] in "\r\n":
                run += line[index]
                index += 1
            if _is_flushable_sentence_part("".join(current)):
                current.append(run)
                parts.append("".join(current))
                current = []
            elif parts:
                # 句末标点已把 current 清空：换行附着在前一段末尾，
                # 保证跨段合并时段落分隔仍在。
                parts[-1] += "".join(current) + run
                current = []
            else:
                current.append(run)
            continue

        if char in "（(【[":
            depth += 1
            current.append(char)
            index += 1
            continue
        if char in "）)】]":
            depth = max(0, depth - 1)
            current.append(char)
            index += 1
            continue
        if depth > 0:
            current.append(char)
            index += 1
            continue

        if char == ".":
            dots = ""
            while index < length and line[index] == ".":
                dots += "."
                index += 1
            current.append(dots)
            if index >= length or line[index].isspace():
                if _is_flushable_sentence_part("".join(current)):
                    parts.append("".join(current))
                    current = []
            continue

        if char == "…":
            dots = ""
            while index < length and line[index] == "…":
                dots += "…"
                index += 1
            # 省略号后紧跟的终止标点与停顿符是一个整体（「…！」「……？」），
            # 不得把标点留在下一段开头。
            while index < length and line[index] in "。！？!?~,，":
                dots += line[index]
                index += 1
            # 省略号是完整停顿，停顿段有实质内容时独立成段；句首省略号
            # （「……好，……」「…………我确认…」）是下一句的起手装饰，语气
            # 短停顿（「嗯…」）不是句边界，二者都留在原句中。
            has_content = bool("".join(current).strip())
            candidate = "".join(current) + dots
            if (
                has_content
                and len(candidate.strip()) >= pause_min_chars
                and not is_kaomoji_or_pure_punct(candidate)
            ):
                parts.append(candidate)
                current = []
            else:
                current.append(dots)
            continue

        if char in "。！？!?~":
            current.append(char)
            index += 1
            # 2026-09-13 用户裁定：句末标点后的省略号不跟随前段，归下一段
            # 开头（「红眼睛也是。……」→「红眼睛也是。」+「……是老师画
            # 的吗。」），避免「。」「……」两个符号连排在段尾；ASCII 点
            # 保持吸收（英文省略场景维持现状）。
            while index < length and line[index] in "。！？!?~,，":
                current.append(line[index])
                index += 1
            if _is_flushable_sentence_part("".join(current)):
                parts.append("".join(current))
                current = []
            continue

        current.append(char)
        index += 1

    if current and "".join(current).strip():
        parts.append("".join(current))
    return parts


def _mask_inline_atoms(text: str) -> tuple[str, list[str]]:
    """保护行内代码与链接/图片，返回 (masked_text, atoms)。"""
    atoms: list[str] = []

    def _repl(match: re.Match[str]) -> str:
        atoms.append(match.group(0))
        return f"\x00A{len(atoms) - 1}\x00"

    masked = _INLINE_CODE_RE.sub(_repl, text)
    masked = _LINK_IMAGE_RE.sub(_repl, masked)
    return masked, atoms


def _restore_inline_atoms(text: str, atoms: list[str]) -> str:
    """把行内原子占位符还原为原始内容。"""
    for index in range(len(atoms) - 1, -1, -1):
        text = text.replace(f"\x00A{index}\x00", atoms[index])
    return text


def _split_plain_text(
    text: str,
    pause_min_chars: int = _PAUSE_BREAK_MIN_CHARS,
) -> list[str]:
    """Split ordinary text into atomic action/sentence parts."""
    masked_text, inline_atoms = _mask_inline_atoms(text)
    bracket_parts = split_segments_at_bracket_boundaries(
        [masked_text],
        max_segments=0,
    )
    expanded: list[str] = []
    for part in bracket_parts:
        # 句中插入语（括号前没有句末标点）回并前文，保持原句完整；
        # 句首/句末后的括号仍按固定优先级独立成段。
        part_head = part.lstrip()[:1]
        if (
            expanded
            and part_head
            and part_head in "（(【["
            and expanded[-1].rstrip()
            and expanded[-1].rstrip()[-1] not in _SENTENCE_BOUNDARY_CHARS
            and not is_action_only_text(expanded[-1])
        ):
            expanded[-1] += part
            continue
        if is_action_only_text(part):
            expanded.append(part)
            continue
        # 整段交给句子切分：换行由 _sentence_split_line 作为硬边界处理，
        # 不再先 splitlines() 丢掉段落分隔。
        expanded.extend(_sentence_split_line(part, pause_min_chars))

    merged: list[str] = []
    for part in expanded:
        # 纯标点/颜文字回并前段；≤2 字符碎段（如省略号停顿切走后剩余的
        # 「空。」）同样回并——但动作段原子性优先：碎段绝不回并进动作段
        # （如「（动作）」后的短应答「在。」「能。」保持独立）。
        if merged and (
            is_kaomoji_or_pure_punct(part)
            or (
                len(part.strip()) <= _MIN_FRAGMENT_CHARS
                and not is_action_only_text(merged[-1])
            )
        ):
            merged[-1] += part
        else:
            merged.append(part)
    result = [part for part in merged if part.strip()]
    return [_restore_inline_atoms(part, inline_atoms) for part in result]


def split_text_at_brackets(text: str) -> list[str]:
    """把单段文本按括号边界拆成片段。已修复中文括号前置字符导致的失效问题。

    位于句边界的括号（句首动作、句末动作）独立成段；
    句中括号回并前文，保持原句连续。
    """
    if not text:
        return []
        
    parts: list[str] = []
    buffer: list[str] = []
    quote_stack: list[str] = []
    index = 0
    
    while index < len(text):
        char = text[index]
        if quote_stack:
            buffer.append(char)
            if char == quote_stack[-1]:
                quote_stack.pop()
            elif char in _QUOTE_PAIRS and _has_matching_quote_close(text, index):
                quote_stack.append(_QUOTE_PAIRS[char])
            index += 1
            continue
        if char in _QUOTE_PAIRS and _has_matching_quote_close(text, index):
            quote_stack.append(_QUOTE_PAIRS[char])
            buffer.append(char)
            index += 1
            continue

        matched_pair: tuple[str, str] | None = None
        
        for open_bracket, close_bracket in BRACKET_PAIRS:
            if char == open_bracket:
                # 修复中文失效：仅在 ASCII 字母数字、点、下划线后，才判定为程序函数调用并不予切分
                if index > 0 and (is_ascii_alnum(text[index - 1]) or text[index - 1] in "._"):
                    break
                matched_pair = (open_bracket, close_bracket)
                break
                
        if matched_pair is None:
            buffer.append(char)
            index += 1
            continue
            
        open_bracket, close_bracket = matched_pair
        depth = 1
        scan_index = index + 1
        
        while scan_index < len(text) and depth > 0:
            if text[scan_index] == open_bracket:
                depth += 1
            elif text[scan_index] == close_bracket:
                depth -= 1
            scan_index += 1
            
        if depth != 0:
            buffer.append(text[index:])
            index = len(text)
            break

        # 2026-09-13 用户裁定：括号动作一律优先独立成段（固定优先级，
        # 不区分句中/句首、不做长度区分），覆盖 v1.4.0 的句中括号回并规则。
        # 括号独立后，紧随的终止标点簇与停顿符跟随括号段，
        # 避免后一段以前导标点开头；但省略号例外——「（声音很低）……」
        # 的省略号归下一段开头（「……原来被靠着睡」），两个符号不连排。
        if buffer:
            parts.append("".join(buffer))
            buffer = []
        segment_end = scan_index
        if (
            segment_end < len(text)
            and text[segment_end] != "…"
        ):
            segment_end = max(segment_end, _terminator_cluster_end(text, segment_end))
        parts.append(text[index:segment_end])
        index = segment_end

    if buffer:
        parts.append("".join(buffer))

    return parts


def split_segments_at_bracket_boundaries(
    segments: list[str],
    *,
    max_segments: int | str,
) -> list[str]:
    """把每段内的括号包裹内容拆成独立的消息段。采用强类型防御机制。"""
    max_segs = _as_nonneg_int(max_segments)
        
    if not segments:
        return list(segments)
        
    result: list[str] = []
    for segment in segments:
        masked, atoms = _mask_inline_atoms(segment)
        for part in split_text_at_brackets(masked):
            stripped_part = part.strip()
            if stripped_part and not re.match(r"^[_\-\*\s]{3,}$", stripped_part):
                result.append(_restore_inline_atoms(stripped_part, atoms))
                
    if not result:
        return result
        
    if max_segs > 0 and len(result) > max_segs:
        head = result[: max_segs - 1]
        tail = " ".join(result[max_segs - 1 :])
        result = head + [tail]
        
    return result


def _masked_position_map(masked_text: str, atoms: list[str]) -> list[int]:
    """Map masked inline-atom placeholders back to original character indexes."""
    mapping: list[int] = []
    original_index = 0
    atom_index = 0
    index = 0
    while index < len(masked_text):
        if masked_text.startswith("\x00A", index):
            end = masked_text.find("\x00", index + 2)
            if end == -1:
                end = len(masked_text) - 1
            atom_len = len(atoms[atom_index]) if atom_index < len(atoms) else 1
            while index <= end:
                mapping.append(original_index)
                index += 1
            original_index += atom_len
            atom_index += 1
        else:
            mapping.append(original_index)
            index += 1
            original_index += 1
    return mapping


def _top_level_bracket_spans(text: str) -> list[tuple[int, int]]:
    """Return outermost balanced bracket spans, ignoring inline atoms."""
    masked_text, atoms = _mask_inline_atoms(text)
    mapping = _masked_position_map(masked_text, atoms)
    masked_spans: list[tuple[int, int]] = []
    quote_stack: list[str] = []
    index = 0
    while index < len(masked_text):
        char = masked_text[index]
        if quote_stack:
            if char == quote_stack[-1]:
                quote_stack.pop()
            elif char in _QUOTE_PAIRS and _has_matching_quote_close(
                masked_text, index
            ):
                quote_stack.append(_QUOTE_PAIRS[char])
            index += 1
            continue
        if char in _QUOTE_PAIRS and _has_matching_quote_close(masked_text, index):
            quote_stack.append(_QUOTE_PAIRS[char])
            index += 1
            continue

        matched_pair: tuple[str, str] | None = None
        for open_bracket, close_bracket in BRACKET_PAIRS:
            if char == open_bracket:
                if index > 0 and (
                    is_ascii_alnum(masked_text[index - 1])
                    or masked_text[index - 1] in "._"
                ):
                    break
                matched_pair = (open_bracket, close_bracket)
                break
        if matched_pair is None:
            index += 1
            continue
        open_bracket, close_bracket = matched_pair
        depth = 1
        scan_index = index + 1
        while scan_index < len(masked_text) and depth > 0:
            if masked_text[scan_index] == open_bracket:
                depth += 1
            elif masked_text[scan_index] == close_bracket:
                depth -= 1
            scan_index += 1
        if depth == 0:
            masked_spans.append((index, scan_index))
            index = scan_index
        else:
            index += 1

    spans: list[tuple[int, int]] = []
    for start, end in masked_spans:
        orig_start = mapping[start]
        orig_end = mapping[end - 1] + 1
        if spans and orig_start < spans[-1][1]:
            continue
        spans.append((orig_start, orig_end))
    return spans
