"""智能分段插件内部模块。"""
from __future__ import annotations

import re

from astrbot.api import logger

from .core import is_non_natural_language
from .protection import _MD_HR_RE, _MD_LIST_LINE_RE, _MD_QUOTE_RE, _is_table_delimiter, _is_table_row, has_protected_block, is_markdown_document, split_protected_blocks

_MDIT_INSTANCE = None


_MDIT_AVAILABLE = False


try:
    from markdown_it import MarkdownIt
    from mdit_plain.renderer import RendererPlain

    _MDIT_INSTANCE = MarkdownIt(renderer_cls=RendererPlain)
    _MDIT_AVAILABLE = True
except Exception:
    _MDIT_INSTANCE = None
    _MDIT_AVAILABLE = False


_HEAVY_FALLBACK_WARNED = False


# 私有区单字符：markdown-it 不解析，渲染前替换行首 `>`，渲染后还原。
_QUOTE_PLACEHOLDER = "\ue000"


def _warn_heavy_fallback_once() -> None:
    global _HEAVY_FALLBACK_WARNED
    if _HEAVY_FALLBACK_WARNED:
        return
    _HEAVY_FALLBACK_WARNED = True
    logger.warning(
        "mdit_plain/markdown-it-py 不可用，heavy Markdown 清洗降级为内容保全式正则"
    )


def _render_heavy_markdown(text: str) -> str:
    """Render pre-cleaned Markdown to plain text when the real renderer exists."""
    if _MDIT_AVAILABLE and _MDIT_INSTANCE is not None:
        # mdit 按 CommonMark 把行首 `>` 当引用标记；颜文字 `>_<`/`><` 的 `>`
        # 先用私有区字符占位，渲染后还原，否则 mdit 会吞掉这个 `>`。
        protected = re.sub(
            r"^>(?=[_<])", _QUOTE_PLACEHOLDER, text, flags=re.MULTILINE
        )
        try:
            rendered = _MDIT_INSTANCE.render(protected).strip()
        except Exception:
            _warn_heavy_fallback_once()
            rendered = protected
        return rendered.replace(_QUOTE_PLACEHOLDER, ">")
    _warn_heavy_fallback_once()
    return text


def _strip_italic_marker(match: re.Match[str]) -> str:
    """Drop one `*...*` pair unless a star touches an ASCII word character.

    星号任一侧紧贴 ASCII 字母/数字时是乘法或单词内符号（`2*3*4`、
    `foo*bar*baz`），不能当强调删除；中文等非 ASCII 字符相邻时仍按斜体清洗。
    """
    text = match.string
    before = text[match.start() - 1] if match.start() else ""
    after = text[match.end()] if match.end() < len(text) else ""
    if (before.isascii() and before.isalnum()) or (
        after.isascii() and after.isalnum()
    ):
        return match.group(0)
    return match.group(1)


def _clean_inline_markdown(text: str) -> str:
    """Strip inline Markdown markers while keeping the visible content."""
    cleaned = text
    cleaned = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1 (\2)", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", cleaned)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    # 强调标记的内侧不接空白：`5 * 3 = 15` 是乘法，不是斜体，
    # 星号必须原样保留；`*斜体*`（中文常见，无空格）仍正常清洗。
    cleaned = re.sub(
        r"(?<!\*)\*(?![\s*])(.+?)(?<![\s*])\*(?!\*)",
        _strip_italic_marker,
        cleaned,
    )
    # 删除线必须不紧贴单词/数字：`10~~20 度` 是区间，不是删除线。
    cleaned = re.sub(r"(?<!\w)~~(?!\s)(.+?)(?<!\s)~~(?!\w)", r"\1", cleaned)
    cleaned = re.sub(r"(?<!`)`(?!`)([^`]+)(?<!`)`(?!`)", r"\1", cleaned)
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
        if _is_table_delimiter(stripped) or re.match(r"^\|[\s:\-|]+\|\s*$", stripped):
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
        if _MD_QUOTE_RE.match(cleaned):
            cleaned = re.sub(r"^>\s?", "", cleaned)
        cleaned = re.sub(r"^[-*+]\s+", "", cleaned)
        cleaned = re.sub(r"^\d+\.\s+", "", cleaned)
        cleaned = _clean_inline_markdown(cleaned)
        cleaned_lines.append(cleaned)
    return "\n".join(cleaned_lines).strip()


