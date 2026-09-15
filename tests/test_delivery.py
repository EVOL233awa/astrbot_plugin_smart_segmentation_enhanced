# tests/test_delivery.py
"""smart 延迟与 linear 延迟的单元测试，覆盖计划 §5.2 与补发上一段来源。"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import FakeContext  # noqa: E402

from astrbot_plugin_smart_segmentation_enhanced.main import (  # noqa: E402
    PendingFollowUp,
    SmartSegmentationPlugin,
)
from segmentation.delivery import (  # noqa: E402
    calculate_send_delay,
    calculate_symbol_adjustment,
    previous_tail_symbols,
    weighted_length,
)


class TestLinearDelayUnchanged(unittest.TestCase):
    """linear 模式为确定性公式（无抖动），且忽略 smart 参数。"""

    def test_linear_formula_deterministic(self):
        for text in ("", "a", "你好世界，今天也要加油。"):
            expected = min(3.0, 1.25 + len(text) * 0.05)
            for _ in range(20):
                with self.subTest(text=text):
                    delay = calculate_send_delay(text, 1.25, 0.05, 3.0)
                    self.assertEqual(delay, expected)

    def test_linear_ignores_previous_segment_and_smart_params(self):
        """linear 是默认策略：传上一段与符号参数不得改变输出。"""
        segment = "一段普通的回复文本"
        expected = min(5.0, 1.0 + len(segment) * 0.1)
        with_extra = calculate_send_delay(
            segment,
            1.0,
            0.1,
            5.0,
            strategy="linear",
            previous_segment="上一段……",
            smart_increase_symbols=["……"],
            smart_increase_seconds=1.0,
            smart_decrease_symbols=["！"],
            smart_decrease_seconds=1.0,
        )
        self.assertEqual(with_extra, expected)

    def test_clamped_to_zero_and_max(self):
        # linear 无抖动：空文本间隔为 0，超长文本钳到 delay_max。
        self.assertEqual(calculate_send_delay("", 0.0, 0.0, 1.0), 0.0)
        self.assertEqual(calculate_send_delay("很长的文本" * 100, 1.0, 1.0, 2.0), 2.0)

    def test_smart_jitter_configurable(self):
        """smart 抖动可配置：jitter=0 时输出确定，jitter>0 时有随机余量。"""
        previous = "让我再想想……"
        segment = "一段超过十二个字符的普通长段文本。"
        # 抖动关闭：顶格长段恒为 2.0 + 0.3 = 2.30
        for _ in range(10):
            delay = calculate_send_delay(
                segment,
                1.25,
                0.05,
                2.0,
                strategy="smart",
                previous_segment=previous,
                jitter_max_seconds=0.0,
                smart_increase_symbols=["……"],
                smart_increase_seconds=0.3,
                smart_decrease_symbols=["！"],
                smart_decrease_seconds=0.3,
            )
            self.assertEqual(delay, 2.3)
        # 默认抖动 0.15 在钳制前加入，顶格长段被钳平，修正后仍恒为 2.30
        for _ in range(10):
            delay = calculate_send_delay(
                segment,
                1.25,
                0.05,
                2.0,
                strategy="smart",
                previous_segment=previous,
                jitter_max_seconds=0.15,
                smart_increase_symbols=["……"],
                smart_increase_seconds=0.3,
                smart_decrease_seconds=0.3,
                smart_decrease_symbols=["！"],
            )
            self.assertEqual(delay, 2.3)

    def test_smart_symbol_adjustment_floats_above_delay_max(self):
        """smart 符号修正在 delay_max 之上叠加：长段「……后多等」可感知。"""
        import random

        previous = "让我再想想……"
        segment = "一段超过十二个字符的普通长段文本。"
        for seed in range(30):
            random.seed(seed)
            delay = calculate_send_delay(
                segment,
                1.25,
                0.05,
                2.0,
                strategy="smart",
                previous_segment=previous,
                smart_increase_symbols=["……"],
                smart_increase_seconds=0.3,
                smart_decrease_symbols=["！"],
                smart_decrease_seconds=0.3,
            )
            # 抖动加在钳制之前：顶格长段钳制后恒为 2.0，+0.3 修正后恒为 2.30
            self.assertEqual(delay, 2.3)

    def test_smart_floors_at_one_second(self):
        """smart 的符号修正（含多重减少）叠加后间隔不低于 1s（风控下限）。"""
        import random

        for seed in range(30):
            random.seed(seed)
            delay = calculate_send_delay(
                "好。",
                1.3,
                0.0,
                2.0,
                strategy="smart",
                previous_segment="！?!",
                smart_increase_symbols=["……"],
                smart_increase_seconds=0.3,
                smart_decrease_symbols=["！", "？！"],
                smart_decrease_seconds=0.3,
            )
            # 基础 1.3 钳制后 1.3，减少修正 -0.6 → 0.7，保底抬回 1.0
            self.assertEqual(delay, 1.0)


class TestSmartWeightedLength(unittest.TestCase):
    """smart 基础长度：非 ASCII 1.0、ASCII 0.5。"""

    def test_weighted_length(self):
        self.assertEqual(weighted_length(""), 0.0)
        self.assertEqual(weighted_length("你好"), 2.0)
        self.assertEqual(weighted_length("abcd"), 2.0)
        self.assertEqual(weighted_length("你好ab"), 3.0)

    def test_same_weighted_length_same_base_range(self):
        import random

        chinese = "十个汉字的回复文本哦"  # 10 字符 × 1.0
        ascii_text = "x" * 20  # 20 字符 × 0.5 = 10.0
        for seed in range(10):
            random.seed(seed)
            d1 = calculate_send_delay(
                chinese, 1.0, 0.1, 9.0, strategy="smart"
            )
            random.seed(seed)
            d2 = calculate_send_delay(
                ascii_text, 1.0, 0.1, 9.0, strategy="smart"
            )
            self.assertEqual(d1, d2)


class TestSmartSymbolAdjustment(unittest.TestCase):
    """上一段结尾符号串的增减修正。"""

    def test_tail_symbols_skip_trailing_whitespace(self):
        self.assertEqual(previous_tail_symbols("知道了……  \n"), "……")
        self.assertEqual(previous_tail_symbols("好了！"), "！")
        self.assertEqual(previous_tail_symbols("没事了。"), "。")
        self.assertEqual(previous_tail_symbols("普通文字"), "")
        self.assertEqual(previous_tail_symbols(None), "")

    def test_increase_and_decrease_sum(self):
        # …… 计 2 字符 × 0.5 = +1.0；！计 1 × 0.3 = -0.3
        adjustment = calculate_symbol_adjustment(
            "……", ["……"], 0.5, ["！"], 0.3
        )
        self.assertEqual(adjustment, 0.5)
        adjustment = calculate_symbol_adjustment(
            "！", ["……"], 0.5, ["！"], 0.3
        )
        self.assertEqual(adjustment, -0.3)

    def test_counts_each_occurrence(self):
        adjustment = calculate_symbol_adjustment(
            "！！", [], 0.5, ["！"], 0.3
        )
        self.assertEqual(adjustment, -0.6)

    def test_empty_tail_no_adjustment(self):
        self.assertEqual(calculate_symbol_adjustment("", ["……"], 0.5, [], 0.3), 0.0)

    def test_nfkc_normalization_matches_full_and_half_width(self):
        # NFKC 映射：单 … → ...（3 点）、…… → ......（6 点）。
        # 配置全角「！」自动覆盖半角 !；配置半角 ... 自动覆盖单 …。
        self.assertEqual(
            calculate_symbol_adjustment("好的!", ["！"], 0.5, [], 0.3), 0.5
        )
        self.assertEqual(
            calculate_symbol_adjustment("嗯…", ["..."], 0.5, [], 0.3), 0.5
        )
        self.assertEqual(
            calculate_symbol_adjustment("等一下……", ["……"], 0.5, [], 0.3), 0.5
        )
        self.assertEqual(
            calculate_symbol_adjustment("认真的?", [], 0.5, ["？"], 0.3), -0.3
        )

    def test_list_full_and_half_width_duplicates_deduped(self):
        # 列表内 ！ 与 ! 归一化等价，去重后半角 ! 结尾只计一次
        self.assertEqual(
            calculate_symbol_adjustment("好!", ["！", "!"], 0.5, [], 0.3), 0.5
        )

    def test_longest_match_consumes_combined_symbols(self):
        # 最长匹配、命中后消费：组合符号命中后组成部分不再重复计分。
        # 旧叠加语义下 ?! 结尾会同时命中 ！ 与 ？！（-0.6），现在只算 ？！。
        self.assertEqual(
            calculate_symbol_adjustment("真的？!", [], 0.5, ["！", "？！"], 0.3),
            -0.3,
        )
        # ...? 结尾只命中 ...?（+0.5），不再叠加 ...（+0.5）
        self.assertEqual(
            calculate_symbol_adjustment(
                "好吧...?", ["……", "...", "...?"], 0.5, [], 0.3
            ),
            0.5,
        )
        # 相邻的独立语气单元各自计分：独立！(-0.3) + ？！组合(-0.3) = -0.6
        self.assertEqual(
            calculate_symbol_adjustment("！?!", [], 0.5, ["！", "？！"], 0.3),
            -0.6,
        )
        self.assertEqual(
            calculate_symbol_adjustment(
                "不想说……", ["……", "...", "...?"], 0.5, ["！", "？！"], 0.3
            ),
            0.5,
        )

    def test_mixed_increase_and_decrease_on_one_tail(self):
        # 同一结尾串上增减并存：... 消费 3 字符（+0.5），剩余 ! 计 -0.3
        self.assertEqual(
            calculate_symbol_adjustment(
                "先这样……!", ["……", "...", "...?"], 0.5, ["！", "？！"], 0.3
            ),
            0.2,
        )

    def test_smart_delay_applies_adjustment_from_previous_segment(self):
        import random

        previous = "让我再想想……"
        segment = "好的。"
        delays = []
        for seed in range(30):
            random.seed(seed)
            delays.append(
                calculate_send_delay(
                    segment,
                    1.0,
                    0.0,
                    9.0,
                    strategy="smart",
                    previous_segment=previous,
                    smart_increase_symbols=["……"],
                    smart_increase_seconds=0.5,
                    smart_decrease_symbols=["！"],
                    smart_decrease_seconds=0.3,
                )
            )
        # 基础 = 1.0 + 3×0.0 = 1.0，加抖动 0~0.15，再加 …… 修正 +0.5
        for delay in delays:
            self.assertGreaterEqual(delay, 1.5)
            self.assertLessEqual(delay, 1.65)


class TestPendingFollowUpPreviousSegment(unittest.IsolatedAsyncioTestCase):
    """计划 §5.2：首条补发用已发送的首段，后续补发用循环中的上一条。"""

    def _make_plugin_and_pending(self, strategy: str = "smart"):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        pending = PendingFollowUp(
            session="s",
            segments=["第二段。", "第三段！"],
            delay_base=0.0,
            delay_per_char=0.0,
            delay_max=9.0,
            expires_at=float("inf"),
            delay_strategy=strategy,
            previous_segment="第一段……",
            smart_increase_symbols=["……"],
            smart_increase_seconds=5.0,
            smart_decrease_symbols=["！"],
            smart_decrease_seconds=5.0,
        )
        return plugin, pending

    async def test_previous_segment_sources_across_follow_ups(self):
        plugin, pending = self._make_plugin_and_pending()
        calls: list[dict] = []

        async def fake_send(session, segment):
            return True

        def fake_delay(segment, base, per_char, max_d, **kwargs):
            calls.append(kwargs)
            return 0.0

        with mock.patch.object(plugin, "_send_follow_up_with_retry", fake_send):
            with mock.patch(
                "astrbot_plugin_smart_segmentation_enhanced.plugin.follow_up.calculate_send_delay",
                side_effect=fake_delay,
            ):
                await plugin._run_follow_up_segments(pending)

        self.assertEqual(len(calls), 2)
        # 首条补发的上一段是已发送的首段
        self.assertEqual(calls[0]["previous_segment"], "第一段……")
        self.assertEqual(calls[0]["strategy"], "smart")
        # 后续补发的上一段是循环中的前一条
        self.assertEqual(calls[1]["previous_segment"], "第二段。")

    async def test_linear_pending_ignores_previous(self):
        plugin, pending = self._make_plugin_and_pending(strategy="linear")
        calls: list[dict] = []

        async def fake_send(session, segment):
            return True

        def fake_delay(segment, base, per_char, max_d, **kwargs):
            calls.append(kwargs)
            return 0.0

        with mock.patch.object(plugin, "_send_follow_up_with_retry", fake_send):
            with mock.patch(
                "astrbot_plugin_smart_segmentation_enhanced.plugin.follow_up.calculate_send_delay",
                side_effect=fake_delay,
            ):
                await plugin._run_follow_up_segments(pending)

        self.assertEqual(calls[0]["previous_segment"], "第一段……")
        self.assertEqual(calls[0]["strategy"], "linear")


class TestSmartDelayConfigSanitize(unittest.TestCase):
    """配置加载阶段的净化：两侧冲突清洗与一次性警告。"""

    def _make_plugin(self, config: dict):
        return SmartSegmentationPlugin(FakeContext(), config)

    def test_user_symbols_pass_through_untouched(self):
        """符号语义完全由用户决定：增加列表不做任何内置排除。"""
        plugin = self._make_plugin(
            {
                "enabled": True,
                "delay_strategy": "smart",
                "smart_delay_increase_symbols": ["。", "……"],
            }
        )
        settings = plugin._get_settings()
        self.assertEqual(settings.smart_increase_symbols, ["。", "……"])

    def test_conflict_keeps_decrease(self):
        plugin = self._make_plugin(
            {
                "enabled": True,
                "delay_strategy": "smart",
                "smart_delay_increase_symbols": ["……", "？"],
                "smart_delay_decrease_symbols": ["？", "！"],
            }
        )
        settings = plugin._get_settings()
        self.assertNotIn("？", settings.smart_increase_symbols)
        self.assertIn("？", settings.smart_decrease_symbols)
        self.assertIn("……", settings.smart_increase_symbols)

    def test_invalid_strategy_falls_back_to_linear(self):
        plugin = self._make_plugin(
            {"enabled": True, "delay_strategy": "turbo"}
        )
        settings = plugin._get_settings()
        self.assertEqual(settings.delay_strategy, "linear")

    def test_warned_only_once(self):
        plugin = self._make_plugin(
            {
                "enabled": True,
                "delay_strategy": "smart",
                "smart_delay_increase_symbols": ["……", "？"],
                "smart_delay_decrease_symbols": ["？"],
            }
        )
        plugin._get_settings()
        plugin._get_settings()
        self.assertEqual(len(plugin._smart_delay_warned), 1)


class TestSmartDelaySchema(unittest.TestCase):
    """schema 层：策略键与 4 个 smart 专属项的条件显示。"""

    def test_schema_keys_and_conditions(self):
        import json

        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "_conf_schema.json",
        )
        with open(schema_path, encoding="utf-8") as handle:
            schema = json.load(handle)
        strategy = schema["delay_strategy"]
        self.assertEqual(strategy["type"], "string")
        self.assertEqual(strategy["default"], "linear")
        self.assertEqual(strategy["options"], ["linear", "smart"])
        self.assertEqual(schema["delay_base"]["default"], 1.3)
        jitter = schema.get("jitter_max_seconds")
        self.assertIsInstance(jitter, dict)
        self.assertEqual(jitter.get("type"), "float")
        self.assertEqual(jitter.get("default"), 0.15)
        self.assertEqual(jitter.get("condition"), {"delay_strategy": "smart"})

        expected = {
            "smart_delay_increase_symbols": (["……", "...", "...?"], None),
            "smart_delay_increase_seconds": (None, 0.25),
            "smart_delay_decrease_symbols": (["！", "？！", "！？"], None),
            "smart_delay_decrease_seconds": (None, 0.25),
        }
        for key, (symbols_default, seconds_default) in expected.items():
            entry = schema.get(key)
            self.assertIsInstance(entry, dict, key)
            self.assertEqual(
                entry.get("condition"), {"delay_strategy": "smart"}, key
            )
            if symbols_default is not None:
                self.assertEqual(entry.get("default"), symbols_default, key)
            if seconds_default is not None:
                self.assertEqual(entry.get("default"), seconds_default, key)


if __name__ == "__main__":
    unittest.main()
