# tests/test_local_split.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from segmentation import (
    local_fallback_split,
    stabilize_segment_lengths,
)
from segmentation.boundary import _PAUSE_BREAK_MIN_CHARS


class TestSegmentationGuardAndFallback(unittest.TestCase):
    def test_local_fallback_splits_action_and_speech(self):
        """验证本地兜底会优先把圆括号动作与话语分开。"""
        segments = local_fallback_split(
            "（开心）今天发工资了，要不要一起去吃大餐？",
            8,
        )
        self.assertIn("（开心）", segments)
        self.assertTrue(any("今天发工资了" in seg for seg in segments))

    def test_stabilize_segment_lengths_splits_long_segment(self):
        """验证超长分段会被二次拆分，避免单条消息过长。"""
        long_segment = "第一句很好。第二句也很好。第三句还是很好。" * 20
        stabilized = stabilize_segment_lengths(
            [long_segment],
            max_segments=20,
            max_chars_per_segment=80,
        )
        self.assertGreater(len(stabilized), 1)

    def test_long_text_has_no_overlong_tail(self):
        """验证长文在硬段数下不会把尾部全部堆到最后一段。"""
        text = "第一句很好。第二句也很好。第三句还是很好。" * 40
        segments = local_fallback_split(text, 8, 180)
        self.assertLessEqual(len(segments), 8)
        self.assertLessEqual(max(len(seg) for seg in segments), 180)

    def test_local_fallback_preserves_multiline_action_bracket(self):
        """验证跨多行的动作括号在本地切分时整体独立成段。"""
        original = (
            "（看到用户已经回到工位，测试助手安静地翻开笔记本，\n"
            "逐行核对刚才的记录，确认没有遗漏）\n\n"
            "……好，现在继续处理下一项。"
        )
        segments = local_fallback_split(original, 12, 180)
        self.assertEqual(len(segments), 2)
        self.assertTrue(segments[0].startswith("（") and segments[0].endswith("）"))
        self.assertEqual(segments[1], "……好，现在继续处理下一项。")

    def test_local_fallback_long_action_is_not_fragmented(self):
        """验证超长动作括号不会被 max_chars 切成碎片。"""
        original = "（" + "很长的动作描写" * 30 + "）然后助手轻轻开口说话。"
        segments = local_fallback_split(original, 12, 80)
        self.assertEqual(len(segments), 2)
        self.assertTrue(segments[0].startswith("（") and segments[0].endswith("）"))
        self.assertIn("然后助手轻轻开口说话", segments[1])

    def test_stabilize_segment_lengths_keeps_action_atomic(self):
        """验证长度稳定阶段不会拆分完整动作括号。"""
        long_action = "（" + "很长" * 80 + "）"
        stabilized = stabilize_segment_lengths(
            [long_action],
            max_segments=12,
            max_chars_per_segment=80,
        )
        self.assertEqual(stabilized, [long_action])

    def test_question_exclamation_marks_allow_hard_split(self):
        """验证？！/！/？可以成为本地打包阶段的硬分段边界。"""
        self.assertEqual(
            local_fallback_split("真的吗？！然后呢。", 12, 180),
            ["真的吗？！", "然后呢。"],
        )
        self.assertEqual(
            local_fallback_split("第一句！第二句？", 12, 180),
            ["第一句！", "第二句？"],
        )

    def test_long_bracket_split_as_action(self):
        """验证长动作括号也会独立成段，不会和台词粘在一起。"""
        long_text = "（" + "这是超过三十个字符的说明文字" * 3 + "）然后说话。"
        segments = local_fallback_split(long_text, 8, 180)
        self.assertEqual(len(segments), 2)
        self.assertTrue(any(seg.startswith("（") and seg.endswith("）") for seg in segments))
        self.assertTrue(any("然后说话" in seg for seg in segments))

    def test_stabilize_max_segments_hard_with_many_action_segments(self):
        """验证长度稳定阶段的最终兜底也能压到硬段数以内。"""
        segments = [f"（动作{i}）" for i in range(14)]
        stabilized = stabilize_segment_lengths(
            segments,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertLessEqual(len(stabilized), 12)
        self.assertEqual("".join(stabilized).replace("\n", ""), "".join(segments))

    def test_ellipsis_pause_min_chars_configurable(self):
        """验证省略号停顿独立阈值可经参数配置：短停顿归属随阈值变化。"""
        # 「啊……」共 3 字符：阈值 3 时独立成段，阈值 4 时留在原句。
        text = "（动作）啊……等一下！"
        with_pause_independent = local_fallback_split(
            text, 12, 180, pause_min_chars=3
        )
        with_pause_merged = local_fallback_split(
            text, 12, 180, pause_min_chars=4
        )
        self.assertIn("啊……", with_pause_independent)
        self.assertNotIn("啊……等一下！", with_pause_independent)
        self.assertIn("啊……等一下！", with_pause_merged)

    def test_conf_schema_ellipsis_pause_key_matches_code_default(self):
        """验证 schema 的省略号阈值键存在，且默认值与代码层兜底一致。"""
        import json

        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "_conf_schema.json",
        )
        with open(schema_path, encoding="utf-8") as handle:
            schema = json.load(handle)
        entry = schema.get("ellipsis_pause_min_chars")
        self.assertIsInstance(entry, dict)
        self.assertEqual(entry.get("type"), "int")
        self.assertEqual(entry.get("default"), 4)
        self.assertEqual(entry.get("default"), _PAUSE_BREAK_MIN_CHARS)
        self.assertEqual(entry.get("slider", {}).get("min"), 2)
        self.assertEqual(entry.get("slider", {}).get("max"), 10)


if __name__ == "__main__":
    unittest.main()


class TestStageFourSplitOptions(unittest.TestCase):
    """阶段 4：短尾合并的端到端行为与默认关闭不变性。"""

    def test_short_tail_merge_enabled_and_disabled(self):
        """默认关闭时分段不变；开启后末段并入前一段，阈值生效。"""
        text = "今天上午的日程已经排满了，主要有三个会议连着开。明白。"
        default = local_fallback_split(text, 8, 180)
        self.assertEqual(
            default,
            ["今天上午的日程已经排满了，主要有三个会议连着开。", "明白。"],
        )
        merged = local_fallback_split(
            text, 8, 180, merge_short_tail=True, short_tail_threshold=3
        )
        self.assertEqual(
            merged, ["今天上午的日程已经排满了，主要有三个会议连着开。明白。"]
        )
        # 阈值 2 时「明白。」(3 字符) 不并入
        kept = local_fallback_split(
            text, 8, 180, merge_short_tail=True, short_tail_threshold=2
        )
        self.assertEqual(kept, default)

    def test_short_tail_merge_skips_action_tail(self):
        """尾段是动作段时即使开启短尾合并也保持独立。"""
        text = "今天上午的日程已经排满了，主要有三个会议连着开。（点头）"
        segments = local_fallback_split(
            text, 8, 180, merge_short_tail=True, short_tail_threshold=5
        )
        self.assertEqual(segments[-1], "（点头）")
