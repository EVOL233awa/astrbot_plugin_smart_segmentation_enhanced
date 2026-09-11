# tests/test_boundary.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from segmentation import (
    local_fallback_split,
)


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
