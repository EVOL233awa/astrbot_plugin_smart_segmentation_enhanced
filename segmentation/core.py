"""智能分段插件内部模块。"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


# 只删成对标签。未闭合的 <thinking> 若按「延伸到文末」删除，正文里出现
# 字面量标签时会吞掉其后全部可见内容；推理泄漏由宿主 Provider 层处理
# （4.29.3 的 openai_source 已把成对推理抽到 reasoning_content）。
THINKING_TAG_RE = re.compile(
    r"<thinking>[\s\S]*?</thinking>",
    flags=re.IGNORECASE,
)


# 只清理孤立的闭标签；未闭合的开标签连正文一起原样保留，与宿主 Provider
# 行为一致（4.29.3 也只清理文末的闭标签，不删开标签之后的正文）。
THINKING_BOUNDARY_RE = re.compile(r"</thinking>", flags=re.IGNORECASE)


PROTOCOL_ARTIFACTS_RE = re.compile(
    r"("
    r"<tool_call>[\s\S]*?</tool_call>"
    r"|<function_call>[\s\S]*?</function_call>"
    r"|\[Tooltip:[^\]]*\]"
    r"|\[System:[^\]]*\]"
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


# 可并入相邻段的标点：句读标点与停顿符（省略号、破折号）。
# 它们只标记停顿，读起来是「话没说完」，跟着后面的动作段是自然语流。
# 波浪号与颜文字不在此列——`～`、`✨>_<` 是完整的语气/表情表达单元，
# 并进动作段会把两个单元粘成一条，打散又会丢失原有节奏。
_PURE_SENTENCE_PUNCT_CHARS = frozenset(
    "。！？!?，,、；;：:\"'“”‘’（）()【】[]{}《》「」『』"
    "…·.—–― \u3000"
)


def is_pure_sentence_punct(text: str) -> bool:
    """判断文本是否只由句读标点与停顿符组成（不含波浪号与颜文字）。"""
    stripped = str(text or "").strip()
    if not stripped:
        return False
    return all(char in _PURE_SENTENCE_PUNCT_CHARS for char in stripped)


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
        
    # 2. 纯标点检测：半角与全角的波浪号、破折号同为语气符号，不能当成正文
    puncts = set("。！？!?~～〜.……,，、;；:：'\"()（）【】[]{}<>《》—–― ")
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


_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# 闭合围栏只允许行尾空白；带 info string 的行（如 ```python）不是闭合。
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")


def _split_fenced_text(text: str) -> list[tuple[bool, str]]:
    """按 Markdown 代码围栏把文本切成「是否围栏内」片段，围栏行与内容原样保留。

    未闭合的围栏视为延续到文末（与 Markdown 渲染一致），因此其内容也不会被清洗。
    """
    segments: list[tuple[bool, str]] = []
    buffer: list[str] = []
    fence_char = ""
    fence_length = 0
    in_fence = False

    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if in_fence:
            buffer.append(line)
            match = _FENCE_CLOSE_RE.match(body)
            if (
                match
                and match.group(1)[0] == fence_char
                and len(match.group(1)) >= fence_length
            ):
                segments.append((True, "".join(buffer)))
                buffer = []
                in_fence = False
            continue

        match = _FENCE_OPEN_RE.match(body)
        if match:
            if buffer:
                segments.append((False, "".join(buffer)))
                buffer = []
            fence_char = match.group(1)[0]
            fence_length = len(match.group(1))
            in_fence = True
        buffer.append(line)

    if buffer:
        segments.append((in_fence, "".join(buffer)))
    return segments


def _strip_protocol_outside_fences(chunk: str, remove_thinking: bool) -> str:
    if remove_thinking:
        chunk = THINKING_TAG_RE.sub("", chunk)
        chunk = THINKING_BOUNDARY_RE.sub("", chunk)
    return PROTOCOL_ARTIFACTS_RE.sub("", chunk)


def strip_thinking_content(text: str, *, remove_thinking: bool = True) -> str:
    """移除 thinking 标签、Tool Call 伪影，只保留最终可见正文（保留通用代码块）。

    ``remove_thinking=False`` 时完全不碰 thinking 标签（含成对块），
    只清理 Tool Call 等协议伪影。
    """
    if not text:
        return ""
    return "".join(
        chunk if in_fence else _strip_protocol_outside_fences(chunk, remove_thinking)
        for in_fence, chunk in _split_fenced_text(str(text))
    ).strip()


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

    空括号（`（）`、`()`）不算动作段：它常见于函数调用、占位符等程序
    写法，独立成条会切出只有一对括号的碎气泡。
    """
    stripped = str(text or "").strip()
    # Markdown 强调/分隔符残留（``***（点头）``、``---（叹气）``）不是正文，
    # 允许它们挂在动作括号前，动作段仍保持原子性。
    lead = 0
    while lead < len(stripped) and stripped[lead] in "*_-":
        lead += 1
    if lead:
        stripped = stripped[lead:].lstrip()
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
                    if not stripped[1:index].strip():
                        return False
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
