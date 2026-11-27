"""v1.5.8 审计修复的回归用例。

按类名与用例名对应审计编号，锁住修复后的用户可见行为：
- 协议与 thinking 清洗：B10-1、A10-1；
- 平台补发与配置降级：B1-1、B6-2；
- 强制切分与行内原子：B4-1、B4-2、B4-3、BC-3；
- 边界与预处理：B9-1、B9-2、B9-3、B13-2、A5-1；
- 段数与长度优先关系：A12-1（仅日志与文档）。
"""

import asyncio
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import (  # noqa: E402
    FakeContext,
    FakeLLMResponse,
)

import astrbot_plugin_smart_segmentation_enhanced.segmentation.markdown_clean as markdown_clean  # noqa: E402
from astrbot_plugin_smart_segmentation_enhanced.main import (  # noqa: E402
    _NO_PROACTIVE_SEND_PLATFORMS,
    SmartSegmentationPlugin,
)
from astrbot_plugin_smart_segmentation_enhanced.plugin.orchestrator import (  # noqa: E402
    _pipe_is_force_symbol,
)
from astrbot_plugin_smart_segmentation_enhanced.plugin.prepared_store import (  # noqa: E402
    _merge_non_alnum_edges,
)
from astrbot_plugin_smart_segmentation_enhanced.plugin.settings import (  # noqa: E402
    SegmentationSettings,
    SettingsMixin,
)
from astrbot_plugin_smart_segmentation_enhanced.segmentation import (  # noqa: E402
    align_prepared_in_outbound,
    clean_heavy_markdown,
    clean_light_markdown,
    local_fallback_split,
    split_protected_blocks,
    strip_thinking_content,
)
from astrbot_plugin_smart_segmentation_enhanced.segmentation.boundary import (  # noqa: E402
    _CLOSING_TRAIL_CHARS,
    _ends_at_hard_sentence,
    _ends_with_kaomoji,
    _force_split_by_symbols,
    _split_plain_text,
    split_segments_at_bracket_boundaries,
)
from astrbot_plugin_smart_segmentation_enhanced.segmentation.packing import (  # noqa: E402
    attach_symbol_only_pieces,
    force_split_outside_inline_atoms,
)


class TestProtocolArtifactsFenceAware(unittest.TestCase):
    """B10-1：代码围栏内原样保留，未闭合协议标签不吞后文。"""

    def test_fenced_tool_call_is_kept(self):
        text = (
            "示例：\n"
            "```python\n"
            '<tool_call>{"name": "weather"}</tool_call>\n'
            "```\n"
            "以上是格式。"
        )
        self.assertEqual(strip_thinking_content(text), text)

    def test_unclosed_tool_call_keeps_following_text(self):
        text = '先说明 <tool_call>{"a": 1} 后面还有正文。'
        cleaned = strip_thinking_content(text)
        self.assertIn("后面还有正文", cleaned)
        self.assertIn("<tool_call>", cleaned)

    def test_paired_tool_call_outside_fence_removed(self):
        text = '开头<tool_call>{"a": 1}</tool_call>结尾正文。'
        self.assertEqual(strip_thinking_content(text), "开头结尾正文。")


class TestPendingLockStale(unittest.IsolatedAsyncioTestCase):
    """B3-1：首段没发出时锁不能让后续回复等满 300s。"""

    async def test_stale_pending_lock_released_without_dropping_queue(self):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        settings = plugin._get_settings()
        pending_id = plugin._register_pending_follow_up(
            session="s",
            segments=["第二段内容足够长，用于测试。", "第三段内容足够长。"],
            settings=settings,
            first_segment="第一段。",
        )
        pending = plugin._pending_follow_ups[pending_id]
        lock = asyncio.Lock()
        await lock.acquire()
        pending.lock = lock
        pending.registered_at = time.monotonic() - 30.0

        plugin._prune_expired_pending_follow_ups()

        self.assertFalse(lock.locked())
        self.assertIsNone(pending.lock)
        self.assertIn(pending_id, plugin._pending_follow_ups)
        self.assertEqual(len(pending.segments), 2)


