"""智能分段插件内部模块。"""
from __future__ import annotations

import json
import re

from .core import is_non_natural_language

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
            escaped_body = body.replace("<", "＜").replace(">", "＞")
            processed_segments.append(header + escaped_body)
            continue
            
        # 3. 针对常规自然语言（如带有颜文字的消息段），执行安全替换
        escaped_seg = seg.replace("<", "＜").replace(">", "＞")
        processed_segments.append(escaped_seg)
        
    return processed_segments


_MD_HEADING_RE = re.compile(r"^#{1,6}\s")


_MD_HR_RE = re.compile(r"^[-*_]{3,}\s*$")


_MD_TABLE_LINE_RE = re.compile(r"^\|.*\|\s*$")


_MD_LIST_LINE_RE = re.compile(r"^([-*+]|\d+\.)\s")


_MD_QUOTE_RE = re.compile(r"^>(?:\s|[\u4e00-\u9fa5])")


_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


_LINK_IMAGE_RE = re.compile(r"!?\[([^\]\n]*)\]\(([^)\n]*)\)")


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


def _is_closing_fence(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("```") and stripped.strip("`").strip() == ""


def _consume_code_fence(lines: list[str], start: int) -> tuple[list[str], int]:
    block = [lines[start]]
    index = start + 1
    while index < len(lines) and not _is_closing_fence(lines[index]):
        block.append(lines[index])
        index += 1
    if index < len(lines):
        block.append(lines[index])
        index += 1
    return block, index


def _consume_table(lines: list[str], start: int) -> tuple[list[str], int]:
    block = [lines[start]]
    index = start + 1
    while index < len(lines) and _MD_TABLE_LINE_RE.match(lines[index].strip()):
        block.append(lines[index])
        index += 1
    return block, index


def _consume_list(lines: list[str], start: int) -> tuple[list[str], int]:
    block = [lines[start]]
    index = start + 1
    while index < len(lines) and _MD_LIST_LINE_RE.match(lines[index].strip()):
        block.append(lines[index])
        index += 1
    return block, index


def _consume_quote(lines: list[str], start: int) -> tuple[list[str], int]:
    block = [lines[start]]
    index = start + 1
    while index < len(lines) and _MD_QUOTE_RE.match(lines[index].strip()):
        block.append(lines[index])
        index += 1
    return block, index


def _is_markdown_block_start(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("```")
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


def _has_structure_after(lines: list[str], index: int) -> bool:
    """Return True when a later non-blank line is still Markdown structure."""
    scan = index + 1
    while scan < len(lines):
        if lines[scan].strip():
            return _is_markdown_structural_line(lines[scan])
        scan += 1
    return False


def _markdown_region_ranges(lines: list[str]) -> list[tuple[int, int]]:
    """Find contiguous Markdown regions from first to last structural line."""
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        if not _is_markdown_structural_line(lines[index]):
            index += 1
            continue

        start = index
        scan = index
        first_is_heading = (
            _MD_HEADING_RE.match(lines[start].strip()) is not None
        )
        last_end = _consume_markdown_block(lines, scan)
        scan = last_end
        first_non_blank_after_start = True
        while True:
            while scan < len(lines) and not lines[scan].strip():
                scan += 1
            if scan >= len(lines):
                break
            if _is_markdown_structural_line(lines[scan]):
                first_non_blank_after_start = False
                last_end = _consume_markdown_block(lines, scan)
                scan = last_end
                continue
            if _has_structure_after(lines, scan):
                first_non_blank_after_start = False
                scan += 1
                continue
            if first_is_heading and first_non_blank_after_start:
                scan += 1
                last_end = scan
                first_non_blank_after_start = False
                continue
            break

        ranges.append((start, last_end))
        index = last_end
    return ranges


def _consume_markdown_block(lines: list[str], start: int) -> int:
    """Consume one complete Markdown block and return the next line index."""
    stripped = lines[start].strip()
    if stripped.startswith("```"):
        _, end = _consume_code_fence(lines, start)
        return end
    if _MD_TABLE_LINE_RE.match(stripped):
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


def split_protected_blocks(text: str) -> list[tuple[str, str]]:
    """Split text into protected JSON/Markdown blocks and ordinary text chunks."""
    lines = text.splitlines()
    chunks: list[tuple[str, str]] = []
    regions = _markdown_region_ranges(lines)
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
