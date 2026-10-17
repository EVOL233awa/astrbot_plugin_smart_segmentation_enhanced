"""智能分段插件内部模块。"""
from __future__ import annotations

import json
import re

from .core import is_non_natural_language

def _escape_angle_brackets_outside_inline_atoms(text: str) -> str:
    """转义半角尖括号，但保留行内代码与链接/图片内的原文。"""
    atoms: list[str] = []

    def _mask(match: re.Match[str]) -> str:
        atoms.append(match.group(0))
        return f"\x00E{len(atoms) - 1}\x00"

    masked = _INLINE_CODE_RE.sub(_mask, text)
    masked = _LINK_IMAGE_RE.sub(_mask, masked)
    masked = masked.replace("<", "＜").replace(">", "＞")
    for index in range(len(atoms) - 1, -1, -1):
        masked = masked.replace(f"\x00E{index}\x00", atoms[index])
    return masked


def safe_escape_segments(segments: list[str]) -> list[str]:
    """
    高选择性安全后处理器：
    只对自然语言段落中的半角尖括号进行转义，严格绕过代码块、JSON、Markdown 语法标记，
    在保障原本特性的前提下，完美解决尖括号物理吞噬问题。
    """
    processed_segments = []
    for seg in segments:
        stripped = seg.strip()
        
        # 1. 如果是非自然语言段（代码块、纯 JSON、配置字段等），严格保持原文原样
        if (
            is_non_natural_language(seg)
            or "```" in stripped
            or is_markdown_document(seg)
            or has_protected_block(seg)
        ):
            processed_segments.append(seg)
            continue
            
        # 2. 如果是 Markdown 块引用（> 后接空白或正文），保留语法标记，仅转义后续文本内容
        if _MD_QUOTE_RE.match(stripped):
            boundary = seg.find(">")
            header = seg[:boundary + 1]
            body = seg[boundary + 1:]
            processed_segments.append(
                header + _escape_angle_brackets_outside_inline_atoms(body)
            )
            continue
            
        # 3. 针对常规自然语言（如带有颜文字的消息段），执行安全替换；
        #    行内代码与链接内部保持原样，避免 `a<b>c` 被改成 `a＜b＞c`。
        processed_segments.append(_escape_angle_brackets_outside_inline_atoms(seg))
        
    return processed_segments


_MD_HEADING_RE = re.compile(r"^#{1,6}\s")


_MD_HR_RE = re.compile(r"^[-*_]{3,}\s*$")


# 兼容有无首尾竖线的 GFM 表格行；单独 a|b 不算表格行。
_MD_TABLE_LINE_RE = re.compile(r"^\|.*\|\s*$")


# GFM 分隔行：--- | ---、| :--- | ---: | 等；至少包含一个竖线。
_MD_TABLE_DELIM_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


_MD_LIST_LINE_RE = re.compile(r"^([-*+]|\d+\.)\s")


# > 后允许空格、正文、加粗等 Markdown 语法；排除 >_< / >< 这类颜文字。
_MD_QUOTE_RE = re.compile(r"^>(?![_<])")


# Setext 一级标题下划线。只认 = 形式，--- 继续按分隔线处理，
# 避免把普通正文后的横线误判成标题。
_MD_SETEXT_RE = re.compile(r"^\s{0,3}=+\s*$")


_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


_LINK_IMAGE_RE = re.compile(r"!?\[([^\]\n]*)\]\(([^)\n]*)\)")


_FENCE_RE = re.compile(r"(`{3,}|~{3,})")


_FENCE_CLOSE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*$")


_FENCE_MIDLINE_CLOSE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


_FENCE_LANG_ID_RE = re.compile(r"^[A-Za-z0-9_+\-.#]*$")


def _is_json_like_block(text: str) -> bool:
    """Return True only when a balanced bracket block is valid JSON."""
    stripped = text.strip()
    if not (
        (stripped.startswith("{") and stripped.endswith("}"))
        or (stripped.startswith("[") and stripped.endswith("]"))
    ):
        return False
    try:
        json.loads(stripped)
        return True
    except Exception:
        return False


