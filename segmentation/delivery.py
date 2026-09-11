"""智能分段插件内部模块。"""
from __future__ import annotations

import random


def calculate_send_delay(
    segment: str,
    delay_base: float | None,
    delay_per_char: float | None,
    delay_max: float | None,
) -> float:
    """根据文本长度计算分条发送间隔。"""
    base = float(delay_base) if delay_base is not None else 2.0
    per_char = float(delay_per_char) if delay_per_char is not None else 0.05
    max_d = float(delay_max) if delay_max is not None else 3.0
    seg_len = len(segment) if segment else 0
    normalized_delay = base + seg_len * per_char
    normalized_delay += random.uniform(0.0, 0.15)
    return max(0.0, min(max_d, normalized_delay))
