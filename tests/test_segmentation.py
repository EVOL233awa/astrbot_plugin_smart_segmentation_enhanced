# test_segmentation.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from segmentation import (
    is_non_natural_language,
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
        # 即使包含大括号，也应当判定为 Markdown 笔记而非纯非自然语言
        self.assertTrue(is_markdown_heavy(md_text))
        
        segments = local_fallback_split(md_text, max_segs=0)
        # 预期切分为 4 个自然段落
        self.assertEqual(len(segments), 4)
        self.assertEqual(segments[0], "# 接口配置说明")
        self.assertTrue(segments[2].startswith("{"))

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
        self.assertTrue(is_markdown_heavy(md_text))
        
        segments = local_fallback_split(md_text, max_segs=0)
        # 预期切分为 4 个自然段落，代码块作为一个整体被保护
        self.assertEqual(len(segments), 4)
        self.assertIn("client = Client", segments[2])
        self.assertTrue(segments[2].endswith("```"))

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
        self.assertEqual(len(segments), 4)
        self.assertIn("（微微颔首）", segments)
        self.assertTrue(any("老师这几天" in seg for seg in segments))
        self.assertFalse(
            any(
                "提示词是" in seg and "老师这几天" in seg
                for seg in segments
            )
        )

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

if __name__ == "__main__":
    unittest.main()
