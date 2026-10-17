# tests/test_anchor.py
"""块级结构锚点一次调用路径的单元与端到端测试（计划 §5.6）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import FakeContext, FakeLLMResponse  # noqa: E402

from astrbot_plugin_smart_segmentation_enhanced.main import (  # noqa: E402
    SmartSegmentationPlugin,
)
from segmentation import (  # noqa: E402
    build_anchor_masking,
    expand_segments_at_anchors,
    generate_anchors,
    local_fallback_split,
    parse_raw_model_segments,
    segment_from_anchored_output,
    split_protected_blocks,
    verify_anchor_integrity,
)

CODE_BLOCK = "```python\nprint(1)\n```"
BLOCK_TEXT = f"说明如下：\n{CODE_BLOCK}\n后续说明。"


class TestAnchorMasking(unittest.TestCase):
    """锚点生成与掩码构建。"""

    def test_generate_anchors_unique_and_absent(self):
        anchors = generate_anchors(3, BLOCK_TEXT)
        self.assertEqual(len(anchors), 3)
        self.assertEqual(len(set(anchors)), 3)
        for anchor in anchors:
            self.assertRegex(anchor, r"ZZSEG[0-9A-F]{4}")
            self.assertNotIn(anchor.lower(), BLOCK_TEXT.lower())

    def test_generate_anchors_avoids_original_collision(self):
        text = "原文里有 ZZSEG0000 和 ZZSEG0001 这样的字样。"
        anchors = generate_anchors(2, text)
        for anchor in anchors:
            self.assertNotIn(anchor.lower(), text.lower())

    def test_build_anchor_masking_replaces_block_in_place(self):
        masking = build_anchor_masking(BLOCK_TEXT)
        self.assertIsNotNone(masking)
        self.assertTrue(masking.has_text)
        self.assertEqual(len(masking.anchors), 1)
        anchor = masking.anchors[0]
        self.assertIn(anchor, masking.masked_text)
        self.assertNotIn("print(1)", masking.masked_text)
        self.assertIn("说明如下：", masking.masked_text)
        self.assertIn("后续说明。", masking.masked_text)
        # 锚点与块一一对应，块内容完整
        self.assertEqual(masking.anchor_to_block[anchor], CODE_BLOCK)

    def test_build_anchor_masking_none_without_block(self):
        self.assertIsNone(build_anchor_masking("普通一句话，没有块。"))

    def test_build_anchor_masking_block_only_has_no_text(self):
        masking = build_anchor_masking(f"{CODE_BLOCK}\n```json\n{{\"a\": 1}}\n```")
        self.assertIsNotNone(masking)
        self.assertFalse(masking.has_text)
        self.assertEqual(len(masking.anchors), 2)

    def test_fine_grained_runs_keep_interleaved_text(self):
        """普通正文始终是独立文本区间；merge 只影响相邻结构块。"""
        text = "# 标题\n\n正文第一段。\n\n- 列表项\n\n正文第二段。"
        for merge in (True, False):
            chunks = split_protected_blocks(text, merge_adjacent_markdown=merge)
            text_chunks = [chunk for kind, chunk in chunks if kind == "text"]
            self.assertTrue(any("正文第一段。" in chunk for chunk in text_chunks))

        # 仅被空行分隔的相邻结构：默认合并，merge=False 分开。
        adjacent = "# 标题\n\n- 列表项"
        merged = split_protected_blocks(adjacent)
        fine = split_protected_blocks(adjacent, merge_adjacent_markdown=False)
        self.assertEqual(len([c for k, c in merged if k != "text"]), 1)
        self.assertEqual(len([c for k, c in fine if k != "text"]), 2)


class TestAnchorVerifyAndExpand(unittest.TestCase):
    """锚点校验与按锚点边界展开。"""

    def setUp(self):
        self.masking = build_anchor_masking(BLOCK_TEXT)
        self.anchor = self.masking.anchors[0]
        self.block = self.masking.anchor_to_block[self.anchor]

    def test_verify_passes_on_clean_output(self):
        segments = ["说明如下：", self.anchor, "后续说明。"]
        self.assertTrue(verify_anchor_integrity(segments, self.masking.anchors))

    def test_verify_fails_on_missing_anchor(self):
        segments = ["说明如下：", "后续说明。"]
        self.assertFalse(verify_anchor_integrity(segments, self.masking.anchors))

    def test_verify_fails_on_duplicated_anchor(self):
        segments = [self.anchor, "中段", self.anchor]
        self.assertFalse(verify_anchor_integrity(segments, self.masking.anchors))

    def test_verify_fails_on_reordered_anchor(self):
        anchors = generate_anchors(2, "a```b```c```d```e")
        self.assertFalse(verify_anchor_integrity([anchors[1], anchors[0]], anchors))

    def test_verify_fails_on_truncated_anchor(self):
        truncated = self.anchor[:-1]
        self.assertFalse(
            verify_anchor_integrity(["说明如下：", truncated, "后续说明。"], [self.anchor])
        )

    def test_expand_splits_around_anchor(self):
        expanded = expand_segments_at_anchors(
            [f"说明如下：{self.anchor}后续说明。"],
            self.masking.anchor_to_block,
        )
        self.assertEqual(expanded, ["说明如下：", self.block, "后续说明。"])

    def test_expand_handles_multiple_anchors_in_one_segment(self):
        text = "前\n```\na\n```\n中\n```\nb\n```\n后"
        masking = build_anchor_masking(text)
        self.assertIsNotNone(masking)
        a1, a2 = masking.anchors
        expanded = expand_segments_at_anchors(
            [f"前{a1}中{a2}后"], masking.anchor_to_block
        )
        self.assertEqual(
            expanded, ["前", "```\na\n```", "中", "```\nb\n```", "后"]
        )

    def test_expand_restores_pipe_only_in_text(self):
        """块内容里的 | 不受占位符污染；文本段占位符还原。"""
        text = "前面|文字\n```\na|b\n```\n后面"
        masking = build_anchor_masking(text)
        anchor = masking.anchors[0]
        expanded = expand_segments_at_anchors(
            [f"前面|文字{anchor}后面"],
            masking.anchor_to_block,
            pipe_placeholder="RAWPIPE",
        )
        self.assertTrue(any("a|b" in seg for seg in expanded))
        self.assertTrue(any("前面|文字" in seg for seg in expanded))
        self.assertFalse(any("RAWPIPE" in seg for seg in expanded))


class TestSegmentFromAnchoredOutput(unittest.TestCase):
    """重建 → 校验 → 展开的核心编排。"""

    def test_success_path(self):
        masking = build_anchor_masking(BLOCK_TEXT)
        anchor = masking.anchors[0]
        llm_segments = parse_raw_model_segments(
            f"说明如下： | {anchor} | 后续说明。",
            fallback_text=masking.masked_text,
        )
        expanded = segment_from_anchored_output(
            llm_segments, masking.masked_text, masking
        )
        self.assertEqual(expanded, ["说明如下：", CODE_BLOCK, "后续说明。"])

    def test_reconstruction_failure_returns_none(self):
        masking = build_anchor_masking(BLOCK_TEXT)
        # 模型输出与原文无法签名对齐（内容被改写）
        expanded = segment_from_anchored_output(
            ["完全无关的输出"], masking.masked_text, masking
        )
        self.assertIsNone(expanded)

    def test_dropped_anchor_segment_self_heals(self):
        """模型把锚点段与上文连排时，重建+展开仍能正确还原（自愈）。"""
        masking = build_anchor_masking(BLOCK_TEXT)
        llm_segments = parse_raw_model_segments(
            "说明如下： | 后续说明。",
            fallback_text=masking.masked_text,
        )
        expanded = segment_from_anchored_output(
            llm_segments, masking.masked_text, masking
        )
        self.assertEqual(expanded, ["说明如下：", CODE_BLOCK, "后续说明。"])

    def test_anchor_split_across_segments_returns_none(self):
        """锚点被切进两个分段（截断）时校验失败，返回 None。"""
        masking = build_anchor_masking(BLOCK_TEXT)
        anchor = masking.anchors[0]
        llm_segments = parse_raw_model_segments(
            f"说明如下：{anchor[:-1]} | {anchor[-1]}后续说明。",
            fallback_text=masking.masked_text,
        )
        self.assertIsNone(
            segment_from_anchored_output(llm_segments, masking.masked_text, masking)
        )


class TestAnchorPathEndToEnd(unittest.IsolatedAsyncioTestCase):
    """main._segment_text 锚点路径：mock LLM 的端到端行为。"""

    def _make_plugin(self, completion: str):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        calls: list[dict] = []

        async def fake_llm_generate(**kwargs):
            calls.append(kwargs)
            return FakeLLMResponse(completion)

        plugin.context.llm_generate = fake_llm_generate
        return plugin, calls

    def _settings(self, plugin):
        settings = plugin._get_settings()
        return settings

    async def test_end_to_end_restores_blocks_without_anchor_residue(self):
        plugin, calls = self._make_plugin("说明如下： | ZZSEG0000 | 后续说明。")
        settings = self._settings(plugin)
        segments = await plugin._segment_text(
            BLOCK_TEXT, provider_id="fake", settings=settings
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("ZZSEG0000", calls[0]["prompt"])
        self.assertNotIn("print(1)", calls[0]["prompt"])
        joined = "\n".join(segments)
        self.assertNotIn("ZZSEG", joined)
        self.assertIn(CODE_BLOCK, segments)
        self.assertEqual(segments, ["说明如下：", CODE_BLOCK, "后续说明。"])

    async def test_degrades_to_local_on_anchor_loss(self):
        plugin, calls = self._make_plugin("说明如下： | 后续说明。")
        settings = self._settings(plugin)
        segments = await plugin._segment_text(
            BLOCK_TEXT, provider_id="fake", settings=settings
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            segments,
            local_fallback_split(BLOCK_TEXT, settings.max_segments, settings.max_chars_per_segment),
        )
        self.assertFalse(any("ZZSEG" in seg for seg in segments))

    async def test_block_only_never_calls_llm(self):
        plugin, calls = self._make_plugin("不应被调用")
        settings = self._settings(plugin)
        block_only = f"{CODE_BLOCK}\n```json\n{{\"a\": 1}}\n```"
        segments = await plugin._segment_text_with_anchors(
            block_only, provider_id="fake", settings=settings
        )
        self.assertEqual(calls, [])
        self.assertFalse(any("ZZSEG" in seg for seg in segments))
        self.assertTrue(any("print(1)" in seg for seg in segments))

    async def test_plain_text_bypasses_anchor_path(self):
        """不含受保护块的文本走普通流程，prompt 中没有锚点。"""
        plugin, calls = self._make_plugin("第一句。第二句。")
        settings = self._settings(plugin)
        await plugin._segment_text(
            "第一句。第二句。", provider_id="fake", settings=settings
        )
        self.assertEqual(len(calls), 1)
        self.assertNotIn("ZZSEG", calls[0]["prompt"])
