# tests/test_core.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from segmentation import (
    is_non_natural_language,
    normalize_content_key,
)


class TestSegmentationGuardAndFallback(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