def _consume_json_block(lines: list[str], start: int) -> tuple[list[str] | None, int]:
    """Consume the first balanced JSON block, skipping quoted content."""
    depth = 0
    in_string = False
    escaped = False
    block: list[str] = []
    for index in range(start, len(lines)):
        line = lines[index]
        block.append(line)
        for char in line:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            else:
                if char == '"':
                    in_string = True
                elif char in "{[":
                    depth += 1
                elif char in "}]":
                    depth -= 1
        if depth == 0:
            block_text = "\n".join(block)
            if _is_json_like_block(block_text):
                return block, index + 1
            return None, start
    return None, start


def _is_closing_fence(line: str, fence_char: str = "") -> bool:
    match = _FENCE_CLOSE_RE.match(line)
    if match is None:
        return False
    return not fence_char or match.group(1)[0] == fence_char


def _fence_is_nested_container(info: str) -> bool:
    """markdown/md 围栏内部允许再出现围栏（文档示例常见写法）。"""
    normalized = info.strip().lower()
    return normalized in {"markdown", "md"} or normalized.startswith("markdown")


def _consume_code_fence(lines: list[str], start: int) -> tuple[list[str], int]:
    opening = lines[start].lstrip()
    fence_char = "`" if opening.startswith("```") else "~"
    opening_match = _FENCE_RE.match(opening)
    info = opening[opening_match.end():] if opening_match else ""
    nested = _fence_is_nested_container(info)
    depth = 1
    block = [lines[start]]
    index = start + 1
    while index < len(lines):
        line = lines[index]
        close_match = _FENCE_CLOSE_RE.match(line)
        if close_match is not None and close_match.group(1)[0] == fence_char:
            depth -= 1
            block.append(line)
            index += 1
            if depth == 0:
                return block, index
            continue
        if nested:
            stripped = line.lstrip()
            open_match = _FENCE_RE.match(stripped)
            if open_match is not None and open_match.group(1)[0] == fence_char:
                depth += 1
        block.append(line)
        index += 1
    return block, index


def _is_table_row(line: str) -> bool:
    """判断一行是否像 GFM 表格行（允许省略首尾竖线）。"""
    stripped = line.strip()
    if not stripped or "|" not in stripped:
        return False
    return (
        stripped.startswith("|")
        or stripped.endswith("|")
        or " | " in stripped
    )


def _is_table_delimiter(line: str) -> bool:
    return _MD_TABLE_DELIM_RE.match(line) is not None


def _is_table_start(lines: list[str], index: int) -> bool:
    """表头 + 分隔行才算表格起点；单行 a | b 不误判。"""
    return (
        _is_table_row(lines[index])
        and index + 1 < len(lines)
        and _is_table_delimiter(lines[index + 1])
    )


def _consume_table(lines: list[str], start: int) -> tuple[list[str], int]:
    block = [lines[start]]
    index = start + 1
    while index < len(lines) and _is_table_row(lines[index]):
        block.append(lines[index])
        index += 1
    return block, index


def _consume_list(lines: list[str], start: int) -> tuple[list[str], int]:
    """消费列表项及其缩进续行/项间空行，避免续行被当普通正文切走。"""
    block = [lines[start]]
    index = start + 1
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            lookahead = index + 1
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            if lookahead < len(lines) and (
                _MD_LIST_LINE_RE.match(lines[lookahead].strip()) is not None
                or lines[lookahead][:1].isspace()
            ):
                block.append(raw)
                index += 1
                continue
            break
        if _MD_LIST_LINE_RE.match(stripped) is not None or raw[:1].isspace():
            block.append(raw)
            index += 1
            continue
        break
    return block, index


def _consume_quote(lines: list[str], start: int) -> tuple[list[str], int]:
    block = [lines[start]]
    index = start + 1
    while index < len(lines) and _MD_QUOTE_RE.match(lines[index].strip()):
        block.append(lines[index])
        index += 1
    return block, index


def _is_setext_heading_at(lines: list[str], index: int) -> bool:
    """标题行 + === 下划线；正文后接横线不误判。"""
    if index + 1 >= len(lines):
        return False
    stripped = lines[index].strip()
    if not stripped or _is_markdown_block_start(lines[index]):
        return False
    if stripped.endswith(("。", "！", "？", ".", "!", "?")):
        return False
    return _MD_SETEXT_RE.match(lines[index + 1]) is not None


