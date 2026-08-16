"""智能分段纯逻辑工具。"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import math
import random
import re
from typing import Any

_LOG = logging.getLogger(__name__)

# 正则表达式定义
THINKING_TAG_RE = re.compile(
    r"<thinking>(?:[\s\S]*?)(?:</thinking>|$)",
    flags=re.IGNORECASE,
)
THINKING_BOUNDARY_RE = re.compile(r"</?thinking>", flags=re.IGNORECASE)

# 协议与工具调用伪影清洗正则（已移除对通用 ``` 代码块的粗暴拦截，仅清除特定工具调用和系统标签）
PROTOCOL_ARTIFACTS_RE = re.compile(
    r"("
    r"<tool_call>[\s\S]*?(?:</tool_call>|$)"
    r"|<function_call>[\s\S]*?(?:</function_call>|$)"
    r"|\[Tooltip:[^\]]*(?:\]|$)"
    r"|\[System:[^\]]*(?:\]|$)"
    r")",
    flags=re.IGNORECASE,
)

BRACKET_PAIRS: tuple[tuple[str, str], ...] = (
    ("（", "）"),
    ("(", ")"),
    ("【", "】"),
    ("[", "]"),
)

_HARD_SENTENCE_END_CHARS = frozenset("！？!?")

_LEFT_BOUNDARY_CHARS = frozenset("（([【“‘「『《")
_SEGMENT_LEADING_PUNCT_CHARS = frozenset("…·~—–―〜？！。.")
_LEADING_PUNCTUATION_RE = re.compile(
    r"^(?:…+|\.{2,}|·+|~+|—+|–+|―+|〜+|？+|！+|。{2,})+$"
)

STYLE_GUIDES = {
    "natural": "像和朋友微信聊天一样自然地分条发送。有的消息短有的长，节奏随意。",
    "conservative": "偏沉稳的发消息风格，一条消息说比较完整的内容，不会频繁发短消息。",
    "active": "活泼的发消息风格，喜欢发短消息连击，反应词和正文分开发。",
}


def is_ascii_alnum(char: str) -> bool:
    """仅判定字符是否为 ASCII 范围内的字母或数字，避免中文字符干扰。"""
    return char.isascii() and char.isalnum()


# 颜文字与纯标点识别器
def is_kaomoji_or_pure_punct(text: str) -> bool:
    """判定文本是否为纯标点、连续标点或短颜文字，用于防止其孤立成段。"""
    if not text:
        return False
    text = text.strip()
    if not text:
        return False
        
    # 1. 含有中文汉字则绝不判定为纯颜文字，防止语义短句被强行向前合并
    if re.search(r"[\u4e00-\u9fa5]", text):
        return False
        
    # 2. 纯标点检测
    puncts = set("。！？!?~.……,，、;；:：'\"()（）【】[]{}<>《》 ")
    if all(c in puncts for c in text):
        return True
        
    # 3. 常见颜文字特征检测 (排除了中文后，再检测连续符号)
    if len(text) <= 10 and re.search(r"[><✨☆ㅠ^°¯\*_\-\|/\\]{2,}", text):
        return True
        
    # 4. 经典字母颜文字白名单
    if text.lower() in ["qwq", "awa", "qaq", "ovo", "uwu", "orz", "t_t", "q_q", "x_x", "tv_t"]:
        return True
        
    return False


def is_leading_punctuation(text: str) -> bool:
    """判断文本是否为应归属后续内容段的前导标点。"""
    stripped = str(text or "").strip()
    return bool(_LEADING_PUNCTUATION_RE.match(stripped))


# 基础清洗与检测工具
def strip_thinking_content(text: str) -> str:
    """移除 thinking 标签、Tool Call 伪影，只保留最终可见正文（保留通用代码块）。"""
    if not text:
        return ""
    cleaned_text = THINKING_TAG_RE.sub("", str(text))
    cleaned_text = THINKING_BOUNDARY_RE.sub("", cleaned_text)
    cleaned_text = PROTOCOL_ARTIFACTS_RE.sub("", cleaned_text)
    return cleaned_text.strip()


def is_non_natural_language(text: str) -> bool:
    """检测文本是否为完整的 JSON、纯代码等非自然语言。"""
    if not text:
        return False
    stripped = text.strip()
    
    # 1. 优先全局守卫：汉字占比超 10% 绝不判定为非自然语言，防止结构化文档退化为整条发送
    chinese_char_count = len(re.findall(r"[\u4e00-\u9fa5]", stripped))
    if len(stripped) > 0 and (chinese_char_count / len(stripped)) >= 0.10:
        return False
        
    # 2. 结构化 JSON 解析校验
    if (stripped.startswith("{") and stripped.endswith("}")) or \
       (stripped.startswith("[") and stripped.endswith("]")):
        try:
            json.loads(stripped)
            return True
        except Exception:
            pass
            
    # 3. 纯代码块特征保护：只有整段以围栏开头的文本才整体放行。
    if stripped.startswith("```"):
        return True
        
    # 4. 强特征配置字段
    if '"mcpServers"' in stripped or '"active":' in stripped or '"command":' in stripped:
        return True
        
    # 5. 特殊字符密度综合判定（已移除多余的二次中文校验）
    special_chars = set('{}[]":;,=<>')
    special_count = sum(1 for char in stripped if char in special_chars)
    if len(stripped) > 20 and (special_count / len(stripped)) > 0.15:
        return True
        
    return False


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


def is_markdown_heavy(text: str) -> bool:
    """
    检测文本是否包含大量 Markdown 格式特征。
    其判定结果供调用方决策是否放弃句末标点切分，改用双换行段落拆分，以保护内部格式。
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
        
    md_patterns = [
        r"^#{1,6}\s", r"^[\*\-\+]\s", r"^\d+\.\s", r"^>\s",
        r"^`{3}", r"^\|.*\|", r"^[-*_]{3,}$",
        r"^\s*[{\[]", r'^\s*"[^"]+"\s*:',
    ]
    md_line_count = 0
    for line in lines:
        stripped = line.strip()
        for pattern in md_patterns:
            if re.match(pattern, stripped):
                md_line_count += 1
                break
                
    if len(lines) > 0 and (md_line_count / len(lines)) > 0.20:
        return True
    if md_line_count >= 2:
        return True
        
    return False


