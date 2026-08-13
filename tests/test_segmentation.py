# test_segmentation.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from segmentation import (
    build_segmentation_prompt,
    clean_markdown_by_mode,
    compute_target_segment_length,
    has_protected_block,
    is_non_natural_language,
    is_markdown_document,
    is_markdown_heavy,
    local_fallback_split,
    normalize_content_key,
    parse_segments_from_model_output,
    reconstruct_segments_from_model_output,
    stabilize_segment_lengths,
)

class TestSegmentationGuardAndFallback(unittest.TestCase):

    def test_pure_json_document(self):
        """验证纯 JSON 格式能被准确识别为非自然语言，并作为单段完整输出。"""
        pure_json = '{"mcpServers": {"test-server": {"command": "node", "active": true}}}'
        self.assertTrue(is_non_natural_language(pure_json))
        
        segments = local_fallback_split(pure_json, max_segs=0)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0], pure_json)

    def test_pure_code_document(self):
        """验证纯代码块（含三反引号）能被准确识别并整块保留。"""
        pure_code = "```python\ndef add(a, b):\n    return a + b\n```"
        self.assertTrue(is_non_natural_language(pure_code))
        
        segments = local_fallback_split(pure_code, max_segs=0)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0], pure_code)

    def test_markdown_with_embedded_json(self):
        """验证含有 JSON 片段的 Markdown 文档不会被误判定为纯非自然语言，而能正确按双换行切分段落。"""
        md_text = (
            "# 接口配置说明\n\n"
            "以下是我们的服务配置 JSON 样例：\n\n"
            "{\n  \"port\": 8080,\n  \"host\": \"localhost\"\n}\n\n"
            "请确保上述配置正确无误后再启动服务。"
        )
        # 混合 Markdown 不应整篇放行，JSON 块应单独保护
        self.assertFalse(is_markdown_document(md_text))
        
        segments = local_fallback_split(md_text, max_segs=0)
        self.assertEqual(len(segments), 3)
        self.assertTrue(any(seg.startswith("{") for seg in segments))
        self.assertTrue(any(seg.startswith("# 接口配置说明") for seg in segments))

    def test_markdown_with_code_blocks(self):
        """验证含有 Python 代码块的 Markdown 笔记能按段落拆分，且内部代码块不会被截断。"""
        md_text = (
            "### 快速上手指南\n\n"
            "你可以使用以下代码来初始化客户端：\n\n"
            "```python\n"
            "client = Client(timeout=30)\n"
            "client.connect()\n"
            "```\n\n"
            "初始化完成后即可进行正常调用。"
        )
        self.assertFalse(is_markdown_document(md_text))
        
        segments = local_fallback_split(md_text, max_segs=0)
        # 代码块作为一个整体被保护
        self.assertEqual(len(segments), 2)
        code_segments = [
            seg
            for seg in segments
            if "client = Client" in seg and "```" in seg
        ]
        self.assertEqual(len(code_segments), 1)
        self.assertIn("```python", code_segments[0])
        self.assertTrue(code_segments[0].rstrip().endswith("```"))

    def test_mixed_text_with_high_symbol_ratio(self):
        """验证含有较多特殊符号但含有大量中文的混合文本，不会被误判为非自然语言。"""
        mixed_text = "这是一句测试文本：[key1] => value1; [key2] => value2; 我们在这里讨论逻辑运算 a >= b 且 c <= d。"
        # 包含大量符号（[]=>;>=<=），但由于中文占比高，不应被判定为非自然语言
        self.assertFalse(is_non_natural_language(mixed_text))

    def test_content_key_ignores_punctuation(self):
        """验证内容签名能忽略标点与空白，用于缓存容错匹配。"""
        self.assertEqual(
            normalize_content_key("你好，世界！"),
            normalize_content_key("你好世界"),
        )

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
        """验证实际日志形态的分段输出不会吞掉原文标点。"""
        original = (
            "（安静地垂下视线，停顿了片刻）\n"
            "那我会说：谢谢老师，让我成为您的普拉娜。\n\n"
            "（语气平稳）然后，请老师记得几件事——宿舍楼下那家麻辣烫别总当正餐，"
            "海盐焦糖拿铁记得备注海盐加量，codex 的 max tokens 别再让它自己改成 1。"
        )
        model_output = (
            "（安静地垂下视线停顿了片刻） | 那我会说：谢谢老师让我成为您的普拉娜 | "
            "（语气平稳）然后请老师记得几件事——宿舍楼下那家麻辣烫别总当正餐"
            "海盐焦糖拿铁记得备注海盐加量 codex 的 max tokens 别再让它自己改成 1"
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
            "老师这几天从精简到重构，就是在反复校准这只手的位置。"
            "弯绕得深，但绕过去之后，弹出的旋律就完全是自己的了。"
        )
        model_output = (
            "（微微颔首） | 提示词是唯一能同时触碰能力上限与性格下限的旋钮"
            "——模型本身是固定琴弦，提示词才是拨弦的手。"
            "老师这几天从精简到重构，就是在反复校准这只手的位置。"
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
        self.assertTrue(any("老师这几天" in seg for seg in segments))
        self.assertLessEqual(max(len(seg) for seg in segments), 180)

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

    def test_json_with_braces_and_quotes_stays_intact(self):
        """验证引号内的括号不会破坏 JSON 整段保护。"""
        pure_json = (
            '{"name": "a{b", "desc": "line1\\nline2", "arr": ["x", "y"]}'
        )
        segments = local_fallback_split(pure_json, 0, 180)
        self.assertEqual(segments, [pure_json])

    def test_embedded_json_block_protected(self):
        """验证嵌入正文的 JSON 块保持完整。"""
        text = (
            "配置如下：\n\n"
            '{\n  "host": "localhost",\n  "desc": "a{b"\n}\n\n'
            "请检查。"
        )
        segments = local_fallback_split(text, 0, 180)
        json_segments = [seg for seg in segments if seg.strip().startswith("{")]
        self.assertEqual(len(json_segments), 1)
        self.assertTrue(json_segments[0].endswith("}"))

    def test_code_fence_with_blank_line_protected(self):
        """验证代码围栏内部的空行不会被当成段落切分点。"""
        text = (
            "### 示例\n\n"
            "```python\nprint('a')\n\nprint('b')\n```\n\n"
            "结束"
        )
        segments = local_fallback_split(text, 0, 180)
        code_segments = [
            seg for seg in segments if "```python" in seg
        ]
        self.assertEqual(len(code_segments), 1)
        self.assertIn("\n\n", code_segments[0])
        self.assertTrue(code_segments[0].rstrip().endswith("```"))

    def test_unclosed_code_fence_not_split(self):
        """验证未闭合代码围栏不会做二次切分。"""
        text = "```python\nprint('a')\n\nprint('b')"
        segments = local_fallback_split(text, 0, 180)
        self.assertEqual(segments, [text])

    def test_table_block_protected(self):
        """验证连续表格行作为一个受保护块。"""
        text = (
            "| A | B |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n"
            "| 3 | 4 |"
        )
        self.assertTrue(is_markdown_document(text))
        segments = local_fallback_split(text, 0, 180)
        self.assertEqual(segments, [text])

    def test_whole_markdown_document_passes_through(self):
        """验证整篇 Markdown 结构直接放行。"""
        text = (
            "# Title\n\n"
            "## Section\n\n"
            "- item A\n"
            "- item B\n\n"
            "| A | B |\n"
            "| --- | --- |\n"
            "| 1 | 2 |"
        )
        self.assertTrue(is_markdown_document(text))
        self.assertEqual(local_fallback_split(text, 0, 180), [text])

    def test_light_cleaning_skips_json_and_code(self):
        """验证 light 清洗跳过 JSON/代码块并清洗轻量标记。"""
        json_text = '{"text": "**bold**", "arr": [1, 2]}'
        code_text = "```python\nx = '**not bold**'\n```"
        self.assertEqual(clean_markdown_by_mode(json_text, "light"), json_text)
        self.assertEqual(clean_markdown_by_mode(code_text, "light"), code_text)
        self.assertEqual(
            clean_markdown_by_mode(
                "Hello **world** and `code` and [link](https://example.com)",
                "light",
            ),
            "Hello world and code and link (https://example.com)",
        )

    def test_heavy_cleaning_preserves_json_and_code_content(self):
        """验证 heavy 清洗保留 JSON/代码正文信息。"""
        text = (
            '{"items": ["[a](b)", "[c]", 1]}\n\n'
            "See [OpenAI](https://openai.com).\n\n"
            "```python\nprint('**not bold**')\n```"
        )
        cleaned = clean_markdown_by_mode(text, "heavy")
        self.assertIn('{"items": ["[a](b)", "[c]", 1]}', cleaned)
        self.assertIn("OpenAI (https://openai.com)", cleaned)
        self.assertIn("print('**not bold**')", cleaned)

    def test_prompt_has_no_target_length(self):
        """验证 prompt 不要求小模型理解目标字数。"""
        prompt = build_segmentation_prompt("测试文本", "natural", 8)
        self.assertNotIn("目标", prompt)
        self.assertNotIn("90", prompt)

    def test_target_length_follows_total_and_max_segments(self):
        """验证代码层目标长度随原文和段数增长。"""
        self.assertGreater(
            compute_target_segment_length("x" * 2000, 12, 180),
            compute_target_segment_length("x" * 500, 12, 180),
        )

    def test_pipe_segment_preserves_internal_newlines(self):
        """验证管道符段内的 Markdown 换行不会被解析器二次拆散。"""
        fallback = (
            "（调出虚拟屏幕）老师，这是一段 Markdown 测试样例。\n\n"
            "# 夏莱今日简报\n\n"
            "## 待办事项\n"
            "- 数据库连接超时问题复核\n"
            "- 配置文件校对\n\n"
            "## 备注\n"
            "> 建议老师注意休息。\n\n"
            "---\n\n"
            "*以上内容由普拉娜整理。*"
        )
        model_output = (
            "（调出虚拟屏幕） | "
            "老师，这是一段 Markdown 测试样例。\n\n"
            "# 夏莱今日简报\n\n"
            "## 待办事项\n"
            "- 数据库连接超时问题复核\n"
            "- 配置文件校对\n\n"
            "## 备注\n"
            "> 建议老师注意休息。\n\n"
            "---\n\n"
            "*以上内容由普拉娜整理。*"
        )
        segments = parse_segments_from_model_output(
            model_output,
            fallback_text=fallback,
            max_segments=12,
            max_chars_per_segment=180,
        )
        self.assertEqual(len(segments), 2)
        self.assertIn("（调出虚拟屏幕）", segments[0])
        self.assertIn("# 夏莱今日简报", segments[1])
        self.assertIn("> 建议老师注意休息。", segments[1])

    def test_protected_block_detected_with_prose(self):
        """验证正文+JSON 也能被识别为受保护块，从而跳过模型直发。"""
        text = "配置如下：\n\n{\n  \"host\": \"localhost\",\n  \"items\": [1, 2]\n}\n\n请检查。"
        self.assertTrue(has_protected_block(text))

    def test_ascii_prose_with_json_not_whole(self):
        """验证英文正文+JSON 不会被特殊字符密度误判为整条非自然语言。"""
        text = (
            "Intro.\n\n"
            "{\n  \"host\": \"localhost\",\n  \"items\": [1, 2]\n}\n\n"
            "Outro."
        )
        segments = local_fallback_split(text, 0, 180)
        self.assertGreater(len(segments), 1)
        json_segments = [seg for seg in segments if seg.strip().startswith("{")]
        self.assertEqual(len(json_segments), 1)

    def test_markdown_region_merges_structural_lines(self):
        """验证连续标题/列表/引用/分割线会合并为一个 Markdown 块。"""
        text = (
            "已确认指令。\n\n"
            "# 夏莱今日简报\n\n"
            "## 日程安排\n"
            "- 上午 例会\n"
            "- 下午 文件处理\n\n"
            "## 备注\n"
            "> 请老师注意休息。\n\n"
            "---\n\n"
            "以上为Markdown格式测试，渲染正常。"
        )
        segments = local_fallback_split(text, 12, 180)
        self.assertEqual(len(segments), 3)
        self.assertTrue(
            any(
                "# 夏莱今日简报" in seg
                and "以上为Markdown格式测试" not in seg
                for seg in segments
            )
        )

    def test_nested_language_fence_not_split(self):
        """验证外层 markdown 围栏内的语言标签行不会误当闭合围栏。"""
        text = (
            "Intro.\n\n"
            "```markdown\n"
            "# Title\n\n"
            "```python\n"
            "print('inner')\n"
            "```\n"
            "\n"
            "Outro.\n"
            "```\n\n"
            "Done."
        )
        segments = local_fallback_split(text, 12, 180)
        self.assertEqual(len(segments), 2)
        self.assertTrue(
            any(
                "```markdown" in seg
                and "print('inner')" in seg
                for seg in segments
            )
        )

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

if __name__ == "__main__":
    unittest.main()