class TestPipeIntegrityFallback(unittest.IsolatedAsyncioTestCase):
    """A4-1：占位符被切开时整条本地兜底，原文的 | 不丢。"""

    async def test_broken_placeholder_falls_back_to_local(self):
        plugin = SmartSegmentationPlugin(
            FakeContext(),
            {
                "enabled": True,
                "segmenter": "llm",
                "segmenter_migrated": True,
                "provider_id": "fake",
                "markdown_clean_mode": "off",
                "min_length": 1,
                "max_segments": 12,
            },
        )

        async def fake_llm_generate(**kwargs):
            return FakeLLMResponse("前半句RAW | PIPE后半句，还有一些说明文字。")

        plugin.context.llm_generate = fake_llm_generate
        segments = await plugin._segment_text(
            "前半句|后半句，还有一些说明文字。",
            provider_id="fake",
            settings=plugin._get_settings(),
        )
        joined = "".join(segments)
        self.assertIn("|", joined)
        self.assertNotIn("RAW", joined)
        self.assertNotIn("PIPE", joined)


class TestPreparedNonAlnumEdges(unittest.TestCase):
    """A5-1：非字母数字的头尾插入不能因内容签名相同被丢掉。"""

    def test_merge_suffix_emoji(self):
        prepared = "第一段内容足够长。第二段内容也足够长。"
        segments = ["第一段内容足够长。", "第二段内容也足够长。"]
        merged = _merge_non_alnum_edges(segments, prepared + "😊", prepared)
        self.assertIsNotNone(merged)
        self.assertEqual(merged[-1], "第二段内容也足够长。😊")

    def test_merge_prefix_emoji(self):
        prepared = "第一段内容足够长。第二段内容也足够长。"
        segments = ["第一段内容足够长。", "第二段内容也足够长。"]
        merged = _merge_non_alnum_edges(segments, "😊" + prepared, prepared)
        self.assertIsNotNone(merged)
        self.assertEqual(merged[0], "😊第一段内容足够长。")

    def test_align_puts_leading_emoji_in_head(self):
        prepared = "第一段内容足够长。第二段内容也足够长。"
        aligned = align_prepared_in_outbound("😊" + prepared, prepared)
        self.assertIsNotNone(aligned)
        assert aligned is not None
        self.assertEqual(aligned[0], "😊")


class TestHeavyKaomoji(unittest.TestCase):
    """B13-1：heavy 模式经过 markdown 渲染后，颜文字行首的 > 仍保留。"""

    class _QuoteStrippingRenderer:
        """模拟 markdown-it 的 CommonMark 行为：只删行首引用标记。"""

        def render(self, text: str) -> str:
            return re.sub(r"(?m)^> ", "", text)

    def setUp(self):
        self._original = (
            markdown_clean._MDIT_AVAILABLE,
            markdown_clean._MDIT_INSTANCE,
        )
        markdown_clean._MDIT_AVAILABLE = True
        markdown_clean._MDIT_INSTANCE = self._QuoteStrippingRenderer()

    def tearDown(self):
        (
            markdown_clean._MDIT_AVAILABLE,
            markdown_clean._MDIT_INSTANCE,
        ) = self._original

    def test_kaomoji_survives_render(self):
        self.assertEqual(
            clean_heavy_markdown(">_< 今天又加班了"),
            ">_< 今天又加班了",
        )
        self.assertEqual(clean_heavy_markdown(">< 无语"), ">< 无语")

    def test_normal_quote_still_cleaned(self):
        self.assertEqual(clean_heavy_markdown("> 正常引用"), "正常引用")


class TestMultiplicationStars(unittest.TestCase):
    """A13-1：词内星号是乘法/标识符，不能当斜体删掉。"""

    def test_multiplication_kept(self):
        text = "尺寸 2*3 和 4*5 两种规格"
        self.assertEqual(clean_light_markdown(text), text)
        self.assertEqual(clean_light_markdown("2*3*4"), "2*3*4")

    def test_emphasis_still_stripped(self):
        self.assertEqual(clean_light_markdown("这是*强调*文字"), "这是强调文字")
        self.assertEqual(clean_light_markdown("*斜体*"), "斜体")


