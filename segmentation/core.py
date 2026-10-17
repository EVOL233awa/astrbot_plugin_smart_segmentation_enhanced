"""智能分段插件内部模块。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from typing import Any


_LOG = logging.getLogger(__name__.rsplit(".", 1)[0])


THINKING_TAG_RE = re.compile(
    r"<thinking>(?:[\s\S]*?)(?:</thinking>|$)",
    flags=re.IGNORECASE,
)


THINKING_BOUNDARY_RE = re.compile(r"</?thinking>", flags=re.IGNORECASE)


PROTOCOL_ARTIFACTS_RE = re.compile(
    r"("
    r"<tool_call>[\s\S]*?(?:</tool_call>|$)"
    r"|<function_call>[\s\S]*?(?:</function_call>|$)"
    r"|\[Tooltip:[^\]]*(?:\]|$)"
    r"|\[System:[^\]]*(?:\]|$)"
    r")",
    flags=re.IGNORECASE,
)


BRACKET_PAIRS: tuple[tuple[str, str], ...] = (
    ("（", "）"),
    ("(", ")"),
    ("【", "】"),
    ("[", "]"),
)


_SEGMENT_LEADING_PUNCT_CHARS = frozenset("…·~—–―〜？！。.")


_LEADING_PUNCTUATION_RE = re.compile(
    r"^(?:…+|\.{2,}|·+|~+|—+|–+|―+|〜+|？+|！+|。{2,})+$"
)


def is_ascii_alnum(char: str) -> bool:
    """仅判定字符是否为 ASCII 范围内的字母或数字，避免中文字符干扰。"""
    return char.isascii() and char.isalnum()


def _as_nonneg_int(value: Any) -> int:
    """将配置值安全解析为非负整数；空值、非法值或负数一律回落到 0（不限制）。"""
    try:
        parsed = int(value) if value is not None else 0
    except (ValueError, TypeError):
        return 0
    return parsed if parsed >= 0 else 0


def is_kaomoji_or_pure_punct(text: str) -> bool:
    """判定文本是否为纯标点、连续标点或短颜文字，用于防止其孤立成段。"""
    if not text:
        return False
    text = text.strip()
    if not text:
        return False
        
    # 1. 含有中文汉字则绝不判定为纯颜文字，防止语义短句被强行向前合并
    if re.search(r"[\u4e00-\u9fa5]", text):
        return False
        
    # 2. 纯标点检测
    puncts = set("。！？!?~.……,，、;；:：'\"()（）【】[]{}<>《》 ")
    if all(c in puncts for c in text):
        return True
        
    # 3. 常见颜文字特征检测 (排除了中文后，再检测连续符号)
    if len(text) <= 10 and re.search(r"[><✨☆ㅠ^°¯\*_\-\|/\\]{2,}", text):
        return True
        
    # 4. 经典字母颜文字白名单
    if text.lower() in ["qwq", "awa", "qaq", "ovo", "uwu", "orz", "t_t", "q_q", "x_x", "tv_t"]:
        return True
        
    return False


def is_leading_punctuation(text: str) -> bool:
    """判断文本是否为应归属后续内容段的前导标点。"""
    stripped = str(text or "").strip()
    return bool(_LEADING_PUNCTUATION_RE.match(stripped))


def strip_thinking_content(text: str) -> str:
    """移除 thinking 标签、Tool Call 伪影，只保留最终可见正文（保留通用代码块）。"""
    if not text:
        return ""
    cleaned_text = THINKING_TAG_RE.sub("", str(text))
    cleaned_text = THINKING_BOUNDARY_RE.sub("", cleaned_text)
    cleaned_text = PROTOCOL_ARTIFACTS_RE.sub("", cleaned_text)
    return cleaned_text.strip()


def is_non_natural_language(text: str) -> bool:
    """检测文本是否为完整的 JSON、纯代码等非自然语言。"""
    if not text:
        return False
    stripped = text.strip()
    
    # 1. 优先全局守卫：汉字占比超 10% 绝不判定为非自然语言，防止结构化文档退化为整条发送
    chinese_char_count = len(re.findall(r"[\u4e00-\u9fa5]", stripped))
    if len(stripped) > 0 and (chinese_char_count / len(stripped)) >= 0.10:
        return False
        
    # 2. 结构化 JSON 解析校验
    if (stripped.startswith("{") and stripped.endswith("}")) or \
       (stripped.startswith("[") and stripped.endswith("]")):
        try:
            json.loads(stripped)
            return True
        except Exception:
            pass
            
    # 3. 纯代码块特征保护：只有整段以围栏开头的文本才整体放行。
    if stripped.startswith("```") or stripped.startswith("~~~"):
        return True
        
    # 4. 强特征配置字段
    if '"mcpServers"' in stripped or '"active":' in stripped or '"command":' in stripped:
        return True
        
    # 5. 特殊字符密度综合判定（已移除多余的二次中文校验）
    special_chars = set('{}[]":;,=<>')
    special_count = sum(1 for char in stripped if char in special_chars)
    if len(stripped) > 20 and (special_count / len(stripped)) > 0.15:
        return True
        
    return False


def normalize_segments(segments: Any, max_segments: int | str = 0) -> list[str]:
    """标准化并自动扁平化段落。使用空格拼接防止文本粘连。"""
    max_segs = _as_nonneg_int(max_segments)
        
    if not isinstance(segments, list):
        if isinstance(segments, str):
            return [segments.strip()] if segments.strip() else []
        return []
        
    result: list[str] = []
    
    def flatten(item: Any):
        if isinstance(item, list):
            for sub_item in item:
                flatten(sub_item)
        elif item is not None:
            item_str = str(item).strip()
            if item_str:
                result.append(item_str)
                
    flatten(segments)
    
    if max_segs > 0 and len(result) > max_segs:
        head = result[: max_segs - 1]
        tail = " ".join(result[max_segs - 1 :])
        result = head + [tail]
        
    return result


def is_action_only_text(text: str) -> bool:
    """判断文本是否整体是一个括号动作段。

    动作括号之后允许跟随纯标点/符号（如「（点头）。」「（笑）……」），
    这些收尾符号属于动作段本身；一旦括号后出现正文（汉字、字母、数字），
    就不再是动作段。
    """
    stripped = str(text or "").strip()
    if len(stripped) < 2:
        return False

    for open_bracket, close_bracket in BRACKET_PAIRS:
        if not stripped.startswith(open_bracket):
            continue

        depth = 0
        for index, char in enumerate(stripped):
            if char == open_bracket:
                depth += 1
            elif char == close_bracket:
                depth -= 1
                if depth == 0:
                    trailing = stripped[index + 1:]
                    return all(
                        char.isspace()
                        or unicodedata.category(char)[0] in {"P", "S"}
                        for char in trailing
                    )
        return False

    return False


def normalize_response_text_for_key(text: str) -> str:
    """归一化用于查找预分段缓存的文本。"""
    cleaned = strip_thinking_content(str(text or ""))
    if not cleaned:
        return ""
    return " ".join(cleaned.split())


def normalize_content_key(text: str) -> str:
    """生成忽略空白、大小写与标点的内容签名，用于缓存容错匹配。"""
    cleaned = strip_thinking_content(str(text or ""))
    return "".join(char.lower() for char in cleaned if char.isalnum())


def hash_normalized_text(text: str) -> str:
    """对归一化后的文本做稳定哈希。"""
    normalized = normalize_response_text_for_key(text)
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()
