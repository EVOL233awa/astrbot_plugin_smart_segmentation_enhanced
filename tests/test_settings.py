# tests/test_settings.py
"""segmenter 一次性迁移的单元测试（计划 §5.7，对应 plugin/settings.py）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import FakeContext  # noqa: E402

from astrbot_plugin_smart_segmentation_enhanced.main import (  # noqa: E402
    SmartSegmentationPlugin,
)


class SaveableConfig(dict):
    """带 save_config 的假配置，模拟 AstrBotConfig 的写盘行为。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self):
        self.saved += 1


class TestSegmenterMigration(unittest.TestCase):
    """旧键 use_local_only → segmenter 的一次性映射与权威读取。"""

    def test_legacy_true_maps_to_local(self):
        config = SaveableConfig({"enabled": True, "use_local_only": True})
        settings = SmartSegmentationPlugin(FakeContext(), config)._get_settings()
        self.assertEqual(settings.segmenter, "local")
        # 只写 segmenter 与迁移标记，不修改 use_local_only
        self.assertEqual(config["segmenter"], "local")
        self.assertIs(config["segmenter_migrated"], True)
        self.assertIs(config["use_local_only"], True)
        self.assertEqual(config.saved, 1)

    def test_legacy_false_maps_to_llm(self):
        config = SaveableConfig({"enabled": True, "use_local_only": False})
        settings = SmartSegmentationPlugin(FakeContext(), config)._get_settings()
        self.assertEqual(settings.segmenter, "llm")
        self.assertEqual(config["segmenter"], "llm")
        self.assertIs(config["segmenter_migrated"], True)
        self.assertIs(config["use_local_only"], False)

    def test_missing_legacy_key_uses_schema_default(self):
        """新装/键缺失：use_local_only 按 schema 默认 true → local。"""
        config = SaveableConfig({"enabled": True})
        settings = SmartSegmentationPlugin(FakeContext(), config)._get_settings()
        self.assertEqual(settings.segmenter, "local")

    def test_migration_runs_once(self):
        """迁移标记为真后直接读 segmenter，不再写盘。"""
        config = SaveableConfig(
            {
                "enabled": True,
                "segmenter": "llm",
                "segmenter_migrated": True,
                "use_local_only": True,
            }
        )
        settings = SmartSegmentationPlugin(FakeContext(), config)._get_settings()
        self.assertEqual(settings.segmenter, "llm")
        self.assertEqual(config.saved, 0)

    def test_invalid_segmenter_falls_back_to_local(self):
        config = SaveableConfig(
            {
                "enabled": True,
                "segmenter": "bogus",
                "segmenter_migrated": True,
            }
        )
        settings = SmartSegmentationPlugin(FakeContext(), config)._get_settings()
        self.assertEqual(settings.segmenter, "local")

    def test_migration_persists_across_reload(self):
        """迁移写盘后重载：segmenter 为唯一权威，不再看旧键。"""
        config = SaveableConfig({"enabled": True, "use_local_only": True})
        plugin = SmartSegmentationPlugin(FakeContext(), config)
        self.assertEqual(plugin._get_settings().segmenter, "local")
        # 模拟用户随后把 segmenter 改为 llm（旧键仍残留 true）
        config["segmenter"] = "llm"
        self.assertEqual(plugin._get_settings().segmenter, "llm")


class TestSmartDelaySymbolConflict(unittest.TestCase):
    """P3 回归：全半角/省略号写法不同的同一符号也能识别冲突。"""

    def test_nfkc_equivalent_conflict_removed_from_increase(self):
        config = SaveableConfig(
            {
                "enabled": True,
                "segmenter_migrated": True,
                "smart_delay_increase_symbols": ["！", "……", "？"],
                "smart_delay_decrease_symbols": ["!", "..."],
            }
        )
        settings = SmartSegmentationPlugin(FakeContext(), config)._get_settings()
        # 减少侧生效：增加侧的「！」「……」被剔除
        self.assertEqual(settings.smart_increase_symbols, ["？"])
        self.assertEqual(settings.smart_decrease_symbols, ["!", "..."])


if __name__ == "__main__":
    unittest.main()