class TestQuotedFence(unittest.TestCase):
    """B16-1/B16-2：引用围栏的闭围栏不会把后续正文吞进代码块。"""

    def test_bare_close_after_quoted_fence(self):
        text = "> ```python\n> print(1)\n```\n这段正文应该单独成段，不能被吞掉。"
        chunks = split_protected_blocks(text, merge_adjacent_markdown=False)
        self.assertIn(
            ("text", "这段正文应该单独成段，不能被吞掉。"),
            chunks,
        )

    def test_quote_text_before_fence(self):
        text = (
            "> 示例：```python\n"
            "> print(1)\n"
            "> ```\n"
            "这段正文应该单独成段，不能被吞掉。"
        )
        chunks = split_protected_blocks(text, merge_adjacent_markdown=False)
        self.assertTrue(
            any(kind == "text" and "这段正文" in chunk for kind, chunk in chunks)
        )
        self.assertFalse(
            any(kind == "markdown" and "这段正文" in chunk for kind, chunk in chunks)
        )

    def test_normal_quote_is_still_one_block(self):
        text = "> 第一行引用\n> 第二行引用\n正文在这里。"
        chunks = split_protected_blocks(text, merge_adjacent_markdown=False)
        self.assertTrue(any(kind == "text" and "正文在这里" in chunk for kind, chunk in chunks))


class TestThinkingConfig(unittest.TestCase):
    """A10-1：未闭合不删正文；成对标签由 strip_thinking_tags 控制。"""

    UNCLOSED = "注意 <thinking> 之后不要加空格，后面还有正文。"
    PAIRED = "模板 <thinking>先列提纲</thinking> 然后展开。"

    def test_unclosed_tag_keeps_following_text(self):
        self.assertEqual(strip_thinking_content(self.UNCLOSED), self.UNCLOSED)

    def test_paired_removed_by_default(self):
        self.assertEqual(strip_thinking_content(self.PAIRED), "模板  然后展开。")

    def test_paired_kept_when_disabled(self):
        self.assertEqual(
            strip_thinking_content(self.PAIRED, remove_thinking=False),
            self.PAIRED,
        )

    def test_fenced_block_kept_in_both_modes(self):
        text = "```xml\n<thinking>推理</thinking>\n```"
        self.assertEqual(strip_thinking_content(text), text)
        self.assertEqual(strip_thinking_content(text, remove_thinking=False), text)

    def test_local_path_honors_switch(self):
        segments = local_fallback_split(
            self.PAIRED,
            12,
            180,
            remove_thinking=False,
        )
        self.assertIn("先列提纲", "".join(segments))


class TestThinkingSetting(unittest.TestCase):
    """配置项 strip_thinking_tags 默认开启，可关闭。"""

    class _Plugin(SettingsMixin):
        def __init__(self, config):
            self.config = config
            self._smart_delay_warned: set[str] = set()

    def _settings(self, **overrides):
        config = {"enabled": True, "segmenter_migrated": True, **overrides}
        settings = self._Plugin(config)._get_settings()
        assert settings is not None
        return settings

    def test_default_enabled(self):
        self.assertTrue(self._settings().strip_thinking_tags)

    def test_can_disable(self):
        self.assertFalse(
            self._settings(strip_thinking_tags=False).strip_thinking_tags
        )

    def test_split_options_carry_switch(self):
        plugin = self._Plugin(
            {
                "enabled": True,
                "segmenter_migrated": True,
                "strip_thinking_tags": False,
            }
        )
        settings = plugin._get_settings()
        assert settings is not None
        self.assertFalse(
            plugin._split_option_kwargs(settings)["remove_thinking"]
        )

    def test_qq_official_follow_up_default_off(self):
        self.assertFalse(self._settings().qq_official_follow_up)

    def test_qq_official_follow_up_can_enable(self):
        self.assertTrue(
            self._settings(qq_official_follow_up=True).qq_official_follow_up
        )


class TestFollowUpPlatforms(unittest.TestCase):
    """B1-1：QQ 官方补发由配置控制；其他受限平台始终整条发送。"""

    class _Event:
        def __init__(self, name: str):
            self._name = name

        def get_platform_name(self) -> str:
            return self._name

    def test_qq_official_default_off(self):
        settings = SegmentationSettings()
        self.assertFalse(settings.qq_official_follow_up)
        self.assertFalse(
            SmartSegmentationPlugin._platform_supports_follow_up(
                self._Event("qq_official"), settings
            )
        )

    def test_qq_official_can_enable(self):
        settings = SegmentationSettings(qq_official_follow_up=True)
        self.assertTrue(
            SmartSegmentationPlugin._platform_supports_follow_up(
                self._Event("qq_official"), settings
            )
        )

    def test_webhook_always_off(self):
        settings = SegmentationSettings(qq_official_follow_up=True)
        self.assertFalse(
            SmartSegmentationPlugin._platform_supports_follow_up(
                self._Event("qq_official_webhook"), settings
            )
        )

    def test_other_platform_allowed(self):
        settings = SegmentationSettings()
        self.assertTrue(
            SmartSegmentationPlugin._platform_supports_follow_up(
                self._Event("aiocqhttp"), settings
            )
        )

    def test_limited_platforms_always_blacklisted(self):
        for name in (
            "qq_official_webhook",
            "weixin_official_account",
            "dingtalk",
        ):
            self.assertIn(name, _NO_PROACTIVE_SEND_PLATFORMS)


