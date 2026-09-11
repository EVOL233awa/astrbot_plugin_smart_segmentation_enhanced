"""智能分段插件内部模块。"""
from __future__ import annotations

import re

from .core import BRACKET_PAIRS, _as_nonneg_int, is_action_only_text, is_ascii_alnum, is_kaomoji_or_pure_punct
from .protection import _INLINE_CODE_RE, _LINK_IMAGE_RE

_HARD_SENTENCE_END_CHARS = frozenset("！？!?")


_SENTENCE_BOUNDARY_CHARS = frozenset("。！？!?~…")


_LEFT_BOUNDARY_CHARS = frozenset("（([【“‘「『《")


def _sentence_split_line(line: str) -> list[str]:
    """Split one ordinary text line at sentence-ending punctuation clusters."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
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
                parts.append("".join(current).strip())
                current = []
            continue

        if char == "…":
            dots = ""
            while index < length and line[index] == "…":
                dots += "…"
                index += 1
            current.append(dots)
            if index >= length or line[index].isspace():
                parts.append("".join(current).strip())
                current = []
            continue

        if char in "。！？!?~":
            current.append(char)
            index += 1
            while index < length and line[index] in "。！？!?~.……,，":
                current.append(line[index])
                index += 1
            parts.append("".join(current).strip())
            current = []
            continue

        current.append(char)
        index += 1

    if current and "".join(current).strip():
        parts.append("".join(current).strip())
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


def _split_plain_text(text: str) -> list[str]:
    """Split ordinary text into atomic action/sentence parts."""
    masked_text, inline_atoms = _mask_inline_atoms(text)
    bracket_parts = split_segments_at_bracket_boundaries(
        [masked_text],
        max_segments=0,
    )
    expanded: list[str] = []
    for part in bracket_parts:
        if is_action_only_text(part):
            expanded.append(part)
            continue
        for line in part.splitlines():
            if line.strip():
                expanded.extend(_sentence_split_line(line))

    merged: list[str] = []
    for part in expanded:
        if merged and is_kaomoji_or_pure_punct(part):
            merged[-1] += part
        else:
            merged.append(part)
    result = [part for part in merged if part.strip()]
    return [_restore_inline_atoms(part, inline_atoms) for part in result]


def _buffer_ends_at_sentence_boundary(buffer: list[str]) -> bool:
    """括号前文是否终止于句边界（决定括号段能否独立成段）。

    句中括号（如"取件码（3-2-1874）还是"、"（先扫码）先于心理流程"）
    是正文的一部分而非动作描写，回并前文避免句子被拦腰切断。
    """
    text = "".join(buffer).rstrip()
    if not text:
        return True
    return text[-1] in _SENTENCE_BOUNDARY_CHARS


def split_text_at_brackets(text: str) -> list[str]:
    """把单段文本按括号边界拆成片段。已修复中文括号前置字符导致的失效问题。

    位于句边界的括号（句首动作、句末动作）独立成段；
    句中括号回并前文，保持原句连续。
    """
    if not text:
        return []
        
    parts: list[str] = []
    buffer: list[str] = []
    index = 0
    
    while index < len(text):
        char = text[index]
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

        if _buffer_ends_at_sentence_boundary(buffer):
            if buffer:
                parts.append("".join(buffer))
                buffer = []
            parts.append(text[index:scan_index])
        else:
            buffer.append(text[index:scan_index])
        index = scan_index
        
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
    index = 0
    while index < len(masked_text):
        char = masked_text[index]
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