# Markdown block detection and protection
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


def _is_markdown_or_json_block_start(line: str) -> bool:
    stripped = line.strip()
    return (
        _is_markdown_block_start(line)
        or stripped.startswith("{")
        or stripped.startswith("[")
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


def compute_target_segment_length(
    text: str,
    max_segments: int | str = 0,
    max_chars_per_segment: int | str = 0,
) -> int:
    """Compute the code-layer target length for ordinary chat segments."""
    try:
        max_segs = int(max_segments) if max_segments is not None else 0
    except (ValueError, TypeError):
        max_segs = 0
    try:
        max_chars = (
            int(max_chars_per_segment)
            if max_chars_per_segment is not None
            else 0
        )
    except (ValueError, TypeError):
        max_chars = 0

    total = len(str(text or ""))
    target = max(90 + round(total * 0.06), 100)
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


def _merge_to_max_segments(
    segments: list[str],
    max_segments: int,
    max_chars: int,
) -> list[str]:
    """Merge adjacent smallest groups until the hard segment count is satisfied."""
    result = list(segments)
    while len(result) > max_segments:
        best_index: int | None = None
        best_length: int | None = None
        for index in range(len(result) - 1):
            if is_action_only_text(result[index]) or is_action_only_text(
                result[index + 1]
            ):
                continue
            merged = _join_adjacent(
                result[index],
                result[index + 1],
                max_chars if max_chars > 0 else 0,
            )
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


def pack_segments_to_target(
    segments: list[str],
    target: int,
    max_segments: int | str = 0,
    max_chars_per_segment: int | str = 0,
) -> list[str]:
    """Pack atomic segments by target length without dumping a long tail."""
    try:
        max_segs = int(max_segments) if max_segments is not None else 0
    except (ValueError, TypeError):
        max_segs = 0
    try:
        max_chars = (
            int(max_chars_per_segment)
            if max_chars_per_segment is not None
            else 0
        )
    except (ValueError, TypeError):
        max_chars = 0

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
        packed = _merge_to_max_segments(packed, max_segs, max_chars or target)
        if len(packed) > max_segs:
            _LOG.warning(
                "文本长度超过 max_segments * max_chars_per_segment，"
                "无法同时满足硬段数与硬单条长度；已优先避免超长尾段"
            )
    return packed


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


def _clean_inline_markdown(text: str) -> str:
    """Strip inline Markdown markers while keeping the visible content."""
    cleaned = text
    cleaned = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1 (\2)", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", cleaned)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", cleaned)
    cleaned = re.sub(r"~~(.+?)~~", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    return cleaned


def clean_light_markdown(text: str) -> str:
    """Clean only light inline Markdown and preserve protected structures."""
    if not text:
        return text
    if is_non_natural_language(text) or is_markdown_document(text):
        return text
    if has_protected_block(text):
        return text
    return _clean_inline_markdown(text)


def _strip_table_markers(chunk: str) -> str:
    rows: list[str] = []
    for line in chunk.splitlines():
        stripped = line.strip()
        if re.match(r"^\|[\s:\-|]+\|\s*$", stripped):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _strip_list_markers(chunk: str) -> str:
    cleaned_lines: list[str] = []
    for line in chunk.splitlines():
        cleaned = re.sub(
            r"^(\s*(?:[-*+]|\d+\.)\s+)\[[ xX]\]\s*",
            r"\1",
            line,
        )
        cleaned = re.sub(r"^[-*+]\s+", "", cleaned)
        cleaned = re.sub(r"^\d+\.\s+", "", cleaned)
        cleaned_lines.append(cleaned)
    return "\n".join(cleaned_lines)


def _clean_heavy_text_regex(text: str) -> str:
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if _MD_HR_RE.match(stripped):
            continue
        cleaned = re.sub(r"^#{1,6}\s+", "", line)
        cleaned = re.sub(r"^>\s?", "", cleaned)
        cleaned = re.sub(r"^[-*+]\s+", "", cleaned)
        cleaned = re.sub(r"^\d+\.\s+", "", cleaned)
        cleaned = _clean_inline_markdown(cleaned)
        cleaned_lines.append(cleaned)
    return "\n".join(cleaned_lines).strip()


def _clean_markdown_region_heavy(text: str) -> str:
    """Clean a merged Markdown region while preserving code/table/list content."""
    cleaned_lines: list[str] = []
    in_code = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            cleaned_lines.append(line)
            continue
        if _MD_HR_RE.match(stripped):
            continue
        if _MD_TABLE_LINE_RE.match(stripped):
            cleaned_lines.append(_strip_table_markers(line))
            continue
        if _MD_LIST_LINE_RE.match(stripped):
            cleaned_lines.append(_strip_list_markers(line))
            continue
        cleaned = re.sub(r"^#{1,6}\s+", "", line)
        cleaned = re.sub(r"^>\s?", "", cleaned)
        cleaned = re.sub(r"^[-*+]\s+", "", cleaned)
        cleaned = re.sub(r"^\d+\.\s+", "", cleaned)
        cleaned_lines.append(_clean_inline_markdown(cleaned))
    return "\n".join(cleaned_lines).strip()


def clean_heavy_markdown(text: str) -> str:
    """Clean all Markdown while preserving JSON and code/table/list content."""
    if not text:
        return text
    chunks = split_protected_blocks(text)
    if not chunks:
        return text

    cleaned_parts: list[str] = []
    for kind, chunk in chunks:
        if kind == "json":
            cleaned_parts.append(chunk)
        elif kind == "markdown":
            cleaned_parts.append(_clean_markdown_region_heavy(chunk))
        else:
            cleaned_parts.append(_clean_heavy_text_regex(chunk))
    return "\n".join(part for part in cleaned_parts if part is not None).strip()


def clean_markdown_by_mode(text: str, mode: str) -> str:
    """Apply the configured Markdown cleaning mode."""
    normalized_mode = str(mode or "light").strip().lower()
    if normalized_mode == "heavy":
        return clean_heavy_markdown(text)
    if normalized_mode == "light":
        return clean_light_markdown(text)
    return text


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


def normalize_segments(segments: Any, max_segments: int | str = 0) -> list[str]:
    """标准化并自动扁平化段落。使用空格拼接防止文本粘连。"""
    try:
        max_segs = int(max_segments) if max_segments is not None else 0
    except (ValueError, TypeError):
        max_segs = 0
        
    if not isinstance(segments, list):
        if isinstance(segments, str):
            return [segments.strip()] if segments.strip() else []
        return []
        
    result: list[str] = []
    
    def flatten(item: Any):
        if isinstance(item, list):
            for sub_item in item:
                flatten(sub_item)
        elif item is not None:
            item_str = str(item).strip()
            if item_str:
                result.append(item_str)
                
    flatten(segments)
    
    if max_segs > 0 and len(result) > max_segs:
        head = result[: max_segs - 1]
        tail = " ".join(result[max_segs - 1 :])
        result = head + [tail]
        
    return result


def is_action_only_text(text: str) -> bool:
    """判断文本是否整体被一对括号包裹。"""
    stripped = str(text or "").strip()
    if len(stripped) < 2:
        return False
        
    for open_bracket, close_bracket in BRACKET_PAIRS:
        if not stripped.startswith(open_bracket) or not stripped.endswith(close_bracket):
            continue
            
        depth = 0
        for index, char in enumerate(stripped):
            if char == open_bracket:
                depth += 1
            elif char == close_bracket:
                depth -= 1
                
            if depth == 0:
                return index == len(stripped) - 1
                
    return False


def split_text_at_brackets(text: str) -> list[str]:
    """把单段文本按括号边界拆成片段。已修复中文括号前置字符导致的失效问题。"""
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

        if buffer:
            parts.append("".join(buffer))
            buffer = []
            
        parts.append(text[index:scan_index])
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
    try:
        max_segs = int(max_segments) if max_segments is not None else 0
    except (ValueError, TypeError):
        max_segs = 0
        
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


def normalize_response_text_for_key(text: str) -> str:
    """归一化用于查找预分段缓存的文本。"""
    cleaned = strip_thinking_content(str(text or ""))
    if not cleaned:
        return ""
    return " ".join(cleaned.split())


def normalize_content_key(text: str) -> str:
    """生成忽略空白、大小写与标点的内容签名，用于缓存容错匹配。"""
    cleaned = strip_thinking_content(str(text or ""))
    return "".join(char.lower() for char in cleaned if char.isalnum())


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
    try:
        max_segs = int(max_segments) if max_segments is not None else 0
    except (ValueError, TypeError):
        max_segs = 0

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


def _split_overlong_segment(segment: str, max_chars: int) -> list[str]:
    """将单条超长消息按标点或空白二次拆分，保留原始标点。"""
    sub_segments = local_fallback_split(segment, 0)
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


def stabilize_segment_lengths(
    segments: list[str],
    *,
    max_segments: int | str = 0,
    max_chars_per_segment: int | str = 0,
) -> list[str]:
    """拆分超长分段并重新应用最大分段数限制，保留模型已给出的边界。"""
    try:
        max_segs = int(max_segments) if max_segments is not None else 0
    except (ValueError, TypeError):
        max_segs = 0
    try:
        max_chars = (
            int(max_chars_per_segment)
            if max_chars_per_segment is not None
            else 0
        )
    except (ValueError, TypeError):
        max_chars = 0

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
        if max_chars > 0:
            split_parts: list[str] = []
            for segment in parts:
                if len(segment) > max_chars:
                    split_parts.extend(_split_overlong_segment(segment, max_chars))
                else:
                    split_parts.append(segment)
            parts = split_parts
        stabilized.extend(parts)
    stabilized = _merge_to_global_max(stabilized, max_segs, max_chars)
    return stabilized


def hash_normalized_text(text: str) -> str:
    """对归一化后的文本做稳定哈希。"""
    normalized = normalize_response_text_for_key(text)
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def mask_pipe_in_text(text: str) -> tuple[str, str]:
    """把原文中的 | 替换为无歧义 alnum 占位符，返回 (masked_text, placeholder)。"""
    placeholder = "RAWPIPE"
    while placeholder in text:
        placeholder += "0"
    return text.replace("|", placeholder), placeholder


def restore_pipe_in_segments(segments: list[str], placeholder: str) -> list[str]:
    """把分段结果中的占位符还原为原始 |。"""
    return [seg.replace(placeholder, "|") for seg in segments]


# Prompt 工程防御
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


# 核心解析与兜底逻辑
def parse_segments_from_model_output(
    raw_text: str,
    *,
    fallback_text: str,
    max_segments: int | str,
    max_chars_per_segment: int | str = 0,
) -> list[str]:
    """解析模型分段输出。强类型防御机制包装。"""
    try:
        max_segs = int(max_segments) if max_segments is not None else 0
    except (ValueError, TypeError):
        max_segs = 0
        
    try:
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
            )
        if len(reconstructed) == 1 and len(normalized) > 1:
            return local_fallback_split(
                fallback_text,
                max_segs,
                max_chars_per_segment,
            )
        normalized = expand_under_split_natural_segments(
            reconstructed,
            max_segments=0,
        )
            
    except Exception:
        fallback = strip_thinking_content(fallback_text).strip()
        return local_fallback_split(fallback, max_segs, max_chars_per_segment)
        
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

    if max_chars_per_segment:
        final_merged = stabilize_segment_lengths(
            final_merged,
            max_segments=max_segs,
            max_chars_per_segment=max_chars_per_segment,
        )

    return safe_escape_segments(final_merged)