class TestAsIntOverflow(unittest.TestCase):
    """B6-2：inf 不能在配置解析阶段抛出 OverflowError。"""

    def test_inf_falls_back_to_default(self):
        self.assertEqual(SettingsMixin._as_int(float("inf"), 10), 10)
        self.assertEqual(SettingsMixin._as_int(float("-inf"), 10), 10)


class TestForceSymbolGate(unittest.TestCase):
    """B4-3：配置全角 ｜ 时，ASCII | 也要走后置强制切分。"""

    def test_fullwidth_pipe_counts(self):
        self.assertTrue(_pipe_is_force_symbol(["｜"]))
        self.assertTrue(_pipe_is_force_symbol(["|"]))
        self.assertFalse(_pipe_is_force_symbol(["！"]))


class TestClosingTrailChars(unittest.TestCase):
    """B9-1：全角右引号与 ] 也算句末收尾符。"""

    def test_chars_present(self):
        for char in ("”", "’", "]"):
            self.assertIn(char, _CLOSING_TRAIL_CHARS)

    def test_fullwidth_quote_ends_sentence(self):
        self.assertTrue(_ends_at_hard_sentence("他说：“好！”"))

    def test_force_split_absorbs_closing_quote(self):
        pieces = _force_split_by_symbols("他说：“好！”然后走了。", ["！"])
        self.assertEqual(pieces[0], "他说：“好！”")

    def test_action_after_quote_is_independent(self):
        parts = _split_plain_text("他说：“好。”（点头）")
        self.assertIn("（点头）", parts)


class TestWhitespaceBetweenBrackets(unittest.TestCase):
    """B9-2：括号之间的纯空白不能丢，避免 (a)(b) 粘连。"""

    def test_ascii_space(self):
        text = "version (beta) (final) is out now."
        segments = split_segments_at_bracket_boundaries([text], max_segments=0)
        self.assertEqual("".join(segments), text)

    def test_chinese_space(self):
        text = "我看了（第一章） （第二章）两本书。"
        segments = split_segments_at_bracket_boundaries([text], max_segments=0)
        self.assertEqual("".join(segments), text)


class TestKaomojiOrder(unittest.TestCase):
    """B9-3：ㅋ/ㅠ 是字母，但也要能命中颜文字集合。"""

    def test_korean_tail_chars(self):
        self.assertTrue(_ends_with_kaomoji("怎么办啦ㅋㅋ"))
        self.assertTrue(_ends_with_kaomoji("哭ㅠㅠ"))

    def test_plain_letters_still_stop(self):
        self.assertFalse(_ends_with_kaomoji("怎么"))


class TestTableWithoutOuterPipes(unittest.TestCase):
    """B13-2：两侧都省略竖线的 GFM 表格也要清掉分隔行。"""

    def test_delimiter_removed(self):
        text = "项目 | 状态\n--- | ---\nA | 完成"
        cleaned = clean_heavy_markdown(text)
        self.assertNotIn("--- | ---", cleaned)
        self.assertIn("项目 | 状态", cleaned)
        self.assertIn("A | 完成", cleaned)


class TestInlineAtomsAcrossEngines(unittest.TestCase):
    """B4-1/BC-3：local 引擎与共享助手都不能切开行内代码。"""

    TEXT = "先运行 `ls|grep` 看看输出内容。"

    def test_local_engine_keeps_inline_code(self):
        segments = local_fallback_split(
            self.TEXT,
            12,
            180,
            force_split_symbols=["|"],
        )
        self.assertEqual("".join(segments), self.TEXT)
        self.assertTrue(any("`ls|grep`" in seg for seg in segments))

    def test_shared_helper_keeps_inline_code(self):
        self.assertEqual(
            force_split_outside_inline_atoms([self.TEXT], ["|"]),
            [self.TEXT],
        )

    def test_symbol_only_piece_is_attached(self):
        merged = attach_symbol_only_pieces(["前半句|", "|", "后半句"], ["|"])
        self.assertFalse(any(seg.strip() == "|" for seg in merged))


if __name__ == "__main__":
    unittest.main()