def _is_markdown_block_start(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("```")
        or stripped.startswith("~~~")
        or _MD_TABLE_LINE_RE.match(stripped) is not None
        or _MD_LIST_LINE_RE.match(stripped) is not None
        or _MD_QUOTE_RE.match(stripped) is not None
    )


def _is_markdown_structural_line(line: str) -> bool:
    stripped = line.strip()
    return (
        _MD_HEADING_RE.match(stripped) is not None
        or _MD_HR_RE.match(stripped) is not None
        or _is_markdown_block_start(line)
    )


def _is_structural_at(lines: list[str], index: int) -> bool:
    """含需要向前看一行才能识别的表格与 Setext 标题。"""
    return (
        _is_table_start(lines, index)
        or _is_setext_heading_at(lines, index)
        or _is_markdown_structural_line(lines[index])
    )


def _markdown_region_ranges(
    lines: list[str],
    merge_adjacent_markdown: bool = True,
    max_merged_chars: int = 0,
) -> list[tuple[int, int]]:
    """Find contiguous Markdown regions from first to last structural line.

    只有仅被空行分隔的 Markdown 结构才会合并为同一区域；结构之间的普通
    正文是独立文本区间，会正常参与本地切分。merge_adjacent_markdown 为
    假时每个结构单元独立成块（锚点路径用）。max_merged_chars > 0 时，
    合并后会超过该长度的相邻结构不合并，避免多个块拼成超长受保护段。
    """
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        if not _is_structural_at(lines, index):
            index += 1
            continue

        start = index
        last_end = _consume_markdown_block(lines, index)
        if merge_adjacent_markdown:
            scan = last_end
            while True:
                while scan < len(lines) and not lines[scan].strip():
                    scan += 1
                if scan >= len(lines):
                    break
                if not _is_structural_at(lines, scan):
                    break
                candidate_end = _consume_markdown_block(lines, scan)
                if (
                    max_merged_chars > 0
                    and len("\n".join(lines[start:candidate_end])) > max_merged_chars
                ):
                    break
                last_end = candidate_end
                scan = candidate_end

        ranges.append((start, last_end))
        index = last_end
    return ranges


def _consume_markdown_block(lines: list[str], start: int) -> int:
    """Consume one complete Markdown block and return the next line index."""
    stripped = lines[start].strip()
    if stripped.startswith("```") or stripped.startswith("~~~"):
        _, end = _consume_code_fence(lines, start)
        return end
    if _is_setext_heading_at(lines, start):
        return start + 2
    if _is_table_start(lines, start) or _MD_TABLE_LINE_RE.match(stripped):
        _, end = _consume_table(lines, start)
        return end
    if _MD_LIST_LINE_RE.match(stripped):
        _, end = _consume_list(lines, start)
        return end
    if stripped.startswith(">"):
        _, end = _consume_quote(lines, start)
        return end
    return start + 1


def _split_non_markdown_lines(lines: list[str]) -> list[tuple[str, str]]:
    """Split non-Markdown gaps while preserving JSON blocks."""
    chunks: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("{") or stripped.startswith("["):
            block, next_index = _consume_json_block(lines, index)
            if block is not None:
                chunks.append(("json", "\n".join(block)))
                index = next_index
                continue
            chunks.append(("text", lines[index]))
            index += 1
            continue

        start = index
        while index < len(lines):
            current = lines[index].strip()
            if current.startswith("{") or current.startswith("["):
                break
            index += 1
        chunk = "\n".join(lines[start:index])
        if chunk.strip():
            chunks.append(("text", chunk))
    return chunks


def _looks_like_fence_info(rest: str) -> bool:
    """围栏后的剩余文本是否像语言标识而非正文。"""
    stripped = rest.strip()
    if not stripped:
        return True
    return re.search(r"[\u4e00-\u9fa5。！？]", stripped) is None


def _looks_like_language_id(rest: str) -> bool:
    """行中围栏后的内容是否只是语言标识（避免把内联 ``` 当围栏）。"""
    return _FENCE_LANG_ID_RE.match(rest.strip()) is not None