def _clean_markdown_region_heavy(text: str) -> str:
    """Clean a merged Markdown region while preserving code/table/list content.

    闭合围栏的分段由 ``_clean_code_span`` 处理，这里只收到普通文本与
    未闭合围栏——未闭合围栏按正文清洗，不再当作代码块吞行。
    """
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if _MD_HR_RE.match(stripped):
            continue
        if _is_table_row(stripped):
            cleaned_lines.append(_strip_table_markers(line))
            continue
        if _MD_LIST_LINE_RE.match(stripped):
            cleaned_lines.append(_strip_list_markers(line))
            continue
        cleaned = re.sub(r"^#{1,6}\s+", "", line)
        if _MD_QUOTE_RE.match(cleaned):
            cleaned = re.sub(r"^>\s?", "", cleaned)
        cleaned = re.sub(r"^[-*+]\s+", "", cleaned)
        cleaned = re.sub(r"^\d+\.\s+", "", cleaned)
        cleaned_lines.append(_clean_inline_markdown(cleaned))
    return "\n".join(cleaned_lines).strip()


def _split_code_spans(text: str) -> list[str]:
    """Split text into closed code spans and ordinary Markdown spans.

    闭合围栏必须与开启围栏同字符且不短于开启围栏（CommonMark），多余的
    同字符一并吞掉；未闭合的围栏留在文本流里按正文处理。
    """
    parts: list[str] = []
    cursor = 0
    for match in re.finditer(
        r"(?:(?<!`)(?P<bt>`{3,})(?!`)[\s\S]*?(?P=bt)`*|(?<!~)(?P<tl>~{3,})(?!~)[\s\S]*?(?P=tl)~*)",
        text,
    ):
        if match.start() > cursor:
            parts.append(text[cursor : match.start()])
        parts.append(match.group(0))
        cursor = match.end()
    if cursor < len(text):
        parts.append(text[cursor:])
    return parts


def _is_opening_fence_line(line: str) -> bool:
    """整行开启围栏：行首围栏串，其后为空或信息串。

    同一行再出现同种围栏字符说明这是行内代码（`` ```code``` ``），不是围栏行。
    """
    match = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
    if match is None:
        return False
    return match.group(1)[0] not in match.group(2)


def _clean_code_span(span: str) -> str:
    lines = span.splitlines()
    if (
        len(lines) < 2
        or re.match(r"^\s*(?:`{3,}|~{3,})\s*$", lines[-1]) is None
    ):
        # 未闭合围栏（含单行行内 ```code```）：按正文原样保留，不剥围栏行。
        return span
    if _is_opening_fence_line(lines[0]):
        lines = lines[1:]
    lines = lines[:-1]
    return "\n".join(lines)


def _clean_heavy_chunk_with_mdit(text: str, *, markdown_region: bool) -> str:
    """Clean heavy Markdown while keeping code block bodies untouched by the renderer."""
    cleaned_parts: list[str] = []
    for part in _split_code_spans(text):
        if part.startswith(("```", "~~~")):
            cleaned_parts.append(_clean_code_span(part))
            continue
        if markdown_region:
            cleaned_parts.append(
                _render_heavy_markdown(_clean_markdown_region_heavy(part))
            )
        else:
            cleaned_parts.append(
                _render_heavy_markdown(_clean_heavy_text_regex(part))
            )
    return "\n".join(cleaned_parts).strip()


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
            cleaned_parts.append(
                _clean_heavy_chunk_with_mdit(chunk, markdown_region=True)
            )
        else:
            cleaned_parts.append(
                _clean_heavy_chunk_with_mdit(chunk, markdown_region=False)
            )
    return "\n".join(part for part in cleaned_parts if part is not None).strip()


def clean_markdown_by_mode(text: str, mode: str) -> str:
    """Apply the configured Markdown cleaning mode."""
    normalized_mode = str(mode or "light").strip().lower()
    if normalized_mode == "heavy":
        return clean_heavy_markdown(text)
    if normalized_mode == "light":
        return clean_light_markdown(text)
    return text
