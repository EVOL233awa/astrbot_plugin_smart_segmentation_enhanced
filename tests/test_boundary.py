# tests/test_boundary.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from segmentation import (
    local_fallback_split,
)
from segmentation.boundary import _force_split_by_symbols


class TestSegmentationGuardAndFallback(unittest.TestCase):
    def test_split_function_and_code_comment_not_broken(self):
        """验证 split() 和代码围栏内注释不会被误切。"""
        segments = local_fallback_split("调用 split() 完成。", 0, 180)
        self.assertEqual(segments, ["调用 split() 完成。"])

        code = "代码：\n```python\n# (注释)\nprint(split())\n```\n结尾"
        code_segments = local_fallback_split(code, 0, 180)
        self.assertTrue(
            any(
                "# (注释)" in seg
                and "split()" in seg
                for seg in code_segments
            )
        )

    def test_inline_link_code_not_split(self):
        """验证行内链接与行内代码不会被括号或句号切开。"""
        segments = local_fallback_split(
            "看这个 [链接](https://example.com) 很酷。",
            0,
            180,
        )
        self.assertTrue(any("[链接](https://example.com)" in seg for seg in segments))
        self.assertEqual(
            local_fallback_split("代码：`split()` 会分割。", 0, 180),
            ["代码：`split()` 会分割。"],
        )


if __name__ == "__main__":
    unittest.main()


class TestStageFourCutGuards(unittest.TestCase):
    """阶段 4：no_split_around 候选抑制与 force_split_symbols 预切。"""

    def test_pick_priority_cut_suppresses_guarded_cut(self):
        """切点后紧跟保护词时不选该切点；无其他候选返回 None。"""
        from segmentation.boundary import _pick_priority_cut

        text = "啊。Provider 后续还有一段没有标点的内容若干"
        self.assertEqual(_pick_priority_cut(text, 5, 0, 0), 2)
        self.assertIsNone(
            _pick_priority_cut(text, 5, 0, 0, no_split_around=["Provider"])
        )

    def test_no_split_guard_allows_whitespace_and_nfkc(self):
        """切点与保护词之间允许空白；全半角归一化等价。"""
        from segmentation.boundary import _no_split_guard

        self.assertTrue(_no_split_guard("啊。 Provider 在线", 2, ["Provider"], 8))
        self.assertTrue(_no_split_guard("啊。Ｐｒｏｖｉｄｅｒ在线", 2, ["Provider"], 8))
        self.assertFalse(_no_split_guard("啊。提供方在线", 2, ["Provider"], 8))

    def test_force_split_by_symbols_keeps_symbol_in_front(self):
        """强制切分把符号留在前段末尾。"""
        self.assertEqual(
            _force_split_by_symbols("前半句|后半句", ["|"]),
            ["前半句|", "后半句"],
        )

    def test_force_split_by_symbols_nfkc_equivalence(self):
        """文本里的全角符号与配置的半角符号等价；省略号膨胀后位置正确。"""
        self.assertEqual(
            _force_split_by_symbols("前半句｜后半句", ["|"]),
            ["前半句｜", "后半句"],
        )
        self.assertEqual(
            _force_split_by_symbols("前半句……后半句", ["……"]),
            ["前半句……", "后半句"],
        )

    def test_force_split_by_symbols_overlap_and_no_hit(self):
        """重叠命中保留先收集的区间；无命中返回原文本。"""
        self.assertEqual(_force_split_by_symbols("没有符号的句子", ["|"]), ["没有符号的句子"])
        # "|" 与 "||" 同时配置：先收集的 "|" 命中逐个生效
        self.assertEqual(
            _force_split_by_symbols("a||b", ["|"]),
            ["a|", "|", "b"],
        )

    def test_local_force_split_survives_packing(self):
        """强制预切的片段各自独立打包，不被目标长度合并回去。"""
        default = local_fallback_split("前半句|后半句还有内容，继续说下去。", 8, 180)
        forced = local_fallback_split(
            "前半句|后半句还有内容，继续说下去。",
            8,
            180,
            force_split_symbols=["|"],
        )
        self.assertEqual(default, ["前半句|后半句还有内容，继续说下去。"])
        self.assertEqual(forced[0], "前半句|")
        self.assertEqual(forced[1], "后半句还有内容，继续说下去。")

    def test_local_force_split_skips_protected_block(self):
        """受保护块（代码围栏）内部的强制符号不切分。"""
        text = "代码如下：\n```\na|b\n```\n后续说明。"
        segments = local_fallback_split(text, 8, 180, force_split_symbols=["|"])
        self.assertTrue(any("a|b" in seg for seg in segments))
        self.assertFalse(any(seg.strip() == "a|" for seg in segments))


