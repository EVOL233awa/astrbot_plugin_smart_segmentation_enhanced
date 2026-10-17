"""智能分段插件内部模块。"""
from __future__ import annotations

import re

from astrbot.api import logger

from .core import is_non_natural_language
from .protection import _MD_HR_RE, _MD_LIST_LINE_RE, _MD_TABLE_LINE_RE, has_protected_block, is_markdown_document, split_protected_blocks

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
        try:
            return _MDIT_INSTANCE.render(text).strip()
        except Exception:
            _warn_heavy_fallback_once()
    else:
        _warn_heavy_fallback_once()
    return text


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


def _split_code_spans(text: str) -> list[str]:
    """Split text into code spans and ordinary Markdown spans."""
    return re.split(r"(```[\s\S]*?(?:```|$))", text)


def _clean_code_span(span: str) -> str:
    lines = span.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def _clean_heavy_chunk_with_mdit(text: str, *, markdown_region: bool) -> str:
    """Clean heavy Markdown while keeping code block bodies untouched by the renderer."""
    cleaned_parts: list[str] = []
    for part in _split_code_spans(text):
        if part.startswith("```"):
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
