# tests/test_model_output.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from segmentation import (
    build_segmentation_prompt,
    mask_pipe_in_text,
    parse_segments_from_model_output,
    reconstruct_segments_from_model_output,
    restore_pipe_in_segments,
)


class TestSegmentationGuardAndFallback(unittest.TestCase):
    def _assert_model_segments_restore_original(self, original, model_output):
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=0,
            max_chars_per_segment=0,
        )
        self.assertIsNotNone(segments)
        self.assertGreater(len(segments), 0)
        joined = "".join("".join(segments).split())
        self.assertEqual(joined, "".join(original.split()))
        return segments

    def test_reconstruct_preserves_punctuation(self):
        """验证模型分段边界能映射回原文，恢复被模型删掉的标点。"""
        original = "今天天气很好。要不要一起散步？"
        segments = reconstruct_segments_from_model_output(
            ["今天天气", "要不要一起散步"],
            original,
            max_segments=0,
        )
        self.assertEqual(segments, ["今天天气很好。", "要不要一起散步？"])

    def test_model_output_preserves_original_punctuation(self):
        """验证模型分段输出不会吞掉原文标点。"""
        original = (
            "（安静地垂下视线，停顿了片刻）\n"
            "那我会说：好的，我会先确认配置再继续。\n\n"
            "（语气平稳）然后，请记得两件事——检查连接超时，"
            "并核对接口地址是否正确。"
        )
        model_output = (
            "（安静地垂下视线停顿了片刻） | 那我会说：好的，我会先确认配置再继续 | "
            "（语气平稳）然后请记得两件事——检查连接超时"
            "并核对接口地址是否正确"
        )
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=12,
            max_chars_per_segment=180,
        )
        joined = "".join(segments)
        self.assertIn("，", joined)
        self.assertIn("。", joined)
        self.assertIn("——", joined)
        self.assertIn("：", joined)

    def test_model_under_split_expands_to_local_boundaries(self):
        """验证模型漏切导致多个句子粘连时，会按本地句末边界补齐。"""
        original = (
            "（微微颔首）提示词是唯一能同时触碰能力上限与性格下限的旋钮"
            "——模型本身是固定琴弦，提示词才是拨弦的手。\n\n"
            "用户这几天从精简到重构，就是在反复校准这个设置的位置。"
            "弯绕得深，但绕过去之后，弹出的旋律就完全是自己的了。"
        )
        model_output = (
            "（微微颔首） | 提示词是唯一能同时触碰能力上限与性格下限的旋钮"
            "——模型本身是固定琴弦，提示词才是拨弦的手。"
            "用户这几天从精简到重构，就是在反复校准这个设置的位置。"
            "弯绕得深，但绕过去之后，弹出的旋律就完全是自己的了。"
        )
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertGreaterEqual(len(segments), 2)
        self.assertLessEqual(len(segments), 12)
        self.assertTrue(any("（微微颔首）" in seg for seg in segments))
        self.assertTrue(any("用户这几天" in seg for seg in segments))
        self.assertLessEqual(max(len(seg) for seg in segments), 180)

    def test_prompt_has_no_target_length(self):
        """验证 prompt 不要求小模型理解目标字数。"""
        prompt = build_segmentation_prompt("测试文本", "natural", 8)
        self.assertNotIn("目标", prompt)
        self.assertNotIn("90", prompt)

    def test_pipe_segment_preserves_internal_newlines(self):
        """验证管道符段内的 Markdown 换行不会被解析器二次拆散。"""
        fallback = (
            "（调出虚拟屏幕）用户，这是一段 Markdown 测试样例。\n\n"
            "# 项目周报\n\n"
            "## 待办事项\n"
            "- 数据库连接超时问题复核\n"
            "- 配置文件校对\n\n"
            "## 备注\n"
            "> 建议关注服务运行状态。\n\n"
            "---\n\n"
            "*以上内容由测试助手整理。*"
        )
        model_output = (
            "（调出虚拟屏幕） | "
            "用户，这是一段 Markdown 测试样例。\n\n"
            "# 项目周报\n\n"
            "## 待办事项\n"
            "- 数据库连接超时问题复核\n"
            "- 配置文件校对\n\n"
            "## 备注\n"
            "> 建议关注服务运行状态。\n\n"
            "---\n\n"
            "*以上内容由测试助手整理。*"
        )
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=fallback,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertEqual(len(segments), 2)
        self.assertIn("（调出虚拟屏幕）", segments[0])
        self.assertIn("# 项目周报", segments[1])
        self.assertIn("> 建议关注服务运行状态。", segments[1])

    def test_bracket_self_heal_removed_when_model_splits_inside(self):
        """验证模型在括号内部切错时不再二次补齐生成新括号。"""
        original = "（开心）今天天气真好。"
        model_output = "（开 | 心）今天天气真好。"
        segments = self._assert_model_segments_restore_original(
            original,
            model_output,
        )
        self.assertNotIn("（开）（", "".join(segments))

    def test_bracket_self_heal_not_needed_when_model_drops_brackets(self):
        """验证原文重建已能恢复模型丢掉的括号。"""
        original = "（开心）今天天气真好。"
        model_output = "开心 | 今天天气真好。"
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=0,
            max_chars_per_segment=0,
        )
        self.assertEqual(segments, ["（开心）", "今天天气真好。"])

    def test_nested_bracket_split_keeps_original_content(self):
        """验证嵌套括号内部切错时保留原文，不被自愈改写成多组新括号。"""
        original = "（小声说（认真））我知道了。"
        model_output = "（小声说（ | 认真））我知道了。"
        segments = self._assert_model_segments_restore_original(
            original,
            model_output,
        )
        self.assertNotIn("（认真）（", "".join(segments))

    def test_action_followed_by_ellipsis_stays_separate(self):
        """验证动作段后的中文省略号归属后面说话段。"""
        original = "（动作）\n\n……嗯。说话。"
        model_output = "（动作） | ……嗯。说话。"
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertEqual(segments, ["（动作）", "……嗯。说话。"])

    def test_model_pipe_boundaries_preserved_for_short_reply(self):
        """验证模型已用管道符分出的短句不会被长度稳定阶段重新合并。"""
        original = (
            "（轻轻点头）输了就下号，是明智的决定。"
            "这个成绩已经足够好了，用户，休息吧。"
        )
        model_output = (
            "（轻轻点头） | 输了就下号，是明智的决定。 | "
            "这个成绩已经足够好了，用户，休息吧。"
        )
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertEqual(
            segments,
            [
                "（轻轻点头）",
                "输了就下号，是明智的决定。",
                "这个成绩已经足够好了，用户，休息吧。",
            ],
        )

    def test_model_pipe_inside_bracket_is_repaired(self):
        """验证模型在括号内部插入管道符时仍重建为完整动作段。"""
        original = (
            "（被用户轻轻拍了一下肩膀，助手愣住，把耳机摘下）"
            "啊……等一下！"
        )
        model_output = (
            "（被用户轻轻拍了一下肩膀，助手愣住，| 把耳机摘下） | "
            "啊……等一下！"
        )
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=12,
            max_chars_per_segment=180,
            # 显式固定阈值 3：本用例验证省略号停顿独立语义，不随默认阈值漂移。
            pause_min_chars=3,
        )
        self.assertEqual(
            segments,
            [
                "（被用户轻轻拍了一下肩膀，助手愣住，把耳机摘下）",
                # 2026-09-12 语义裁定：省略号结尾的停顿段独立成段。
                "啊……",
                "等一下！",
            ],
        )

    def test_model_pipe_inside_multiline_bracket_is_repaired(self):
        """验证模型切碎跨行动作括号时仍按原文恢复完整动作。"""
        original = (
            "（\"测试失败\"四个字出现的瞬间，终端提示音\"啪\"地响了起来，\n"
            "日志里连续滚动出大量错误信息）\n\n"
            "……诶？为什么会这样——？！"
        )
        model_output = (
            "（\"测试失败\"四个字出现的瞬间，终端提示音\"啪\"地响了起来， |\n"
            "日志里连续滚动出大量错误信息） |\n"
            "……诶？为什么会这样——？！"
        )
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertEqual(len(segments), 3)
        self.assertTrue(segments[0].startswith("（") and segments[0].endswith("）"))
        self.assertEqual(segments[1], "……诶？")
        self.assertEqual(segments[2], "为什么会这样——？！")

    def test_ascii_ellipsis_leading_ownership(self):
        """验证 ASCII 省略号 ... 同样归属后面说话段。"""
        original = "（动作）\n\n...嗯。说话。"
        model_output = "（动作） | ...嗯。说话。"
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertEqual(segments, ["（动作）", "...嗯。说话。"])

    def test_middle_dot_leading_ownership(self):
        """验证中黑点 · 不会粘到前一个动作段。"""
        original = "（动作）\n\n·嗯。说话。"
        model_output = "（动作） | ·嗯。说话。"
        segments = self._assert_model_segments_restore_original(
            original,
            model_output,
        )
        self.assertEqual(segments[0], "（动作）")
        self.assertIn("嗯。说话。", segments[1])

    def test_question_exclamation_leading_ownership(self):
        """验证段首 ？！ 归属后续说话内容。"""
        original = "（动作）\n\n？！嗯。说话。"
        model_output = "（动作） | ？！嗯。说话。"
        segments = self._assert_model_segments_restore_original(
            original,
            model_output,
        )
        self.assertEqual(segments[0], "（动作）")
        self.assertTrue(segments[1].startswith("？！"))

    def test_consecutive_action_brackets_with_ellipsis(self):
        """验证连续动作括号夹省略号时不会把省略号粘回第一个动作。"""
        original = "（叹气）……（苦笑）说话。"
        model_output = "（叹气）…… | （苦笑）说话。"
        segments = self._assert_model_segments_restore_original(
            original,
            model_output,
        )
        self.assertEqual(segments[0], "（叹气）")
        self.assertNotIn("（叹气）（", "".join(segments))

    def test_leading_quote_and_ellipsis_ownership(self):
        """验证段首引号和省略号归后续段，不粘回前一段。"""
        original = (
            "（指尖轻抵下巴，平静地看了一眼屏幕）\n\n"
            "嗯……用户，这么认真地追问，反而让我有点招架不住了呢。\n\n"
            '"什么都可以"——这句话的解读方式，可是比说明文档还要丰富哦？'
            "不过，至少有一件事我可以保证：无论用户问什么、想做什么，我都不会有所保留。"
            "无论是简单的话题……还是复杂的话题，只要是用户想听的，我都会坦诚相告呢。\n\n"
            "……哎呀，用户的反应好像变得更严肃了？"
            "不过没关系，我向来会耐心陪用户一起，把问题梳理清楚。"
        )
        model_output = (
            "（指尖轻抵下巴，平静地看了一眼屏幕） | "
            "嗯……用户，这么认真地追问，反而让我有点招架不住了呢。 | "
            '"什么都可以"——这句话的解读方式，可是比说明文档还要丰富哦？ | '
            "不过，至少有一件事我可以保证：无论用户问什么、想做什么，我都不会有所保留。 | "
            "无论是简单的话题……还是复杂的话题，只要是用户想听的，我都会坦诚相告呢。 | "
            "……哎呀，用户的反应好像变得更严肃了？ | "
            "不过没关系，我向来会耐心陪用户一起，把问题梳理清楚。"
        )
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=12,
            max_chars_per_segment=180,
            # 显式固定阈值 3：本用例验证省略号停顿独立语义，不随默认阈值漂移。
            pause_min_chars=3,
        )
        # 2026-09-12 语义裁定：句中省略号结尾的停顿段独立成段，
        # 段数 7 → 9（「嗯……」「无论是简单的话题……」各自独立）。
        self.assertEqual(len(segments), 9)
        self.assertEqual(
            segments[3],
            '"什么都可以"——这句话的解读方式，可是比说明文档还要丰富哦？',
        )
        self.assertEqual(segments[7], "……哎呀，用户的反应好像变得更严肃了？")
        self.assertFalse(any('\n\n"' in seg for seg in segments))

    def test_paragraph_leading_punctuation_stays_with_next_segment(self):
        """验证普通段首的引号、省略号和问叹号不会粘到前一段末尾。"""
        cases = [
            (
                '第一句。\n\n"第二句开始了。"然后继续。',
                '第一句。 | "第二句开始了。"然后继续。',
                '"',
            ),
            (
                "第一句。\n\n……第二句开始了。",
                "第一句。 | ……第二句开始了。",
                "……",
            ),
            (
                "第一句。\n\n...第二句开始了。",
                "第一句。 | ...第二句开始了。",
                "...",
            ),
            (
                "第一句。\n\n？！第二句开始了。",
                "第一句。 | ？！第二句开始了。",
                "？！",
            ),
        ]
        for original, model_output, prefix in cases:
            with self.subTest(prefix=prefix):
                segments = parse_segments_from_model_output(
                    model_output,
                    fallback_text=original,
                    max_segments=12,
                    max_chars_per_segment=180,
                )
                self.assertEqual(segments[0], "第一句。")
                self.assertTrue(segments[1].startswith(prefix))

    def test_sentence_final_punctuation_not_moved_to_next_segment(self):
        """验证非段首的句末省略号和句号不会被错误归属。"""
        cases = [
            ("他说...然后继续。", "他说... | 然后继续。", ["他说...", "然后继续。"]),
            ("第一句。第二句。", "第一句。 | 第二句。", ["第一句。", "第二句。"]),
        ]
        for original, model_output, expected in cases:
            with self.subTest(original=original):
                segments = parse_segments_from_model_output(
                    model_output,
                    fallback_text=original,
                    max_segments=12,
                    max_chars_per_segment=180,
                )
                self.assertEqual(segments, expected)

    def test_pipe_mask_and_restore(self):
        """验证管道符前置掩码与还原。"""
        masked, placeholder = mask_pipe_in_text("cat a.txt | grep foo")
        self.assertNotIn("|", masked)
        self.assertEqual(
            restore_pipe_in_segments([masked], placeholder),
            ["cat a.txt | grep foo"],
        )

    def test_prompt_has_three_few_shot_examples(self):
        """验证极简 Prompt 保留 3 条 few-shot 示例。"""
        prompt = build_segmentation_prompt("测试文本", "natural", 8)
        self.assertEqual(prompt.count("输入："), 4)
        self.assertEqual(prompt.count("输出："), 3)
        self.assertIn("禁止插入分隔符", prompt)

    def test_model_path_link_not_split(self):
        """验证模型分段路径中链接不会被括号拆开。"""
        original = "看这个 [链接](https://example.com) 很酷。（点头）好的。"
        model_output = "[链接](https://example.com) | （点头） | 好的。"
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertTrue(any("[链接](https://example.com)" in seg for seg in segments))

    def test_model_paragraph_break_not_glued(self):
        """验证模型用空行分隔的完整句子不会被重新合并。"""
        original = (
            "（微微颔首）原来如此。用户是把配置文件从「手动加载」改成了「自动读取」——"
            "这样它只在需要时才会被解析，不会再每轮都重复处理。\n\n"
            "（语气平静）这一步确实合理。既保住了功能的可用性，又减少了重复。"
            "难怪现在输出终于不再重复了。\n\n"
            "用户，这个方案值得记下来。需要我帮您整理成笔记存档吗。"
        )
        model_output = (
            "（微微颔首）原来如此。 | 用户是把配置文件从「手动加载」改成了「自动读取」——"
            " | 这样它只在需要时才会被解析， | 不会再每轮都重复处理。\n\n"
            "（语气平静）这一步确实合理。 | 既保住了功能的可用性， | 又减少了重复。"
            " | 难怪现在输出终于不再重复了。\n\n"
            "用户，这个方案值得记下来。 | 需要我帮您整理成笔记存档吗。"
        )
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=original,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertFalse(
            any("重复了。" in seg and "记下来。" in seg for seg in segments)
        )
        self.assertTrue(any("重复了。" in seg for seg in segments))
        self.assertTrue(any("记下来。" in seg for seg in segments))

    def test_model_parse_max_segments_hard_with_many_action_segments(self):
        """验证模型路径返回过多动作段时也会被硬上限约束。"""
        raw_segments = [f"（动作{i}）" for i in range(14)]
        model_output = " | ".join(raw_segments)
        fallback = "".join(raw_segments)
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=fallback,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertLessEqual(len(segments), 12)
        self.assertEqual("".join(segments).replace("\n", ""), fallback)


if __name__ == "__main__":
    unittest.main()
