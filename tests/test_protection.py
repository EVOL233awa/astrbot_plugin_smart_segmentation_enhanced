# tests/test_protection.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from segmentation import (
    has_protected_block,
    is_markdown_document,
    is_non_natural_language,
    local_fallback_split,
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
            "# 项目周报\n\n"
            "## 日程安排\n"
            "- 上午 例会\n"
            "- 下午 文件处理\n\n"
            "## 备注\n"
            "> 请确认服务运行状态。\n\n"
            "---\n\n"
            "以上为Markdown格式测试，渲染正常。"
        )
        segments = local_fallback_split(text, 12, 180)
        self.assertEqual(len(segments), 3)
        self.assertTrue(
            any(
                "# 项目周报" in seg
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

    def test_kaomoji_leading_greater_than_escaped(self):
        """验证以 > 开头的颜文字不再被误判为 Markdown 引用而跳过转义。"""
        self.assertEqual(local_fallback_split(">_<", 0, 180), ["＞_＜"])
        self.assertEqual(
            local_fallback_split("（动作）>_< 你好。", 8, 180),
            ["（动作）", "＞_＜ 你好。"],
        )

    def test_global_max_segments_with_protected_block(self):
        """验证正文与受保护块穿插时全局最大段数仍然生效。"""
        s1 = "第一句内容足够长。" * 20
        s2 = "第二句内容足够长。" * 20
        text = s1 + "\n\n" + '{"a": 1}' + "\n\n" + s2
        segments = local_fallback_split(text, 3, 180)
        self.assertLessEqual(len(segments), 3)
        self.assertTrue(any(seg.strip().startswith("{") for seg in segments))

    def test_heavy_markdown_region_with_plain_prose_stays_merged(self):
        """验证 v1.3.0 Markdown 区域保护：含正文的重度 Markdown 仍整块放行。"""
        text = (
            "# 标题\n\n"
            "正文第一句。正文第二句。\n\n"
            "- 项目一\n"
            "- 项目二\n\n"
            "> 引用第一句。引用第二句。"
        )
        self.assertTrue(is_markdown_document(text))
        segments = local_fallback_split(text, 12, 180)
        self.assertEqual(segments, [text])


if __name__ == "__main__":
    unittest.main()
