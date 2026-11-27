# tests/test_orchestrator.py
"""LLM 编排路径：管道符掩码下的强制切分与锚点路径文本段补切。"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import FakeContext  # noqa: E402

from astrbot_plugin_smart_segmentation_enhanced.main import (  # noqa: E402
    SmartSegmentationPlugin,
)


class EchoLLMContext(FakeContext):
    """把提示词末尾的输入原样作为模型输出回显。"""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def llm_generate(self, **kwargs):
        prompt = kwargs.get("prompt", "")
        self.prompts.append(prompt)
        return SimpleNamespace(
            completion_text=prompt.rsplit("输入：\n", 1)[-1]
        )


def _make_plugin(context, **overrides):
    config = {
        "enabled": True,
        "segmenter": "llm",
        "segmenter_migrated": True,
    }
    config.update(overrides)
    return SmartSegmentationPlugin(context, config)


def _norm(text: str) -> str:
    return "".join(text.split())


class TestForceSplitWithMaskedPipe(unittest.IsolatedAsyncioTestCase):
    async def test_pipe_force_symbol_applies_in_llm_path(self):
        """原文 | 被掩码后，强制切分符号 | 仍要在掩码文本上生效。"""
        text = "前半|后半。"
        plugin = _make_plugin(EchoLLMContext(), force_split_symbols=["|"])
        segments = await plugin._segment_text(
            text,
            provider_id="p",
            settings=plugin._get_settings(),
        )
        self.assertEqual(segments, ["前半|", "后半。"])


class TestAnchorPathRepair(unittest.IsolatedAsyncioTestCase):
    async def test_action_bracket_split_after_anchor_expand(self):
        """锚点还原后的普通文本段补齐动作括号切分，与普通路径同口径。"""
        text = (
            "先说结论。\n\n"
            "```python\nprint(1)\n```\n\n"
            "（点头）然后继续说明一些足够长的内容。"
        )
        plugin = _make_plugin(EchoLLMContext())
        segments = await plugin._segment_text(
            text,
            provider_id="p",
            settings=plugin._get_settings(),
        )
        self.assertIn("（点头）", segments)
        self.assertEqual(_norm("".join(segments)), _norm(text))
        self.assertTrue(any("```python" in seg for seg in segments))


if __name__ == "__main__":
    unittest.main()
