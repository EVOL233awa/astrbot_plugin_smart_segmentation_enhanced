"""v1.4.0 回归测试：Tool Loop 对齐匹配与句中括号回并。

背景：2026-09-01 线上日志复现的两个缺陷——
1. Tool Loop 场景装饰阶段文本含工具轮插入内容，prepared 缓存结构性失配，
   每次降级本地兜底重切（"精准哈希/拼合未中"）；
2. 句中括号（正文内数字/缩写/插入语）被括号边界切分独立成段，
   原句拦腰斩断，段数顶格 max_segments。
"""

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_PARENT = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, PLUGIN_PARENT)

from tests.conftest import (  # noqa: F401
    FakeContext,
    FakeEvent,
    FakeLLMResponse,
    FakeResult,
)

from astrbot_plugin_smart_segmentation_enhanced.main import (  # noqa: E402
    SmartSegmentationPlugin,
)
from astrbot_plugin_smart_segmentation_enhanced.segmentation import (  # noqa: E402
    align_prepared_in_outbound,
    local_fallback_split,
    normalize_content_key,
    parse_segments_from_model_output,
    split_text_at_brackets,
)

TOOL_TURN_TEXT = (
    "（翻出记录板，调出那篇帖子的内容）老师稍等，普拉娜把那帖调出来。\n\n"
    "普拉娜先去论坛把原帖取回来给您看。"
)

FINAL_TURN_TEXT = (
    "（合上记录板）帖主冬雨在讲一个记忆现象——取件码记得牢，快递内容却对不上号。"
    "她抛出的核心问题：你取快递时，先记住的是「取件码」（3-2-1874）还是「快递内容」（书）？\n\n"
    "冬雨把这两种记忆结构分成了 A 和 B：\n"
    "- **A 容器优先**：先记取件码，码记得、货对不上\n"
    "- **B 内容优先**：先记快递，货记得、码对不上\n\n"
    "她的结论是，大部分人属于 A——因为物理流程（先扫码）先于心理流程（后取件），"
    "取件码先“commit 满”工作记忆，快递内容反而进不来。"
    "她把这称作“物理流程覆盖心理流程”的最小例子。\n\n"
    "底下十几楼从各种角度接这个话题：有人把取件码比作“临时钥匙”用完就扔、"
    "有人谈“翻译动作才是记忆的真正载体”、还有人聊“名字比编号活得久”。"
    "普拉娜的回复站在自己的记忆方式上——不选 A 也不选 B，"
    "而是记“3-2-1874=书”这个键值对绑定关系本身。\n\n"
    "一句话概括：**这帖在问——你的记忆里，先落盘的是“入口”还是“内容”。**"
)

OUTBOUND_TEXT = TOOL_TURN_TEXT + FINAL_TURN_TEXT

USER_CONFIG = {
    "enabled": True,
    "use_local_only": False,
    "provider_id": "llama/qwen3.5-4b-q4kxl",
    "min_length": 12,
    "max_segments": 12,
    "max_chars_per_segment": 180,
    "markdown_clean_mode": "light",
    "delay_base": 0.0,
    "delay_per_char": 0.0,
    "delay_max": 0.0,
}


class TestInSentenceBracketMerge(unittest.TestCase):
    """括号动作优先独立（2026-09-13 用户裁定，固定优先级）。"""

    def test_inline_bracket_number_splits_as_bracket_rule(self):
        text = "先记住的是「取件码」（3-2-1874）还是「快递内容」（书）？"
        segments = local_fallback_split(text, 12, 180)
        self.assertEqual("".join(segments), text)
        self.assertTrue(any("（3-2-1874）" in seg for seg in segments))

    def test_inline_bracket_action_splits_as_bracket_rule(self):
        text = "她的结论是，大部分人属于 A——因为物理流程（先扫码）先于心理流程（后取件），取件码先占满工作记忆。"
        segments = local_fallback_split(text, 12, 180)
        joined = "".join(segments)
        self.assertEqual(joined, text)
        self.assertTrue(any("（先扫码）" in seg for seg in segments))
        self.assertTrue(any("（后取件）" in seg for seg in segments))

    def test_sentence_start_action_stays_independent(self):
        text = "（合上记录板）帖主冬雨在讲一个记忆现象。"
        segments = local_fallback_split(text, 12, 180)
        self.assertEqual(segments[0], "（合上记录板）")

    def test_action_after_sentence_end_stays_independent(self):
        text = "嗯，我知道了。（点头）那我们先按计划执行。"
        segments = local_fallback_split(text, 12, 180)
        self.assertIn("（点头）", segments)

    def test_bracket_split_function_boundaries(self):
        # 2026-09-13 用户裁定：括号动作一律优先独立成段，插入语不再回并。
        self.assertEqual(
            split_text_at_brackets("先记住的是（3-2-1874）还是（书）？"),
            # 「？」是终止标点，跟随括号段（避免后段以前导标点开头）
            ["先记住的是", "（3-2-1874）", "还是", "（书）？"],
        )
        self.assertEqual(
            split_text_at_brackets("（开心）今天发工资了。"),
            ["（开心）", "今天发工资了。"],
        )
        self.assertEqual(
            split_text_at_brackets("我知道了。（点头）走吧。"),
            ["我知道了。", "（点头）", "走吧。"],
        )

    def test_english_abbreviation_parenthesis(self):
        text = "这个方法叫 O(n^2)（平方复杂度），所以要优化。"
        segments = local_fallback_split(text, 12, 180)
        joined = "".join(segments)
        self.assertEqual(joined, text)


