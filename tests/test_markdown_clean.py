# tests/test_markdown_clean.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from segmentation import (
    clean_markdown_by_mode,
)


class TestSegmentationGuardAndFallback(unittest.TestCase):
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

    def test_heavy_cleaning_removes_markdown_markers(self):
        """验证 heavy 模式无论是否走 mdit_plain 都输出纯文本。"""
        self.assertEqual(
            clean_markdown_by_mode(
                "**加粗** 和 `代码` 以及 [链接](https://example.com)",
                "heavy",
            ),
            "加粗 和 代码 以及 链接 (https://example.com)",
        )




class TestInlineMarkerSafety(unittest.TestCase):
    """普通文本里的数学符号不被当成 Markdown 强调标记。"""

    def test_multiplication_stars_kept(self):
        self.assertEqual(
            clean_markdown_by_mode("价格是 5 * 3 = 15 元。", "light"),
            "价格是 5 * 3 = 15 元。",
        )

    def test_numeric_range_tildes_kept(self):
        self.assertEqual(
            clean_markdown_by_mode("温度 10~~20 度都算正常。", "light"),
            "温度 10~~20 度都算正常。",
        )

    def test_emphasis_still_cleaned(self):
        self.assertEqual(
            clean_markdown_by_mode("这是 *斜体文字* 和 **粗体**。", "light"),
            "这是 斜体文字 和 粗体。",
        )


class TestThematicBreakFallback(unittest.TestCase):
    """分隔线符号间夹空格时，heavy 清洗也要整行清掉（无论有无 mdit）。"""

    def test_spaced_thematic_breaks_removed(self):
        for divider in ("* * *", "_ _ _", "- - -", "***", "___"):
            self.assertEqual(
                clean_markdown_by_mode(divider, "heavy"), "", divider
            )

    def test_spaced_thematic_break_line_inside_paragraph(self):
        cleaned = clean_markdown_by_mode("上一段。\n* * *\n下一段。", "heavy")
        self.assertEqual(cleaned.replace("\n", "").strip(), "上一段。下一段。")

    def test_light_keeps_thematic_break(self):
        self.assertEqual(clean_markdown_by_mode("* * *", "light"), "* * *")

    def test_list_item_still_cleaned(self):
        self.assertEqual(clean_markdown_by_mode("* 项目一", "heavy"), "项目一")


if __name__ == "__main__":
    unittest.main()
