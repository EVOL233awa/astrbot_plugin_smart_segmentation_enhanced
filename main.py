"""AstrBot 智能分段插件。"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult
from astrbot.api.event import filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star

# 导入多段拼接自愈与本地切分兜底依赖
from .segmentation import (
    build_anchor_masking,
    build_segmentation_prompt,
    calculate_send_delay,
    hash_normalized_text,
    is_action_only_text,
    parse_raw_model_segments,
    parse_segments_from_model_output,
    segment_from_anchored_output,
    strip_thinking_content,
    clean_markdown_by_mode,
    normalize_response_text_for_key,
    normalize_content_key,
    align_prepared_in_outbound,
    mask_pipe_in_text,
    restore_pipe_in_segments,
    # 导入非自然语言检测与本地逻辑切分工具
    apply_force_split_to_segments,
    apply_no_split_boundaries,
    is_non_natural_language,
    is_markdown_document,
    has_protected_block,
    local_fallback_split,
    merge_short_tail_segment,
    safe_escape_segments,
    stabilize_segment_lengths,
)

_PREPARED_SEGMENT_TTL_SECONDS = 60.0
_PENDING_FOLLOW_UP_TTL_SECONDS = 60.0
_PENDING_REAPER_INTERVAL_SECONDS = 5.0
_PENDING_EXTRA_KEY = "smart_segmentation_pending_id"
_PREPARED_EXTRA_KEY = "smart_segmentation_prepared_segments"


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


@dataclass(slots=True)
class PreparedSegments:
    segments: list[str]
    raw_norm_text: str  # 缓存此分段对应的原始文本规范化表示，用作自愈拼合时的唯一基准
    expires_at: float
    match_key: str = ""


@dataclass(slots=True)
class PendingFollowUp:
    session: str
    segments: list[str]
    delay_base: float
    delay_per_char: float
    delay_max: float
    expires_at: float
    lock: asyncio.Lock | None = None
    # 延迟策略与 smart 参数在注册时快照：补发是后台任务，
    # 不受中途配置变更影响；previous_segment 是已发送的上一段，
    # smart 模式按其结尾符号修正与下一段之间的间隔。
    delay_strategy: str = "linear"
    previous_segment: str | None = None
    jitter_max_seconds: float = 0.15
    smart_increase_symbols: list[str] = field(default_factory=list)
    smart_increase_seconds: float = 0.25
    smart_decrease_symbols: list[str] = field(default_factory=list)
    smart_decrease_seconds: float = 0.25


class SmartSegmentationPlugin(Star):
    """使用 LLM 对 AstrBot 主回复进行自然分段。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        """初始化智能分段插件并设置初始缓存状态。"""
        super().__init__(context)
        self.config = config if config is not None else {}
        self._prepared_segments: dict[tuple[str, str], PreparedSegments] = {}
        self._pending_follow_ups: dict[str, PendingFollowUp] = {}
        self._active_follow_up_tasks: set[asyncio.Task[Any]] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._lock_reaper_task: asyncio.Task[Any] | None = None
        self._smart_delay_warned: set[str] = set()

    @filter.on_llm_response()
    async def on_llm_response(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """在主 LLM 返回后预先计算智能分段。"""
        settings = self._get_settings()
        if settings is None:
            return

        text = clean_markdown_by_mode(
            self._extract_response_plain_text(response),
            settings.markdown_clean_mode,
        )
        if not self._should_segment_text(text, settings):
            return

        # 仅在 local 引擎、纯 JSON/代码、整篇 Markdown 时跳过分段模型；
        # 含受保护块的普通消息走锚点路径（§5.6），不再整条降级本地。
        if (
            settings.segmenter == "local"
            or is_non_natural_language(text)
            or is_markdown_document(text)
        ):
            logger.info(
                "智能分段已启用本地/受保护块直发模式，跳过分段模型调用"
            )
            segments = local_fallback_split(
                text,
                settings.max_segments,
                settings.max_chars_per_segment,
                pause_min_chars=settings.ellipsis_pause_min_chars,
                **self._split_option_kwargs(settings),
            )
        else:
            provider_id = await self._resolve_provider_id(event, settings)
            if not provider_id:
                logger.warning("智能分段未找到可用 provider_id，跳过本次分段")
                self._store_skip_segment(event.unified_msg_origin, text)
                return

            try:
                segments = await asyncio.wait_for(
                    self._segment_text(
                        text,
                        provider_id=provider_id,
                        settings=settings,
                    ),
                    timeout=settings.timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "智能分段 LLM 调用超时（> %.2fs），已跳过本次回复",
                    settings.timeout_seconds,
                )
                self._store_skip_segment(event.unified_msg_origin, text)
                return
            except Exception as exc:
                logger.error("智能分段 LLM 调用失败: %s", exc, exc_info=True)
                self._store_skip_segment(event.unified_msg_origin, text)
                return

        if not segments:
            return

        self._store_prepared_segments(event.unified_msg_origin, text, segments)
        event.set_extra(
            _PREPARED_EXTRA_KEY,
            {"text": text, "segments": list(segments)},
        )
        logger.info("智能分段预处理完成，共 %s 段", len(segments))

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """发送前把完整回复替换为首段，并登记剩余段。"""
        settings = self._get_settings()
        if settings is None:
            return

        result = event.get_result()
        if result is None or not self._is_model_text_result(result):
            return

        raw_outbound = self._extract_plain_text_chain(result)
        if not raw_outbound:
            return
        outbound_text = clean_markdown_by_mode(
            raw_outbound,
            settings.markdown_clean_mode,
        )
        if not outbound_text:
            return
        if outbound_text != raw_outbound:
            result.chain = [Plain(outbound_text)]

        session = event.unified_msg_origin
        self._prune_expired_pending_follow_ups()
        self._ensure_lock_reaper()
        lock = self._session_locks.get(session)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session] = lock
        await lock.acquire()
        follow_up_holds_lock = False
        try:
            # 传入最大分段数与最小长度参数，以支持自愈拼接决策及本地高精兜底切分
            segments = self._take_prepared_segments_from_event(
                event,
                outbound_text,
                settings=settings,
            )
            if segments is None:
                segments = self._pop_prepared_segments(
                    event.unified_msg_origin,
                    outbound_text,
                    min_length=settings.min_length,
                    max_segments=settings.max_segments,
                    max_chars_per_segment=settings.max_chars_per_segment,
                    ellipsis_pause_min_chars=settings.ellipsis_pause_min_chars,
                    **self._split_option_kwargs(settings),
                )
            if not segments or len(segments) <= 1:
                return

            segments = stabilize_segment_lengths(
                segments,
                max_segments=settings.max_segments,
                max_chars_per_segment=settings.max_chars_per_segment,
                pause_min_chars=settings.ellipsis_pause_min_chars,
            )
            if not segments or len(segments) <= 1:
                return

            first_segment = segments[0]
            follow_up_segments = segments[1:]
            if not follow_up_segments:
                return

            result.chain = [Plain(first_segment)]
            pending_id = self._register_pending_follow_up(
                session=event.unified_msg_origin,
                segments=follow_up_segments,
                settings=settings,
                first_segment=first_segment,
            )
            event.set_extra(_PENDING_EXTRA_KEY, pending_id)
            pending = self._pending_follow_ups.get(pending_id)
            if pending is not None:
                pending.lock = lock
                follow_up_holds_lock = True
            logger.info("智能分段首段已替换，登记 %s 条补发消息", len(follow_up_segments))
        finally:
            if not follow_up_holds_lock:
                lock.release()

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """首段发送后后台补发剩余分段。"""
        pending_id = str(event.get_extra(_PENDING_EXTRA_KEY, "") or "").strip()
        if not pending_id:
            return

        pending = self._pop_pending_follow_up(pending_id)
        if pending is None or not pending.segments:
            return

        try:
            task = asyncio.create_task(self._run_follow_up_segments(pending))
        except Exception:
            self._release_pending_lock(pending)
            raise
        self._track_follow_up_task(task)

    async def terminate(self) -> None:
        """插件卸载时取消尚未完成的补发任务并清空缓存。"""
        if self._lock_reaper_task is not None and not self._lock_reaper_task.done():
            self._lock_reaper_task.cancel()
            try:
                await self._lock_reaper_task
            except asyncio.CancelledError:
                pass
            self._lock_reaper_task = None
        for task in list(self._active_follow_up_tasks):
            if not task.done():
                task.cancel()
        await self._drain_tasks()
        self._active_follow_up_tasks.clear()
        self._prepared_segments.clear()
        for pending in list(self._pending_follow_ups.values()):
            self._release_pending_lock(pending)
        self._pending_follow_ups.clear()
        self._session_locks.clear()

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
        conflicts = sorted(set(smart_increase) & set(smart_decrease))
        if conflicts:
            smart_increase = [
                symbol for symbol in smart_increase if symbol not in conflicts
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

    @staticmethod
    def _extract_response_plain_text(response: LLMResponse) -> str:
        """从大语言模型响应结构中提取纯文本，自动过滤思考标签。"""
        role = str(getattr(response, "role", "") or "").strip().lower()
        if role and role not in {"assistant", "ai"}:
            return ""

        result_chain = getattr(response, "result_chain", None)
        if result_chain is not None and not SmartSegmentationPlugin._is_plain_chain(
            result_chain,
        ):
            return ""

        text = str(getattr(response, "completion_text", "") or "")
        return strip_thinking_content(text)

    @staticmethod
    def _is_plain_chain(message_chain: MessageChain) -> bool:
        """判断消息链是否为纯文本组成的轻量级消息链。"""
        chain = getattr(message_chain, "chain", None)
        return isinstance(chain, list) and bool(chain) and all(
            isinstance(component, Plain) for component in chain
        )

    @classmethod
    def _extract_plain_text_chain(cls, message_chain: MessageChain) -> str:
        """从消息事件结果中提取并整合干净的纯文本内容。"""
        if not cls._is_plain_chain(message_chain):
            return ""
        texts = [component.text for component in message_chain.chain]
        return strip_thinking_content(" ".join(texts))

    @classmethod
    def _is_model_text_result(cls, result: MessageEventResult) -> bool:
        """判断当前消息装饰结果是否为大模型生成的纯文本响应。"""
        is_model_result = getattr(result, "is_model_result", None)
        if callable(is_model_result):
            try:
                if not is_model_result():
                    return False
            except Exception:
                return False
        return cls._is_plain_chain(result)

    @staticmethod
    def _should_segment_text(text: str, settings: SegmentationSettings) -> bool:
        """根据文本长度及特征，判定当前文本是否需要执行分段。"""
        if not text:
            return False
        if len(text) < settings.min_length:
            return False
        return not is_action_only_text(text)

    async def _resolve_provider_id(
        self,
        event: AstrMessageEvent,
        settings: SegmentationSettings,
    ) -> str:
        """获取当前消息事件对应会话的底层 LLM 提供商 ID。"""
        if settings.provider_id:
            return settings.provider_id

        get_current = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(get_current):
            try:
                provider_id = await get_current(event.unified_msg_origin)
                if provider_id:
                    return str(provider_id).strip()
            except Exception as exc:
                logger.debug("获取当前会话 provider_id 失败: %s", exc)

        get_using = getattr(self.context, "get_using_provider", None)
        if callable(get_using):
            try:
                provider = get_using(event.unified_msg_origin)
                meta = provider.meta() if provider and hasattr(provider, "meta") else None
                provider_id = getattr(meta, "id", "") if meta else ""
                return str(provider_id or "").strip()
            except Exception as exc:
                logger.debug("回退获取 provider_id 失败: %s", exc)

        return ""

    async def _segment_text(
        self,
        text: str,
        *,
        provider_id: str,
        settings: SegmentationSettings,
    ) -> list[str]:
        """调用大模型并传入针对性提示词，执行智能分段流程。"""
        if has_protected_block(text) and not is_markdown_document(text):
            return await self._segment_text_with_anchors(
                text,
                provider_id=provider_id,
                settings=settings,
            )
        masked_text, pipe_placeholder = mask_pipe_in_text(text)
        prompt = build_segmentation_prompt(
            masked_text,
            settings.style,
            settings.max_segments,
        )
        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
        )
        raw_text = str(getattr(response, "completion_text", "") or "").strip()
        if not raw_text:
            return [text]

        segments = parse_segments_from_model_output(
            raw_text,
            fallback_text=masked_text,
            max_segments=settings.max_segments,
            max_chars_per_segment=settings.max_chars_per_segment,
            pause_min_chars=settings.ellipsis_pause_min_chars,
            **self._split_option_kwargs(settings),
        )
        return restore_pipe_in_segments(segments, pipe_placeholder)

    async def _segment_text_with_anchors(
        self,
        text: str,
        *,
        provider_id: str,
        settings: SegmentationSettings,
    ) -> list[str]:
        """含受保护块消息的锚点一次调用路径（§5.6）。

        块原位替换为唯一 alnum 锚点后只调用一次 LLM（段数预算扣除块数）；
        重建或锚点校验失败、LLM 异常时对原文执行本地切分——效果等于
        v1.4.1 的整条降级，不倒退。锚点还原在返回前完成，缓存、outbound
        与补发队列只见真实分段。
        """
        def _local_fallback() -> list[str]:
            return local_fallback_split(
                text,
                settings.max_segments,
                settings.max_chars_per_segment,
                pause_min_chars=settings.ellipsis_pause_min_chars,
                **self._split_option_kwargs(settings),
            )

        masking = build_anchor_masking(text)
        if masking is None:
            return _local_fallback()
        if not masking.has_text:
            # 只有受保护块没有文本区间：不调用 LLM（§5.6 步骤 8）。
            return _local_fallback()

        try:
            masked_text, pipe_placeholder = mask_pipe_in_text(masking.masked_text)
            budget = max(1, settings.max_segments - len(masking.anchors))
            prompt = build_segmentation_prompt(masked_text, settings.style, budget)
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
            )
            raw_text = str(getattr(response, "completion_text", "") or "").strip()
            if not raw_text:
                raise ValueError("分段模型返回为空")
            llm_segments = parse_raw_model_segments(raw_text, fallback_text=masked_text)
            expanded = segment_from_anchored_output(
                llm_segments,
                masked_text,
                masking,
                pipe_placeholder,
            )
            if not expanded:
                raise ValueError("锚点校验未通过或重建失败")
        except Exception as exc:
            logger.warning("智能分段锚点路径失败，已降级本地切分: %s", exc)
            return _local_fallback()

        # 还原域后处理：与普通 LLM 路径语义一致（软约束在硬上限之前）。
        final = apply_no_split_boundaries(
            expanded,
            settings.no_split_around,
            settings.max_chars_per_segment,
        )
        final = apply_force_split_to_segments(final, settings.force_split_symbols)
        if settings.merge_short_tail:
            final = merge_short_tail_segment(
                final,
                settings.max_chars_per_segment,
                settings.short_tail_threshold,
            )
        final = safe_escape_segments(final)
        return stabilize_segment_lengths(
            final,
            max_segments=settings.max_segments,
            max_chars_per_segment=settings.max_chars_per_segment,
            pause_min_chars=settings.ellipsis_pause_min_chars,
        )

    def _take_prepared_segments_from_event(
        self,
        event: AstrMessageEvent,
        outbound_text: str,
        *,
        settings: SegmentationSettings,
    ) -> list[str] | None:
        """Use segments bound to the current event before falling back to hashing."""
        extra = event.get_extra(_PREPARED_EXTRA_KEY, None)
        if not isinstance(extra, dict):
            return None

        prepared_text = str(extra.get("text") or "")
        prepared_segments = extra.get("segments")
        if not prepared_text or not isinstance(prepared_segments, list):
            return None
        if not prepared_segments:
            return []

        if outbound_text == prepared_text:
            return list(prepared_segments)

        outbound_key = normalize_content_key(outbound_text)
        prepared_key = normalize_content_key(prepared_text)
        if outbound_key == prepared_key:
            return list(prepared_segments)
        if outbound_key.startswith(prepared_key) and outbound_text.startswith(
            prepared_text
        ):
            segments = list(prepared_segments)
            suffix = outbound_text[len(prepared_text) :]
            # 补切预算与已有段数对齐，避免叠加后超出 max_segments
            # 触发装饰阶段的强制合并、破坏预处理分段边界。
            budget = (
                self._aligned_split_budget(segments, settings)
                if settings.max_segments > 0
                else 0
            )
            suffix_segments = local_fallback_split(
                suffix,
                budget,
                settings.max_chars_per_segment,
                pause_min_chars=settings.ellipsis_pause_min_chars,
                **self._split_option_kwargs(settings),
            )
            segments.extend(suffix_segments)
            return segments
        # Tool Loop / 中间插件改写会在 prepared 文本头部或尾部插入内容，
        # 按内容签名在 outbound 中定位 prepared，头部/尾部只补切插入部分，
        # 预处理的分段边界全部保留。
        aligned = align_prepared_in_outbound(outbound_text, prepared_text)
        if aligned is not None:
            head, _middle, tail = aligned
            segments = list(prepared_segments)
            segments = self._prepend_aligned_parts(
                segments,
                head,
                settings,
            )
            segments = self._append_aligned_parts(
                segments,
                tail,
                settings,
            )
            # 补切预算「至少保留 1 段以不丢内容」可能让总段数超出
            # max_segments（池内已占满时），此处强制合并兜底保证段数达标。
            if settings.max_segments > 0 and len(segments) > settings.max_segments:
                segments = stabilize_segment_lengths(
                    segments,
                    max_segments=settings.max_segments,
                    max_chars_per_segment=settings.max_chars_per_segment,
                    pause_min_chars=settings.ellipsis_pause_min_chars,
                )
            return segments
        return None

    def _aligned_split_budget(self, segments: list[str], settings: SegmentationSettings) -> int:
        """计算对齐补切的段数预算：尽量不超出 max_segments，但至少保留 1 段以不丢内容。"""
        max_segments = settings.max_segments
        if max_segments <= 0:
            return 0
        return max(1, max_segments - len(segments))

    def _prepend_aligned_parts(
        self,
        segments: list[str],
        head_text: str,
        settings: SegmentationSettings,
    ) -> list[str]:
        """对 prepared 之前的插入文本按剩余段数预算本地补切并前置拼接。"""
        if not str(head_text or "").strip():
            return segments
        head_segments = local_fallback_split(
            head_text,
            self._aligned_split_budget(segments, settings),
            settings.max_chars_per_segment,
            pause_min_chars=settings.ellipsis_pause_min_chars,
            **self._split_option_kwargs(settings),
        )
        return head_segments + segments

    def _append_aligned_parts(
        self,
        segments: list[str],
        tail_text: str,
        settings: SegmentationSettings,
    ) -> list[str]:
        """对 prepared 之后的插入文本本地补切并后置拼接。"""
        if not str(tail_text or "").strip():
            return segments
        budget = self._aligned_split_budget(segments, settings)
        tail_segments = local_fallback_split(
            tail_text,
            budget,
            settings.max_chars_per_segment,
            pause_min_chars=settings.ellipsis_pause_min_chars,
            **self._split_option_kwargs(settings),
        )
        return segments + tail_segments

    def _ensure_lock_reaper(self) -> None:
        """Ensure expired pending locks are released even without after_message_sent."""
        if self._lock_reaper_task is None or self._lock_reaper_task.done():
            self._lock_reaper_task = asyncio.create_task(self._run_lock_reaper())

    async def _run_lock_reaper(self) -> None:
        try:
            while True:
                await asyncio.sleep(_PENDING_REAPER_INTERVAL_SECONDS)
                self._prune_expired_pending_follow_ups()
        except asyncio.CancelledError:
            raise

    async def _send_follow_up_with_retry(
        self,
        session: str,
        segment: str,
        attempts: int = 3,
    ) -> bool:
        for attempt in range(1, attempts + 1):
            try:
                sent = await self.context.send_message(
                    session,
                    MessageChain([Plain(segment)]),
                )
            except Exception as exc:
                logger.warning("智能分段补发第 %s 次异常: %s", attempt, exc)
                sent = False
            if sent:
                return True
            if attempt < attempts:
                await asyncio.sleep(min(0.5 * attempt, 2.0))
        return False

    def _store_prepared_segments(
        self,
        session: str,
        response_text: str,
        segments: list[str],
    ) -> None:
        """将预处理的分段结果及原始文本的特征摘要写入高速缓存。"""
        self._prune_expired_prepared_segments()
        text_hash = hash_normalized_text(response_text)
        normalized_session = str(session or "").strip()
        if not normalized_session or not text_hash:
            return

        # 缓存分段结果的同时，缓存其原始文本的规范化特征，作为未来恢复拼接拼合的基准
        self._prepared_segments[(normalized_session, text_hash)] = PreparedSegments(
            segments=list(segments),
            raw_norm_text=normalize_response_text_for_key(response_text),
            expires_at=time.monotonic() + _PREPARED_SEGMENT_TTL_SECONDS,
            match_key=normalize_content_key(response_text),
        )

    def _store_skip_segment(self, session: str, response_text: str) -> None:
        """缓存本次不分段的负向结果，避免装饰阶段被本地兜底二次拆分。"""
        self._store_prepared_segments(session, response_text, [])

    def _pop_prepared_segments(
        self,
        session: str,
        outbound_text: str,
        *,
        min_length: int = 15,
        max_segments: int = 0,
        max_chars_per_segment: int = 0,
        ellipsis_pause_min_chars: int = 4,
        force_split_symbols: list[str] | None = None,
        no_split_around: list[str] | None = None,
        merge_short_tail: bool = False,
        short_tail_threshold: int = 2,
    ) -> list[str] | None:
        """从预存库中获取分段结果（精确匹配 -> 顺序贪婪组合自愈 -> 本地高精分段兜底）。"""
        self._prune_expired_prepared_segments()
        normalized_session = str(session or "").strip()
        norm_outbound = normalize_response_text_for_key(outbound_text)
        if not normalized_session or not norm_outbound:
            return None

        # 1. 精确哈希匹配
        text_hash = hash_normalized_text(outbound_text)
        entry = self._prepared_segments.pop((normalized_session, text_hash), None)
        if entry is not None:
            return list(entry.segments)

        # 2. 顺序自愈：先用内容签名精确匹配，再按内容签名前缀贪婪拼接。
        # 内容签名忽略空白、大小写与标点，可覆盖 Markdown 清洗和 Tool Loop 合并带来的文本差异。
        session_entries = {
            key: val
            for key, val in self._prepared_segments.items()
            if key[0] == normalized_session
        }
        if session_entries:
            outbound_match_key = normalize_content_key(outbound_text)
            for key, entry in session_entries.items():
                if entry.match_key and entry.match_key == outbound_match_key:
                    self._prepared_segments.pop(key, None)
                    return list(entry.segments)

            remaining = outbound_match_key
            assembled_segs = []
            matched_keys = []
            while remaining:
                best_match_len = 0
                best_match_segs = None
                best_match_key = None
                for key, val in session_entries.items():
                    piece = val.match_key
                    # 负向缓存条目（segments 为空，表示"该文本不分段"）不参与拼合，
                    # 否则其覆盖的中间文本会被空分段吞掉。
                    if not val.segments:
                        continue
                    if piece and remaining.startswith(piece):
                        piece_length = len(piece)
                        if piece_length > best_match_len:
                            best_match_len = piece_length
                            best_match_segs = val.segments
                            best_match_key = key
                if best_match_segs is not None and best_match_key:
                    assembled_segs.extend(best_match_segs)
                    remaining = remaining[best_match_len:]
                    matched_keys.append(best_match_key)
                else:
                    break

            if not remaining and assembled_segs:
                for m_key in matched_keys:
                    self._prepared_segments.pop(m_key, None)
                return assembled_segs

            # 2.5 contains 对齐：prepared 文本出现在 outbound 中间
            # （Tool Loop 头部插入等场景，前缀拼合无法命中）。
            best_aligned_key: str | None = None
            best_aligned_entry: Any = None
            best_aligned_parts: tuple[str, str, str] | None = None
            for key, val in session_entries.items():
                # 负向缓存条目（segments 为空）不参与 contains 对齐，
                # 否则对齐命中的正文会被空分段吞掉。
                if not val.segments:
                    continue
                if not val.match_key or len(val.match_key) < 8:
                    continue
                if val.match_key == outbound_match_key:
                    continue
                aligned = align_prepared_in_outbound(
                    outbound_text, val.raw_norm_text
                )
                if aligned is None:
                    continue
                if (
                    best_aligned_parts is None
                    or len(val.match_key) > len(
                        session_entries[best_aligned_key].match_key or ""
                    )
                ):
                    best_aligned_key = key
                    best_aligned_entry = val
                    best_aligned_parts = aligned
            if (
                best_aligned_key
                and best_aligned_entry is not None
                and best_aligned_parts is not None
            ):
                self._prepared_segments.pop(best_aligned_key, None)
                head, _middle, tail = best_aligned_parts
                pooled_settings = SegmentationSettings(
                    max_segments=max_segments,
                    max_chars_per_segment=max_chars_per_segment,
                    force_split_symbols=list(force_split_symbols or []),
                    no_split_around=list(no_split_around or []),
                    merge_short_tail=merge_short_tail,
                    short_tail_threshold=short_tail_threshold,
                )
                segments = list(best_aligned_entry.segments)
                segments = self._prepend_aligned_parts(
                    segments,
                    head,
                    pooled_settings,
                )
                segments = self._append_aligned_parts(
                    segments,
                    tail,
                    pooled_settings,
                )
                # 同 _pop 前缀/对齐分支：补切预算「至少保留 1 段」可能
                # 让总段数超出 max_segments，强制合并兜底保证段数达标。
                if max_segments > 0 and len(segments) > max_segments:
                    segments = stabilize_segment_lengths(
                        segments,
                        max_segments=max_segments,
                        max_chars_per_segment=max_chars_per_segment,
                    )
                return segments

        # 3. 终极自愈兜底：本地纯逻辑高精切分
        if len(outbound_text) >= min_length and not is_action_only_text(outbound_text):
            # 直接复用 segmentation.py 中封装的高精度切分函数
            local_segs = local_fallback_split(
                outbound_text,
                max_segments,
                max_chars_per_segment,
                pause_min_chars=ellipsis_pause_min_chars,
                force_split_symbols=list(force_split_symbols or []),
                no_split_around=list(no_split_around or []),
                merge_short_tail=merge_short_tail,
                short_tail_threshold=short_tail_threshold,
            )
            if local_segs and len(local_segs) > 1:
                logger.info("智能分段精准哈希/拼合未中，已自动降级启动本地高精切分兜底")
                return local_segs

        return None

    def _prune_expired_prepared_segments(self) -> None:
        """清理缓存池中生命周期已结束的预存分段条目，防止内存溢出。"""
        if not self._prepared_segments:
            return
        now = time.monotonic()
        expired_keys = [
            key
            for key, entry in self._prepared_segments.items()
            if entry.expires_at <= now
        ]
        for key in expired_keys:
            self._prepared_segments.pop(key, None)

    def _register_pending_follow_up(
        self,
        *,
        session: str,
        segments: list[str],
        settings: SegmentationSettings,
        first_segment: str | None = None,
    ) -> str:
        """登记当前会话中等待后台补发的所有后续分段消息。"""
        self._prune_expired_pending_follow_ups()
        pending_id = uuid4().hex
        self._pending_follow_ups[pending_id] = PendingFollowUp(
            session=session,
            segments=list(segments),
            delay_base=settings.delay_base,
            delay_per_char=settings.delay_per_char,
            delay_max=settings.delay_max,
            expires_at=time.monotonic() + _PENDING_FOLLOW_UP_TTL_SECONDS,
            delay_strategy=settings.delay_strategy,
            # 首条补发的「上一段」就是已经发送出去的首段。
            previous_segment=first_segment,
            smart_increase_symbols=list(settings.smart_increase_symbols),
            smart_increase_seconds=settings.smart_increase_seconds,
            jitter_max_seconds=settings.jitter_max_seconds,
            smart_decrease_symbols=list(settings.smart_decrease_symbols),
            smart_decrease_seconds=settings.smart_decrease_seconds,
        )
        return pending_id

    def _pop_pending_follow_up(self, pending_id: str) -> PendingFollowUp | None:
        """检索并提取处于挂起状态下的延迟补发分段上下文。"""
        self._prune_expired_pending_follow_ups()
        return self._pending_follow_ups.pop(pending_id, None)

    def _release_pending_lock(self, pending: PendingFollowUp) -> None:
        """释放 pending 持有的会话串行锁，避免补发任务异常后锁泄漏。"""
        if pending.lock is not None and pending.lock.locked():
            pending.lock.release()
        pending.lock = None

    def _prune_expired_pending_follow_ups(self) -> None:
        """扫描并清理所有因超时失效的延迟补发上下文缓存。"""
        if not self._pending_follow_ups:
            return
        now = time.monotonic()
        expired_keys = [
            key for key, entry in self._pending_follow_ups.items() if entry.expires_at <= now
        ]
        for key in expired_keys:
            entry = self._pending_follow_ups.pop(key, None)
            if entry is not None:
                self._release_pending_lock(entry)

    def _track_follow_up_task(self, task: asyncio.Task[Any]) -> None:
        """追踪正在后台运行的异步补发任务，并在其完成后清理任务句柄。"""
        self._active_follow_up_tasks.add(task)
        task.add_done_callback(self._active_follow_up_tasks.discard)

    async def _drain_tasks(self) -> None:
        """强制等待或取消所有正在进行的后台补发异步任务，防止插件卸载时出现孤儿任务。"""
        tasks = [task for task in list(self._active_follow_up_tasks) if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_follow_up_segments(self, pending: PendingFollowUp) -> None:
        """后台异步主循环，按照时间延迟算法逐条补发余下的消息分段。"""
        try:
            # smart 延迟的符号修正看「上一段结尾」：首条补发的上一段是
            # 已发送的首段，后续补发的上一段是循环中的前一条。
            previous_segment = pending.previous_segment
            for segment in pending.segments:
                delay = calculate_send_delay(
                    segment,
                    pending.delay_base,
                    pending.delay_per_char,
                    pending.delay_max,
                    strategy=pending.delay_strategy,
                    previous_segment=previous_segment,
                    smart_increase_symbols=pending.smart_increase_symbols,
                    smart_increase_seconds=pending.smart_increase_seconds,
                    jitter_max_seconds=pending.jitter_max_seconds,
                    smart_decrease_symbols=pending.smart_decrease_symbols,
                    smart_decrease_seconds=pending.smart_decrease_seconds,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

                sent = await self._send_follow_up_with_retry(
                    pending.session,
                    segment,
                )
                previous_segment = segment
                if not sent:
                    logger.error("智能分段补发最终失败，会话: %s", pending.session)
        except asyncio.CancelledError:
            logger.warning("智能分段后台补发任务被取消，会话: %s", pending.session)
            raise
        except Exception as exc:
            logger.error("智能分段后台补发任务异常: %s", exc, exc_info=True)
        finally:
            self._release_pending_lock(pending)