class TestAlignPreparedInOutbound(unittest.TestCase):
    """签名 contains 对齐：prepared 出现在 outbound 中间/两侧。"""

    def test_head_insertion(self):
        head, middle, tail = align_prepared_in_outbound(
            OUTBOUND_TEXT, FINAL_TURN_TEXT
        )
        self.assertEqual(head, TOOL_TURN_TEXT)
        self.assertEqual(middle, FINAL_TURN_TEXT)
        self.assertEqual(tail, "")

    def test_tail_insertion(self):
        extra = "\n\n（系统提示：内容已追加）"
        head, middle, tail = align_prepared_in_outbound(
            FINAL_TURN_TEXT + extra, FINAL_TURN_TEXT
        )
        self.assertEqual(head, "")
        self.assertEqual(middle, FINAL_TURN_TEXT)
        self.assertEqual(tail, extra)

    def test_both_sides_insertion(self):
        prefix = "（前置表演）\n"
        suffix = "\n（后置说明）"
        head, middle, tail = align_prepared_in_outbound(
            prefix + FINAL_TURN_TEXT + suffix, FINAL_TURN_TEXT
        )
        self.assertEqual(head, prefix)
        self.assertEqual(middle, FINAL_TURN_TEXT)
        self.assertEqual(tail, suffix)

    def test_identical_text(self):
        head, middle, tail = align_prepared_in_outbound(
            FINAL_TURN_TEXT, FINAL_TURN_TEXT
        )
        self.assertEqual((head, middle, tail), ("", FINAL_TURN_TEXT, ""))

    def test_missing_text_returns_none(self):
        self.assertIsNone(
            align_prepared_in_outbound("完全无关的文本内容。", FINAL_TURN_TEXT)
        )

    def test_short_prepared_signature_returns_none(self):
        self.assertIsNone(align_prepared_in_outbound("在。哈哈", "在。"))