def _normalize_fence_boundaries(lines: list[str]) -> list[str]:
    """把紧贴正文的围栏标记拆成独立行，便于按行识别代码块。

    处理「先说结论。```python」与「```再说细节。」这类非标准但常见的
    模型输出；普通围栏、嵌套围栏与代码块内的围栏样文本保持原行为。
    只有起始于行中的围栏才允许「围栏 + 尾部正文」作为闭合。
    """
    result: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_depth = 1
    fence_nested = False
    midline_open = False
    for line in lines:
        if in_fence:
            if midline_open:
                match = _FENCE_MIDLINE_CLOSE_RE.match(line)
                if (
                    match is not None
                    and match.group(1)[0] == fence_char
                    and len(match.group(1)) >= fence_len
                ):
                    result.append(line[: match.end(1)])
                    trailing = match.group(2)
                    if trailing.strip():
                        result.append(trailing)
                    in_fence = False
                    midline_open = False
                    continue
                result.append(line)
                continue
            match = _FENCE_CLOSE_RE.match(line)
            if (
                match is not None
                and match.group(1)[0] == fence_char
                and len(match.group(1)) >= fence_len
            ):
                if fence_nested and fence_depth > 1:
                    fence_depth -= 1
                    result.append(line)
                    continue
                result.append(line)
                in_fence = False
                continue
            if fence_nested:
                stripped = line.lstrip()
                open_match = _FENCE_RE.match(stripped)
                if open_match is not None and open_match.group(1)[0] == fence_char:
                    fence_depth += 1
            result.append(line)
            continue

        match = _FENCE_RE.search(line)
        if match is None:
            result.append(line)
            continue
        before = line[: match.start()]
        rest = line[match.end():]
        if before.strip():
            # 行中围栏只接受纯语言标识，避免把「使用 ```code``` 包裹」误判。
            if not _looks_like_language_id(rest):
                result.append(line)
                continue
            result.append(before)
            midline_open = True
        else:
            if not _looks_like_fence_info(rest):
                result.append(line)
                continue
            midline_open = False
        result.append(line[match.start():])
        in_fence = True
        fence_char = match.group(1)[0]
        fence_len = len(match.group(1))
        fence_nested = _fence_is_nested_container(rest)
        fence_depth = 1
    return result


def split_protected_blocks(
    text: str,
    merge_adjacent_markdown: bool = True,
    max_merged_chars: int = 0,
) -> list[tuple[str, str]]:
    """Split text into protected JSON/Markdown blocks and ordinary text chunks.

    merge_adjacent_markdown 控制连续 Markdown 结构是否合并为一个区域：
    默认 True；False 时每个结构单元独立成块，结构之间的普通文字成为
    独立文本区间（供锚点路径细分使用）。max_merged_chars 限制相邻结构
    合并后的最大字符数，0 表示不限制。
    """
    lines = _normalize_fence_boundaries(text.splitlines())
    chunks: list[tuple[str, str]] = []
    regions = _markdown_region_ranges(
        lines,
        merge_adjacent_markdown,
        max_merged_chars=max_merged_chars,
    )
    cursor = 0
    for start, end in regions:
        if start > cursor:
            chunks.extend(_split_non_markdown_lines(lines[cursor:start]))
        chunks.append(("markdown", "\n".join(lines[start:end])))
        cursor = end
    if cursor < len(lines):
        chunks.extend(_split_non_markdown_lines(lines[cursor:]))
    return chunks


def is_markdown_document(text: str) -> bool:
    """Return True only when every non-empty line belongs to a Markdown structure."""
    chunks = split_protected_blocks(text)
    has_structure = False
    for kind, chunk in chunks:
        if kind != "text":
            has_structure = True
            continue
        for line in chunk.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if _MD_HEADING_RE.match(stripped) or _MD_HR_RE.match(stripped):
                has_structure = True
                continue
            return False
    return has_structure


def has_protected_block(text: str) -> bool:
    """Return True when text contains JSON or a block-level Markdown structure."""
    return any(kind != "text" for kind, _ in split_protected_blocks(text))
