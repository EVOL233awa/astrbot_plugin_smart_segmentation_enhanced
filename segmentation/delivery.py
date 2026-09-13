"""智能分段插件内部模块。"""
from __future__ import annotations

import random
import unicodedata


# linear 模式的随机抖动上限，与既有行为保持一致。
_JITTER_MAX_SECONDS = 0.15

# smart 模式的绝对下限：符号修正（含减少）叠加后间隔也不低于 1s，
# 降低平台风控风险。
_SMART_MIN_FLOOR_SECONDS = 1.0

# smart 模式的默认符号修正秒数，与 _conf_schema.json 保持一致。
DEFAULT_SMART_INCREASE_SECONDS = 0.25
DEFAULT_SMART_DECREASE_SECONDS = 0.25


def weighted_length(text: str | None) -> float:
    """smart 模式的加权长度：非 ASCII 字符计 1.0，ASCII 计 0.5。"""
    if not text:
        return 0.0
    return sum(1.0 if ord(char) > 127 else 0.5 for char in text)


def previous_tail_symbols(previous_segment: str | None) -> str:
    """取上一段结尾的连续标点/符号串，先跳过尾部空白。

    这是「！后加速、……后等待」的落点：间隔属于上一段与当前段之间，
    当前段内部的符号不参与修正。
    """
    if not previous_segment:
        return ""
    index = len(previous_segment) - 1
    while index >= 0 and previous_segment[index].isspace():
        index -= 1
    end = index + 1
    while index >= 0:
        category = unicodedata.category(previous_segment[index])
        if not (category.startswith("P") or category.startswith("S")):
            break
        index -= 1
    return previous_segment[index + 1 : end]


def _nfkc_unique_keys(symbols: list[str] | None) -> list[str]:
    """把配置符号映射为 NFKC 归一化键并保序去重。

    全半角等价符号（「！」与 !、「……」与 ...）归一到同一个键，
    避免同一结尾位置被重复计分。空键（空字符串符号）丢弃。
    """
    seen: set[str] = set()
    keys: list[str] = []
    for symbol in symbols or []:
        key = unicodedata.normalize("NFKC", str(symbol))
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def calculate_symbol_adjustment(
    tail_symbols: str,
    increase_symbols: list[str] | None,
    increase_seconds: float,
    decrease_symbols: list[str] | None,
    decrease_seconds: float,
) -> float:
    """按上一段结尾符号串计算等待增减的净秒数。

    匹配在 NFKC 归一化后进行：全角/半角（「！」与 !、「……」与 ...、
    单个 …）自动等价，配置一个符号即可同时覆盖两种输入。扫描采用
    最长匹配、命中后消费：组合符号（如 ?!、...?）命中后其组成部分
    不再重复计分，而相邻的独立符号（如 ！！）各计一次。两侧列表冲突
    （同一归一化键）在配置加载阶段完成清洗。
    """
    if not tail_symbols:
        return 0.0
    tail_norm = unicodedata.normalize("NFKC", tail_symbols)
    if not tail_norm:
        return 0.0

    entries: list[tuple[str, float]] = []
    for key in _nfkc_unique_keys(increase_symbols):
        entries.append((key, float(increase_seconds)))
    for key in _nfkc_unique_keys(decrease_symbols):
        entries.append((key, -float(decrease_seconds)))
    if not entries:
        return 0.0

    # 从左到右贪心：每个位置取能匹配的最长符号，命中即消费该段。
    ordered = sorted(entries, key=lambda entry: -len(entry[0]))
    totals: dict[str, float] = {}
    position = 0
    length = len(tail_norm)
    while position < length:
        hit = None
        for key, seconds in ordered:
            if tail_norm.startswith(key, position):
                hit = (key, seconds)
                break
        if hit is None:
            position += 1
            continue
        key, seconds = hit
        totals[key] = totals.get(key, 0.0) + seconds
        position += len(key)
    return sum(totals.values())


def calculate_send_delay(
    segment: str,
    delay_base: float | None,
    delay_per_char: float | None,
    delay_max: float | None,
    *,
    strategy: str = "linear",
    previous_segment: str | None = None,
    jitter_max_seconds: float = _JITTER_MAX_SECONDS,
    smart_increase_symbols: list[str] | None = None,
    smart_increase_seconds: float = DEFAULT_SMART_INCREASE_SECONDS,
    smart_decrease_symbols: list[str] | None = None,
    smart_decrease_seconds: float = DEFAULT_SMART_DECREASE_SECONDS,
) -> float:
    """根据文本长度计算分条发送间隔。

    linear（确定性，无抖动）：min(delay_max, base + 段长 × per_char)。
    smart（含随机抖动与符号修正）：
        基础间隔 = min(delay_max, base + 加权长度(非 ASCII 1.0 / ASCII 0.5)
                     × per_char + 抖动(0 ~ jitter_max_seconds))
        最终间隔 = max(1s, 基础间隔 + 符号修正)
    符号修正按上一段结尾符号最长匹配（NFKC 归一化，全半角等价），
    叠加在 delay_max 之上允许浮动；下限 _SMART_MIN_FLOOR_SECONDS（1s，
    风控）。linear 忽略 previous_segment 与全部 smart 参数。
    """
    base = float(delay_base) if delay_base is not None else 2.0
    per_char = float(delay_per_char) if delay_per_char is not None else 0.05
    max_d = float(delay_max) if delay_max is not None else 3.0
    is_smart = strategy == "smart"
    if is_smart:
        seg_len = weighted_length(segment)
        normalized_delay = base + seg_len * per_char
        jitter_max = max(0.0, float(jitter_max_seconds))
        if jitter_max > 0:
            normalized_delay += random.uniform(0.0, jitter_max)
        normalized_delay = min(max_d, normalized_delay)
        if previous_segment:
            normalized_delay += calculate_symbol_adjustment(
                previous_tail_symbols(previous_segment),
                smart_increase_symbols,
                smart_increase_seconds,
                smart_decrease_symbols,
                smart_decrease_seconds,
            )
        return max(_SMART_MIN_FLOOR_SECONDS, normalized_delay)
    seg_len = len(segment) if segment else 0
    normalized_delay = base + seg_len * per_char
    return max(0.0, min(max_d, normalized_delay))