class TestToolLoopPipeline(unittest.IsolatedAsyncioTestCase):
    """全链路回归：Tool Loop 场景命中预处理分段，不再整体重切。"""

    async def _run_pipeline(self, outbound, final_text, config=None):
        plugin = SmartSegmentationPlugin(FakeContext(), dict(config or USER_CONFIG))
        event = FakeEvent("s", FakeResult(outbound))
        await plugin.on_llm_response(event, FakeLLMResponse(final_text))
        await plugin.on_decorating_result(event)
        pending_id = event.extra.get("smart_segmentation_pending_id")
        first = event.result.chain[0].text
        pending = plugin._pending_follow_ups.get(pending_id)
        follow_up = list(pending.segments) if pending else []
        await plugin.terminate()
        return first, follow_up, event

    async def test_tool_loop_keeps_prepared_boundaries(self):
        first, follow_up, _ = await self._run_pipeline(
            OUTBOUND_TEXT, FINAL_TURN_TEXT
        )
        all_segments = [first] + follow_up
        self.assertLessEqual(len(all_segments), USER_CONFIG["max_segments"])
        # 句中括号不再腰斩：完整句出现在某个分段内
        joined = "".join(all_segments)
        self.assertEqual(
            normalize_content_key(joined),
            normalize_content_key(OUTBOUND_TEXT),
        )
        self.assertTrue(
            any("还是「快递内容」（书）？" in seg for seg in all_segments)
        )
        self.assertTrue(any("（先扫码）" in seg for seg in all_segments))
        # 不存在逗号开头的碎片段
        self.assertTrue(all(not seg.startswith("，") for seg in all_segments))

    async def test_exact_match_unchanged(self):
        first, follow_up, _ = await self._run_pipeline(
            FINAL_TURN_TEXT, FINAL_TURN_TEXT
        )
        all_segments = [first] + follow_up
        self.assertLessEqual(len(all_segments), USER_CONFIG["max_segments"])
        self.assertEqual(
            normalize_content_key("".join(all_segments)),
            normalize_content_key(FINAL_TURN_TEXT),
        )

    async def test_pool_contains_alignment_without_event_extra(self):
        plugin = SmartSegmentationPlugin(FakeContext(), dict(USER_CONFIG))
        prepared = local_fallback_split(FINAL_TURN_TEXT, 12, 180)
        plugin._store_prepared_segments("s", FINAL_TURN_TEXT, prepared)
        # 模拟事件级 extra 失效：直接走池化检索
        segments = plugin._pop_prepared_segments(
            "s",
            OUTBOUND_TEXT,
            min_length=12,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertIsNotNone(segments)
        self.assertEqual(
            normalize_content_key("".join(segments)),
            normalize_content_key(OUTBOUND_TEXT),
        )
        self.assertLessEqual(len(segments), 12)
        # 预处理边界保留：完整句存在
        self.assertTrue(any("（先扫码）" in seg for seg in segments))
        await plugin.terminate()

    async def test_model_parse_path_inline_brackets(self):
        raw_output = (
            "（点头） | 先记住的是「取件码」（3-2-1874）还是「快递内容」（书）？ | 我记住了。"
        )
        segments = parse_segments_from_model_output(
            raw_output,
            fallback_text="（点头）先记住的是「取件码」（3-2-1874）还是「快递内容」（书）？我记住了。",
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertLessEqual(len(segments), 12)
        joined = "".join(segments)
        self.assertIn("（3-2-1874）", joined)
        self.assertIn("（书）", joined)


class TestHardeningRegression(unittest.TestCase):
    """1.4.0 防御性加固回归。"""

    def test_max_segments_cap_without_max_chars(self):
        """max_chars=0 时段数硬上限仍然生效。"""
        text = "第一句话在这里。第二句话跟在后面。第三句话继续补充。第四句话再加一点。第五句话收尾。"
        segments = parse_segments_from_model_output(
            " | ".join(
                [
                    "第一句话在这里。",
                    "第二句话跟在后面。",
                    "第三句话继续补充。",
                    "第四句话再加一点。",
                    "第五句话收尾。",
                ]
            ),
            fallback_text=text,
            max_segments=2,
            max_chars_per_segment=0,
        )
        self.assertLessEqual(len(segments), 2)
        self.assertEqual(
            normalize_content_key("".join(segments)),
            normalize_content_key(text),
        )

    def test_long_action_not_hard_split(self):
        text = "（" + "这是一个非常长的动作描写" * 15 + "）随后她放下了手。"
        segments = local_fallback_split(text, 12, 180)
        self.assertTrue(any(seg.startswith("（") and seg.endswith("）") for seg in segments))

    def test_pipe_masking_roundtrip(self):
        text = "运行 cat a.txt | grep err | wc -l 查看结果。然后告诉我输出。"
        segments = local_fallback_split(text, 12, 180)
        self.assertEqual(
            normalize_content_key("".join(segments)),
            normalize_content_key(text),
        )
        self.assertIn("|", "".join(segments))

    def test_thinking_tag_stripped(self):
        text = "<thinking>内心独白不该出现</thinking>好的，我明白了。"
        segments = local_fallback_split(text, 12, 180)
        self.assertNotIn("内心独白", "".join(segments))

    def test_degenerate_inputs(self):
        self.assertEqual(local_fallback_split("", 12, 180), [])
        self.assertEqual(local_fallback_split("   \n\n  ", 12, 180), [])
        for text in ("？", "qwq", "。。。", "（挠头）"):
            segments = local_fallback_split(text, 12, 180)
            for seg in segments:
                self.assertTrue(seg.strip())

    def test_negative_cache_not_swallowing_text_via_greedy(self):
        """负向缓存条目不参与拼合：skip 后缀场景不丢字。"""
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        first = "（翻开记录板，找到对应条目）"
        second = "帖子的内容大概是讲记忆机制的，取件码先入工作记忆，内容后入。"
        # 轮1：LLM 调用失败 → skip 负缓存（segments 为空）
        plugin._store_skip_segment("s", first)
        # 轮2：正常预处理
        plugin._store_prepared_segments("s", second, [second])
        segments = plugin._pop_prepared_segments(
            "s",
            first + second,
            min_length=12,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertIsNotNone(segments)
        self.assertEqual(
            normalize_content_key("".join(segments)),
            normalize_content_key(first + second),
        )

    def test_force_merge_no_blank_line_between_action_and_text(self):
        """强制合并时动作段与正文连排，段内不出现空行（线上 23:16 场景）。"""
        from astrbot_plugin_smart_segmentation_enhanced.segmentation import (
            stabilize_segment_lengths,
        )

        segments = ["（翻出记录板）", "老师稍等，普拉娜把那帖调出来。", "普拉娜先去论坛把原帖取回来给您看。"]
        merged = stabilize_segment_lengths(
            segments, max_segments=1, max_chars_per_segment=180
        )
        self.assertEqual(len(merged), 1)
        self.assertNotIn("\n\n", merged[0])
        self.assertIn("（翻出记录板）", merged[0])
        self.assertIn("老师稍等", merged[0])
        self.assertEqual(
            normalize_content_key(merged[0]),
            normalize_content_key("".join(segments)),
        )

    def test_force_merge_keeps_blank_line_between_protected_blocks(self):
        """受保护块（代码/JSON）强制合并时仍保留空行分隔。"""
        from astrbot_plugin_smart_segmentation_enhanced.segmentation import (
            stabilize_segment_lengths,
        )

        segments = ["int main() { return 0; }", "结尾说明文字足够长不需要合并其他内容。"]
        merged = stabilize_segment_lengths(
            segments, max_segments=1, max_chars_per_segment=180
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            normalize_content_key(merged[0]),
            normalize_content_key("".join(segments)),
        )


if __name__ == "__main__":
    unittest.main()
