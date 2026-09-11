# tests/test_packing.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from segmentation import (
    compute_target_segment_length,
    local_fallback_split,
)


class TestSegmentationGuardAndFallback(unittest.TestCase):
    def test_target_length_follows_total_and_max_segments(self):
        """验证代码层目标长度随原文和段数增长。"""
        self.assertGreater(
            compute_target_segment_length("x" * 2000, 12, 180),
            compute_target_segment_length("x" * 500, 12, 180),
        )

    def test_dynamic_target_short_sentences_not_glued(self):
        """验证短句按动态目标切短，不粘连成一条长消息。"""
        segments = local_fallback_split(
            "难怪现在输出终于不再重复了。用户，这个方案值得记下来。",
            12,
            180,
        )
        self.assertGreaterEqual(len(segments), 2)
        self.assertTrue(
            any("重复了。" in seg and "记下来。" not in seg for seg in segments)
        )
        self.assertTrue(
            any("记下来。" in seg and "重复了。" not in seg for seg in segments)
        )

    def test_dynamic_target_long_sentence_stays_whole(self):
        """验证长句按动态目标保持完整，不被硬切。"""
        text = (
            "这个方案的核心价值在于把配置从手动加载改成自动读取，"
            "从而彻底减少重复处理，让程序只在真正需要时才解析文件，"
            "不再每轮都加载无关内容。"
        )
        segments = local_fallback_split(text, 12, 180)
        self.assertEqual(segments, [text])

    def test_dynamic_target_mixed_short_and_long(self):
        """验证短句合并成短条、长句独立成条。"""
        text = (
            "短句一。短句二。"
            "这是一句比较长的句子，用来验证动态目标长度是否能让长句保持完整而不被硬切，"
            "同时短句又不会被无脑合并成一条超长消息。"
        )
        segments = local_fallback_split(text, 12, 180)
        self.assertEqual(len(segments), 2)
        self.assertTrue(any("短句一。短句二。" in seg for seg in segments))
        self.assertTrue(any("这是一句比较长的句子" in seg for seg in segments))

    def test_max_segments_cap_still_enforced(self):
        """验证动态目标切分后仍受 max_segments 硬上限约束。"""
        text = "。".join(f"这是第{i}句比较短的内容" for i in range(1, 30)) + "。"
        segments = local_fallback_split(text, 12, 180)
        self.assertLessEqual(len(segments), 12)

    def test_global_max_segments_hard_with_many_action_segments(self):
        """验证动作原子段过多时仍受全局 max_segments 硬上限约束。"""
        text = "".join(f"（动作{i}）" for i in range(14))
        segments = local_fallback_split(text, 12, 180)
        self.assertLessEqual(len(segments), 12)
        self.assertEqual("".join(segments).replace("\n", ""), text)


if __name__ == "__main__":
    unittest.main()
