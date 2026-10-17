"""智能分段纯逻辑工具（公开 API 门面）。

各子模块按职责拆分；本文件只再导出跨模块使用的公开名字，以及测试直接
依赖的两个边界常量/函数。私有实现请从对应子模块导入，避免把内部细节
固化成包级公开 API。
"""
from __future__ import annotations

from .core import (
    BRACKET_PAIRS,
    PROTOCOL_ARTIFACTS_RE,
    THINKING_BOUNDARY_RE,
    THINKING_TAG_RE,
    hash_normalized_text,
    is_action_only_text,
    is_ascii_alnum,
    is_kaomoji_or_pure_punct,
    is_leading_punctuation,
    is_non_natural_language,
    normalize_content_key,
    normalize_response_text_for_key,
    normalize_segments,
    strip_thinking_content,
)

from .protection import (
    has_protected_block,
    is_markdown_document,
    safe_escape_segments,
    split_protected_blocks,
)

from .markdown_clean import (
    clean_heavy_markdown,
    clean_light_markdown,
    clean_markdown_by_mode,
)

from .anchor import (
    build_anchor_masking,
    expand_segments_at_anchors,
    generate_anchors,
    segment_from_anchored_output,
    verify_anchor_integrity,
)

from .boundary import (
    split_segments_at_bracket_boundaries,
    split_text_at_brackets,
)

from .packing import (
    apply_force_split_to_segments,
    apply_no_split_boundaries,
    compute_target_segment_length,
    merge_short_tail_segment,
    pack_segments_to_target,
)

from .local_split import (
    expand_under_split_natural_segments,
    local_fallback_split,
    stabilize_segment_lengths,
)

from .delivery import calculate_send_delay

from .model_output import (
    STYLE_GUIDES,
    align_prepared_in_outbound,
    build_segmentation_prompt,
    extract_json_array_text,
    mask_pipe_in_text,
    parse_raw_model_segments,
    parse_segments_from_model_output,
    reconstruct_segments_from_model_output,
    restore_pipe_in_segments,
    robust_json_loads,
)

__all__ = [
    "BRACKET_PAIRS",
    "PROTOCOL_ARTIFACTS_RE",
    "STYLE_GUIDES",
    "THINKING_BOUNDARY_RE",
    "THINKING_TAG_RE",
    "align_prepared_in_outbound",
    "apply_force_split_to_segments",
    "apply_no_split_boundaries",
    "build_anchor_masking",
    "build_segmentation_prompt",
    "calculate_send_delay",
    "clean_heavy_markdown",
    "clean_light_markdown",
    "clean_markdown_by_mode",
    "compute_target_segment_length",
    "expand_segments_at_anchors",
    "expand_under_split_natural_segments",
    "extract_json_array_text",
    "generate_anchors",
    "has_protected_block",
    "hash_normalized_text",
    "is_action_only_text",
    "is_ascii_alnum",
    "is_kaomoji_or_pure_punct",
    "is_leading_punctuation",
    "is_markdown_document",
    "is_non_natural_language",
    "local_fallback_split",
    "mask_pipe_in_text",
    "merge_short_tail_segment",
    "normalize_content_key",
    "normalize_response_text_for_key",
    "normalize_segments",
    "pack_segments_to_target",
    "parse_raw_model_segments",
    "parse_segments_from_model_output",
    "reconstruct_segments_from_model_output",
    "restore_pipe_in_segments",
    "robust_json_loads",
    "safe_escape_segments",
    "segment_from_anchored_output",
    "split_protected_blocks",
    "split_segments_at_bracket_boundaries",
    "split_text_at_brackets",
    "stabilize_segment_lengths",
    "strip_thinking_content",
    "verify_anchor_integrity",
]
