"""配置面：类型化设置对象与插件配置的读取、清洗、迁移。

从 main.py 拆出（行为等价）。实例状态 ``config`` 与
``_smart_delay_warned`` 由 main.SmartSegmentationPlugin.__init__ 初始化，
本模块仅作类型声明。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger


@dataclass(slots=True)
class SegmentationSettings:
    # §5.7：segmenter 是发送引擎唯一权威（local/llm）；use_local_only 仅
    # 作为迁移来源键保留，加载阶段一次性映射后不再回读。
    segmenter: str = "local"
    # 留空 = 使用当前会话模型；推荐用户另行配置小模型专门分段
    provider_id: str = ""
    style: str = "natural"
    min_length: int = 10
    max_segments: int = 12
    temperature: float = 0.3
    max_tokens: int = 600
    timeout_seconds: float = 10.0
    delay_base: float = 1.3
    delay_per_char: float = 0.05
    delay_max: float = 2.0
    delay_strategy: str = "linear"
    jitter_max_seconds: float = 0.15
    smart_increase_symbols: list[str] = field(
        default_factory=lambda: ["……", "...", "...?"]
    )
    smart_increase_seconds: float = 0.25
    smart_decrease_symbols: list[str] = field(
        default_factory=lambda: ["！", "？！", "！？"]
    )
    smart_decrease_seconds: float = 0.25
    max_chars_per_segment: int = 180
    markdown_clean_mode: str = "light"
    ellipsis_pause_min_chars: int = 4
    # §5.3-5.5：短尾合并默认关闭；保护词与强制分隔符默认空列表。
    force_split_symbols: list[str] = field(default_factory=list)
    no_split_around: list[str] = field(default_factory=list)
    merge_short_tail: bool = False
    short_tail_threshold: int = 2


class SettingsMixin:
    """配置读取与净化：由 main.SmartSegmentationPlugin 组合使用。"""

    # 以下实例属性由 main.__init__ 统一初始化，此处仅作类型声明。
    config: Any
    _smart_delay_warned: set[str]

    def _get_config_value(self, key: str, default: Any) -> Any:
        """从插件配置中安全获取指定键的值，提供降级默认值。"""
        try:
            if hasattr(self.config, "get"):
                return self.config.get(key, default)
        except Exception as exc:
            logger.debug("读取智能分段配置 %s 失败: %s", key, exc)
        return default

    def _as_str_list(self, value: Any) -> list[str]:
        """把配置值清洗为非空字符串列表：转字符串、去首尾空白、去重保序。"""
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            value = [value]
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    def _split_option_kwargs(self, settings: SegmentationSettings) -> dict[str, Any]:
        """§5.3-5.5 切分选项的统一传参，保持各调用点行为一致。"""
        return {
            "force_split_symbols": settings.force_split_symbols,
            "no_split_around": settings.no_split_around,
            "merge_short_tail": settings.merge_short_tail,
            "short_tail_threshold": settings.short_tail_threshold,
        }

    def _resolve_segmenter(self) -> str:
        """§5.7：segmenter 为发送引擎唯一权威；旧键 use_local_only 一次性迁移。

        仅当 segmenter_migrated=false 时执行：use_local_only=true → local、
        false → llm；只写入 segmenter 与迁移标记，不修改 use_local_only 的
        值。写盘失败时下次加载重试；此后不再回读 use_local_only。
        """
        migrated = self._as_bool(
            self._get_config_value("segmenter_migrated", False), False
        )
        if migrated:
            segmenter = str(
                self._get_config_value("segmenter", "local") or "local"
            ).strip()
            return segmenter if segmenter in {"local", "llm"} else "local"

        legacy_use_local = self._as_bool(
            self._get_config_value("use_local_only", True), True
        )
        segmenter = "local" if legacy_use_local else "llm"
        try:
            self.config["segmenter"] = segmenter
            self.config["segmenter_migrated"] = True
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
            logger.info(
                "已将旧配置 use_local_only=%s 映射为发送引擎 segmenter=%s",
                legacy_use_local,
                segmenter,
            )
        except Exception as exc:
            logger.warning("写入 segmenter 迁移结果失败，下次加载将重试: %s", exc)
        return segmenter

    def _get_settings(self) -> SegmentationSettings | None:
        """根据插件当前配置，解析并构建类型安全的 `SegmentationSettings` 实例。"""
        enabled = self._as_bool(self._get_config_value("enabled", True), True)
        if not enabled:
            return None

        # 发送引擎：先做旧键一次性迁移，再以 segmenter 为唯一权威
        segmenter = self._resolve_segmenter()

        style = str(self._get_config_value("style", "natural") or "natural").strip()
        if style not in {"natural", "conservative", "active"}:
            style = "natural"

        # smart 延迟配置读取：同一符号同时配置在增加与减少列表时减少生效，
        # 违规项在加载阶段剔除并只警告一次；其余符号语义完全由用户自定义。
        delay_strategy = str(
            self._get_config_value("delay_strategy", "linear") or "linear"
        ).strip()
        if delay_strategy not in {"linear", "smart"}:
            delay_strategy = "linear"
        smart_increase = [
            str(symbol)
            for symbol in (
                self._get_config_value(
                    "smart_delay_increase_symbols", ["……", "...", "...?"]
                )
                or []
            )
        ]
        smart_decrease = [
            str(symbol)
            for symbol in (
                self._get_config_value(
                    "smart_delay_decrease_symbols", ["！", "？！", "！？"]
                )
                or []
            )
        ]
        increase_keys = {self._smart_symbol_key(s) for s in smart_increase}
        decrease_keys = {self._smart_symbol_key(s) for s in smart_decrease}
        conflict_keys = increase_keys & decrease_keys
        if conflict_keys:
            conflicts = sorted(
                {
                    symbol
                    for symbol in smart_increase + smart_decrease
                    if self._smart_symbol_key(symbol) in conflict_keys
                }
            )
            smart_increase = [
                symbol
                for symbol in smart_increase
                if self._smart_symbol_key(symbol) not in conflict_keys
            ]
            self._warn_smart_delay_once(
                "symbol_conflict",
                "smart 延迟符号同时配置在增加与减少列表，增加侧已忽略: %s",
                "、".join(conflicts),
            )

        return SegmentationSettings(
            segmenter=segmenter,
            provider_id=str(self._get_config_value("provider_id", "") or "").strip(),
            style=style,
            # 兜底默认值与 _conf_schema.json 保持一致，仅在该配置键缺失时生效
            min_length=max(
                0,
                self._as_int(self._get_config_value("min_length", 10), 10),
            ),
            max_segments=max(
                1,
                self._as_int(self._get_config_value("max_segments", 12), 12),
            ),
            max_chars_per_segment=max(
                20,
                self._as_int(
                    self._get_config_value("max_chars_per_segment", 180),
                    180,
                ),
            ),
            # 兜底默认值与 _conf_schema.json 保持一致；钳制防止 0/负值
            # 让省略号停顿全部独立成段。
            ellipsis_pause_min_chars=min(
                10,
                max(2, self._as_int(
                    self._get_config_value("ellipsis_pause_min_chars", 4),
                    4,
                )),
            ),
            force_split_symbols=self._as_str_list(
                self._get_config_value("force_split_symbols", [])
            ),
            no_split_around=self._as_str_list(
                self._get_config_value("no_split_around", [])
            ),
            merge_short_tail=self._as_bool(
                self._get_config_value("merge_short_tail", False),
                False,
            ),
            # 0/负值等于关闭短尾合并，交由 merge_short_tail 开关控制
            short_tail_threshold=max(
                0,
                self._as_int(self._get_config_value("short_tail_threshold", 2), 2),
            ),
            markdown_clean_mode=self._as_markdown_clean_mode(
                self._get_config_value("markdown_clean_mode", "light"),
                "light",
            ),
            temperature=self._as_float(
                self._get_config_value("temperature", 0.3),
                0.3,
            ),
            max_tokens=max(
                1,
                self._as_int(self._get_config_value("max_tokens", 600), 600),
            ),
            timeout_seconds=max(
                0.1,
                self._as_float(
                    self._get_config_value("timeout_seconds", 10.0),
                    10.0,
                ),
            ),
            delay_base=max(
                0.0,
                self._as_float(self._get_config_value("delay_base", 1.3), 1.3),
            ),
            delay_per_char=max(
                0.0,
                self._as_float(
                    self._get_config_value("delay_per_char", 0.05),
                    0.05,
                ),
            ),
            delay_max=max(
                0.0,
                self._as_float(self._get_config_value("delay_max", 2.0), 2.0),
            ),
            delay_strategy=delay_strategy,
            jitter_max_seconds=max(
                0.0,
                self._as_float(
                    self._get_config_value("jitter_max_seconds", 0.15),
                    0.15,
                ),
            ),
            smart_increase_symbols=smart_increase,
            smart_increase_seconds=max(
                0.0,
                self._as_float(
                    self._get_config_value("smart_delay_increase_seconds", 0.25),
                    0.25,
                ),
            ),
            smart_decrease_symbols=smart_decrease,
            smart_decrease_seconds=max(
                0.0,
                self._as_float(
                    self._get_config_value("smart_delay_decrease_seconds", 0.25),
                    0.25,
                ),
            ),
        )

    @staticmethod
    def _smart_symbol_key(symbol: Any) -> str:
        """smart 延迟符号的等价键：NFKC 全半角等价，省略号长度归一。"""
        normalized = unicodedata.normalize("NFKC", str(symbol)).strip()
        if normalized and set(normalized) <= {".", "…"}:
            return "..."
        return normalized

    def _warn_smart_delay_once(self, key: str, message: str, *args: Any) -> None:
        """同一类 smart 延迟配置问题只警告一次，避免每条消息刷屏。"""
        if key in self._smart_delay_warned:
            return
        self._smart_delay_warned.add(key)
        logger.warning(message, *args)

    @staticmethod
    def _as_bool(value: Any, default: bool) -> bool:
        """将输入值安全转换为布尔值，兼容字符串真伪表达。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return default

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        """将输入值转换为整型，若失败则安全降级到默认值。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        """将输入值转换为浮点数，若失败则安全降级到默认值。"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_markdown_clean_mode(value: Any, default: str) -> str:
        """将配置值安全转换为 markdown_clean_mode。"""
        normalized = str(value or default).strip().lower()
        if normalized not in {"off", "light", "heavy"}:
            return default
        return normalized