def expand_under_split_natural_segments(
    segments: list[str],
    *,
    max_segments: int | str = 0,
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
        local_segments = local_fallback_split(segment, 0)
        if len(local_segments) > 1:
            expanded.extend(local_segments)
        else:
            expanded.append(segment)
    return normalize_segments(expanded, max_segments=0)


def calculate_send_delay(
    segment: str,
    delay_base: float | None,
    delay_per_char: float | None,
    delay_max: float | None,
) -> float:
    """根据文本长度计算分条发送间隔。"""
    base = float(delay_base) if delay_base is not None else 2.0
    per_char = float(delay_per_char) if delay_per_char is not None else 0.05
    max_d = float(delay_max) if delay_max is not None else 3.0
    seg_len = len(segment) if segment else 0
    normalized_delay = base + seg_len * per_char
    normalized_delay += random.uniform(0.0, 0.15)
    return max(0.0, min(max_d, normalized_delay))


def local_fallback_split(
    raw_text: str,
    max_segs: int,
    max_chars_per_segment: int | str = 0,
) -> list[str]:
    """终极自愈兜底：本地纯逻辑高精切分，保护 JSON/Markdown 块并平衡长度。"""
    try:
        max_segments = int(max_segs) if max_segs is not None else 0
    except (ValueError, TypeError):
        max_segments = 0
    try:
        max_chars = (
            int(max_chars_per_segment)
            if max_chars_per_segment is not None
            else 0
        )
    except (ValueError, TypeError):
        max_chars = 0

    cleaned = strip_thinking_content(raw_text)
    if not cleaned:
        return []

    # 纯 JSON/代码等非自然语言整体发送；若同时含受保护块，则按块解析而非整条直发。
    if is_non_natural_language(cleaned) and not has_protected_block(cleaned):
        return safe_escape_segments([cleaned.strip()])

    # 整篇均为 Markdown 结构时整体放行。
    if is_markdown_document(cleaned):
        return safe_escape_segments([cleaned.strip()])

    runs: list[tuple[str, list[str]]] = []
    text_parts: list[str] = []
    for kind, chunk in split_protected_blocks(cleaned):
        if kind == "text":
            text_parts.extend(_split_plain_text(chunk))
        else:
            if text_parts:
                runs.append(("text", text_parts))
                text_parts = []
            runs.append(("protected", [chunk.strip()]))
    if text_parts:
        runs.append(("text", text_parts))

    target = compute_target_segment_length(cleaned, max_segments, max_chars)
    packed: list[str] = []
    for run_kind, parts in runs:
        if run_kind == "protected":
            packed.extend(parts)
            continue
        if max_chars > 0:
            split_parts: list[str] = []
            for segment in parts:
                if (
                    len(segment) > max_chars
                    and not _is_unsplittable_segment(segment)
                ):
                    split_parts.extend(_split_overlong_segment(segment, max_chars))
                else:
                    split_parts.append(segment)
            parts = split_parts
        packed.extend(
            pack_segments_to_target(
                parts,
                target,
                max_segments=max_segments,
                max_chars_per_segment=max_chars,
            )
        )
    if max_segments > 0:
        packed = _merge_to_global_max(packed, max_segments, max_chars)
    return safe_escape_segments(packed)