class TestWhitespaceQuotesAndActionBoundaries(unittest.TestCase):
    """P1 回归：空白/换行保留、标点簇、引号深度与动作段收尾标点。"""

    def test_ascii_sentences_keep_separator_space(self):
        segments = local_fallback_split("Hello. World. This is fine.", 12, 180)
        joined = "".join(segments)
        self.assertIn("Hello. World.", joined)
        self.assertNotIn("Hello.World", joined)

        self.assertEqual(
            local_fallback_split("A. B. C.", 12, 180),
            ["A. B. C."],
        )

    def test_newlines_between_lines_preserved(self):
        self.assertEqual(
            local_fallback_split("第一行内容\n第二行内容", 12, 180),
            ["第一行内容\n第二行内容"],
        )

    def test_paragraph_break_not_glued(self):
        segments = local_fallback_split(
            "老师，晚上好。\n\n您看起来有些疲倦。需要休息吗？", 12, 180
        )
        joined = "".join(segments)
        self.assertIn("\n\n", joined)
        self.assertNotIn("好。您看起来", joined)

    def test_force_split_absorbs_punctuation_cluster(self):
        self.assertEqual(
            local_fallback_split(
                "真的吗！！！然后呢。", 12, 180, force_split_symbols=["！"]
            ),
            ["真的吗！！！", "然后呢。"],
        )
        self.assertEqual(
            local_fallback_split(
                "等等……算了。", 12, 180, force_split_symbols=["..."]
            ),
            ["等等……", "算了。"],
        )

    def test_leading_punctuation_not_split_into_own_segment(self):
        self.assertEqual(
            local_fallback_split(
                "！！！然后呢。", 12, 180, force_split_symbols=["！"]
            ),
            ["！！！然后呢。"],
        )

    def test_quote_interior_is_not_a_cut_position(self):
        from segmentation.boundary import _tier_cut_positions

        text = "「今天天气很好。我们出发吧。」然后回家。"
        # 引号内的「。」不产生切点，只有句末「。」之后的位置可切。
        self.assertEqual(_tier_cut_positions(text, 1), [len(text)])

    def test_action_bracket_with_trailing_punctuation_stays_atomic(self):
        self.assertEqual(
            local_fallback_split("（点头）。好的，我明白了。", 12, 180),
            ["（点头）。", "好的，我明白了。"],
        )


class TestEllipsisAfterSentenceEnd(unittest.TestCase):
    """2026-09-13 用户裁定：句末标点/括号后的省略号归下一段开头，不连排。"""

    def test_sentence_end_ellipsis_moves_to_next_segment(self):
        """句号后的省略号归下一段：句号段干净结尾，省略号开下一段。"""
        segments = local_fallback_split(
            "就这样吧。……下次再说。", 12, 180
        )
        self.assertEqual(
            segments,
            ["就这样吧。", "……下次再说。"],
        )

    def test_bracket_ellipsis_moves_to_next_segment(self):
        """括号后的省略号归下一段：括号段独立，省略号开下一段。"""
        segments = local_fallback_split(
            "门锁好了。（压低声音）……我们换个地方说，这里不安全。",
            12,
            180,
        )
        self.assertEqual(
            segments,
            [
                "门锁好了。",
                "（压低声音）",
                "……我们换个地方说，这里不安全。",
            ],
        )

    def test_content_ellipsis_pause_still_standalone(self):
        """文字后的省略号停顿段语义不变（「让我再想想……」独立）。"""
        segments = local_fallback_split("让我再想想……好的。", 12, 180)
        self.assertIn("让我再想想……", segments)
