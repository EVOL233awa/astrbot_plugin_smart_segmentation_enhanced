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


class TestStageFourStageOptions(unittest.TestCase):
    """阶段 4：短尾合并、llm 边界过滤与强制再切分、schema 条件显示。"""

    def test_merge_short_tail_segment_merges_tail(self):
        """末段不超过阈值且两段都是普通文本时并入前一段。"""
        from segmentation import merge_short_tail_segment

        self.assertEqual(
            merge_short_tail_segment(["第一段话说完了。", "好。"], 0, 2),
            ["第一段话说完了。好。"],
        )
        # 超过阈值不合并
        self.assertEqual(
            merge_short_tail_segment(["第一段话说完了。", "明白。"], 0, 2),
            ["第一段话说完了。", "明白。"],
        )

    def test_merge_short_tail_segment_guards(self):
        """原子段/受保护块豁免；合并后超 max_chars 放弃。"""
        from segmentation import merge_short_tail_segment

        # 合并后超过 max_chars
        self.assertEqual(
            merge_short_tail_segment(
                ["这是一条已经很长的消息内容了。", "好。"], 10, 2
            ),
            ["这是一条已经很长的消息内容了。", "好。"],
        )
        # 尾段是动作段（原子）
        self.assertEqual(
            merge_short_tail_segment(["第一段话说完了。", "（笑）"], 0, 3),
            ["第一段话说完了。", "（笑）"],
        )
        # 尾段是受保护块
        self.assertEqual(
            merge_short_tail_segment(
                ["第一段话说完了。", "```\ncode\n```"], 0, 12
            ),
            ["第一段话说完了。", "```\ncode\n```"],
        )
        # 段数不足或阈值非正
        self.assertEqual(merge_short_tail_segment(["好。"], 0, 2), ["好。"])
        self.assertEqual(
            merge_short_tail_segment(["第一段。", "好。"], 0, 0),
            ["第一段。", "好。"],
        )

    def test_apply_no_split_boundaries(self):
        """边界后紧跟保护词时合回相邻两段；受保护块与超限豁免。"""
        from segmentation import apply_no_split_boundaries

        self.assertEqual(
            apply_no_split_boundaries(
                ["版本号是 3.2。", "Provider 当前在线"], ["Provider"]
            ),
            ["版本号是 3.2。Provider 当前在线"],
        )
        # 合并后超过 max_chars 保持原边界
        self.assertEqual(
            apply_no_split_boundaries(
                ["版本号是 3.2。", "Provider 当前在线"], ["Provider"], max_chars=10
            ),
            ["版本号是 3.2。", "Provider 当前在线"],
        )
        # 前段是受保护块不合并
        self.assertEqual(
            apply_no_split_boundaries(
                ["```\ncode\n```", "Provider 后续"], ["Provider"]
            ),
            ["```\ncode\n```", "Provider 后续"],
        )

    def test_apply_force_split_to_segments(self):
        """非受保护片段按符号再切分；受保护块与原子段保持完整。"""
        from segmentation import apply_force_split_to_segments

        self.assertEqual(
            apply_force_split_to_segments(["前半句|后半句"], ["|"]),
            ["前半句|", "后半句"],
        )
        self.assertEqual(
            apply_force_split_to_segments(["```\na|b\n```"], ["|"]),
            ["```\na|b\n```"],
        )
        self.assertEqual(
            apply_force_split_to_segments(["（挥手）"], ["|"]),
            ["（挥手）"],
        )

    def test_conf_schema_stage_four_keys(self):
        """schema：短尾合并开关/阈值（条件显示）与两个列表键。"""
        import json

        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "_conf_schema.json",
        )
        with open(schema_path, encoding="utf-8") as handle:
            schema = json.load(handle)

        switch = schema["merge_short_tail"]
        self.assertEqual(switch["type"], "bool")
        self.assertIs(switch["default"], False)
        self.assertNotIn("condition", switch)

        threshold = schema["short_tail_threshold"]
        self.assertEqual(threshold["type"], "int")
        self.assertEqual(threshold["default"], 2)
        self.assertEqual(threshold["condition"], {"merge_short_tail": True})
        self.assertEqual(threshold["slider"], {"min": 1, "max": 10, "step": 1})

        for key in ("no_split_around", "force_split_symbols"):
            entry = schema[key]
            self.assertEqual(entry["type"], "list", key)
            self.assertEqual(entry["default"], [], key)


class TestRepackGuards(unittest.TestCase):
    """P3 回归：重排自救不得拆开动作段。"""

    def test_repack_rejects_action_segments(self):
        from segmentation.packing import _repack_within_caps

        self.assertIsNone(
            _repack_within_caps(
                ["（点头）", "普通文本" * 6],
                target=20,
                max_segments=4,
                max_chars=40,
            )
        )


class TestEllipsisLeadingSegment(unittest.TestCase):
    """2026-09-13 用户裁定：省略号开头的段不并入前段（pack 层）。"""

    def test_ellipsis_leading_segment_not_merged_back(self):
        from segmentation import pack_segments_to_target

        packed = pack_segments_to_target(
            ["开会就到这里。", "……明天记得带材料。"],
            100,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertEqual(packed, ["开会就到这里。", "……明天记得带材料。"])
