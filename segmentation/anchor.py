"""智能分段插件内部模块：块级结构锚点一次调用路径（§5.6）。

含受保护块的消息不再整条降级本地：块原位替换为唯一 alnum 锚点后调用
一次 LLM，重建经锚点校验后按锚点边界展开还原。任何失败由调用方降级
本地路径（对原文），最坏结果等于现状。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .model_output import reconstruct_segments_from_model_output
from .protection import split_protected_blocks

# 锚点前缀：纯 alnum（§5.6 实测结论推荐 ZZSEG+hex，不采用括号形式）。
_ANCHOR_PREFIX = "ZZSEG"

# 锚点完整形态：ZZSEG + 4 位大写十六进制；编号空间 65536 远超单条消息
# 的块数上限，耗尽属于异常输入。
_ANCHOR_RE = re.compile(r"ZZSEG[0-9A-F]{4}")


@dataclass(slots=True)
class AnchorMasking:
    """锚点掩码结果：masked 文本、锚点与块的对应关系。"""

    masked_text: str
    anchors: list[str]
    anchor_to_block: dict[str, str]
    # 是否存在非空文本区间；只有块没有文本时不调用 LLM（§5.6 步骤 8）。
    has_text: bool


def generate_anchors(count: int, original_text: str) -> list[str]:
    """生成 count 个唯一 alnum 锚点，确保不出现在原文中（大小写不敏感）。"""
    if count <= 0:
        return []
    lower_original = original_text.casefold()
    anchors: list[str] = []
    for index in range(0x10000):
        candidate = f"{_ANCHOR_PREFIX}{index:04X}"
        if candidate.casefold() in lower_original:
            continue
        if candidate in anchors:
            continue
        anchors.append(candidate)
        if len(anchors) == count:
            return anchors
    raise ValueError(f"无法为 {count} 个受保护块生成唯一锚点")


def build_anchor_masking(text: str) -> AnchorMasking | None:
    """把原文中的受保护块原位替换为唯一锚点；没有块时返回 None。

    使用 merge_adjacent_markdown=False 细分连续 Markdown 结构之间的普通
    文字（§5.6 保护层细分块修复）；块内容原位替换后前后空白与换行保留。
    返回 None 表示该文本没有受保护块（调用方走普通流程）。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n") if "\r" in text else text
    runs = split_protected_blocks(normalized, merge_adjacent_markdown=False)
    blocks = [content for kind, content in runs if kind != "text"]
    if not blocks:
        return None
    has_text = any(kind == "text" and content.strip() for kind, content in runs)

    anchors = generate_anchors(len(blocks), normalized)
    masked = normalized
    anchor_to_block: dict[str, str] = {}
    cursor = 0
    for anchor, block in zip(anchors, blocks):
        position = masked.find(block, cursor)
        if position == -1:
            # 块内容来自同一文本的切分，理论上必然命中；防御性返回 None
            # 让调用方降级本地，绝不产出错误掩码。
            return None
        masked = masked[:position] + anchor + masked[position + len(block):]
        cursor = position + len(anchor)
        anchor_to_block[anchor] = block
    return AnchorMasking(
        masked_text=masked,
        anchors=anchors,
        anchor_to_block=anchor_to_block,
        has_text=has_text,
    )


def verify_anchor_integrity(segments: list[str], anchors: list[str]) -> bool:
    """锚点校验（§5.6 步骤 5）：每个锚点恰好出现一次且顺序一致。

    恰好一次同时保证没有被拆到多个分段、没有跨分段丢失；顺序一致保证
    没有被重排。模型输出经重建后段文本来自 masked 原文，锚点必然精确。
    """
    found: list[str] = []
    for segment in segments:
        for match in _ANCHOR_RE.finditer(segment):
            found.append(match.group(0))
    return found == anchors


def expand_segments_at_anchors(
    segments: list[str],
    anchor_to_block: dict[str, str],
    pipe_placeholder: str | None = None,
) -> list[str]:
    """按锚点边界拆分重建分段并还原块内容（§5.6 步骤 6）。

    一个分段包含多个锚点时按顺序依次拆分；锚点前后的文本为空则丢弃，
    块独立成段。pipe_placeholder 非空时只对文本段还原管道符占位——块
    段使用真实内容，其中的 | 不受占位符污染（§5.6 管道符顺序约束）。
    """
    result: list[str] = []
    for segment in segments:
        pieces: list[tuple[str, bool]] = []
        cursor = 0
        for match in _ANCHOR_RE.finditer(segment):
            anchor = match.group(0)
            block = anchor_to_block.get(anchor)
            if block is None:
                raise ValueError(f"重建分段包含未知锚点: {anchor}")
            before = segment[cursor : match.start()].strip()
            if before:
                pieces.append((before, False))
            pieces.append((block, True))
            cursor = match.end()
        tail = segment[cursor:].strip()
        if tail:
            pieces.append((tail, False))
        for piece, is_block in pieces:
            if is_block:
                # 块段是真实内容，其中的 | 不受占位符污染
                result.append(piece)
            elif pipe_placeholder and pipe_placeholder in piece:
                result.append(piece.replace(pipe_placeholder, "|"))
            else:
                result.append(piece)
    return result


def segment_from_anchored_output(
    llm_segments: list[str],
    masked_text: str,
    masking: AnchorMasking,
    pipe_placeholder: str | None = None,
) -> list[str] | None:
    """锚点路径核心：重建 → 校验 → 展开还原；任一步失败返回 None。

    调用方收到 None 时必须对原文执行本地路径（效果等于现状），不得
    把含锚点的分段交给任何缓存或发送逻辑。
    """
    reconstructed = reconstruct_segments_from_model_output(
        llm_segments,
        masked_text,
        max_segments=0,
    )
    if reconstructed is None:
        return None
    if not verify_anchor_integrity(reconstructed, masking.anchors):
        return None
    expanded = expand_segments_at_anchors(
        reconstructed,
        masking.anchor_to_block,
        pipe_placeholder,
    )
    return expanded or None
