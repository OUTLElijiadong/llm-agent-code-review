"""报告文本清理：移除模型 Markdown 装饰，同时保留围栏中的证据代码。"""

from __future__ import annotations

import re
from typing import Any

_CODE_KEYS = {"code", "source_code", "snippet", "patch", "diff", "raw_code", "fixed_code"}
_FENCED_CODE = re.compile(r"(?P<fence>`{3,}|~{3,})[^\n]*\n[\s\S]*?\n(?P=fence)")
_INLINE_CODE = re.compile(r"(?<!`)(?P<ticks>`+)(?!`)(?P<body>[^\n]*?)(?<!`)(?P=ticks)(?!`)")
_MASK_TOKEN = re.compile(r"\ue000(\d+)\ue001")
_SANDBOX_WORKER_ERROR = re.compile(
    r"Client error ['\"](?P<status>\d{3} [^'\"]+)['\"] for url ['\"]"
    r"https?://sandbox(?::\d+)?(?P<path>/[^'\"]*)['\"]",
    re.IGNORECASE,
)
_HTTPX_HELP_LINK = re.compile(
    r"\s*For more information check:\s*https?://developer\.mozilla\.org/\S*",
    re.IGNORECASE,
)
_SANDBOX_WORKER_URL = re.compile(
    r"https?://sandbox(?::\d+)?(?P<path>/[^\s'\"),，。；]*)?",
    re.IGNORECASE,
)


def _sandbox_worker_label(path: str) -> str:
    route = path.strip("/").split("/", 1)[0].lower()
    labels = {
        "execute": "沙箱执行接口",
        "stop": "沙箱停止接口",
        "cleanup": "沙箱资源回收接口",
        "health": "沙箱健康检查接口",
    }
    return labels.get(route, "沙箱内部接口")


def _clean_execution_noise(text: str) -> str:
    def replace_httpx_error(match: re.Match[str]) -> str:
        return f"{_sandbox_worker_label(match.group('path'))}返回 HTTP {match.group('status')}"

    text = _SANDBOX_WORKER_ERROR.sub(replace_httpx_error, text)
    text = _HTTPX_HELP_LINK.sub("", text)
    return _SANDBOX_WORKER_URL.sub(
        lambda match: _sandbox_worker_label(match.group("path") or ""),
        text,
    )


def _store_masked_value(value: str, slots: dict[str, str]) -> str:
    index = len(slots)
    token = f"\ue000{index}\ue001"
    while token in value:
        index += 1
        token = f"\ue000{index}\ue001"
    slots[token] = value
    return token


def _protect_markdown_code(text: str, *, keep_fences: bool) -> tuple[str, dict[str, str]]:
    slots: dict[str, str] = {}

    def protect_fence(match: re.Match[str]) -> str:
        fenced = match.group(0)
        if keep_fences:
            value = fenced
        else:
            first_line_end = fenced.find("\n")
            value = fenced[first_line_end + 1:].rsplit("\n", 1)[0].rstrip("\n")
        return _store_masked_value(value, slots)

    masked = _FENCED_CODE.sub(protect_fence, text)

    def protect_inline(match: re.Match[str]) -> str:
        value = match.group(0) if keep_fences else match.group("body")
        return _store_masked_value(value, slots)

    return _INLINE_CODE.sub(protect_inline, masked), slots


def _find_closing_bracket(text: str, start: int) -> int | None:
    depth = 0
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == "[":
            depth += 1
        elif character == "]":
            if depth == 0:
                return index
            depth -= 1
    return None


def _parse_link_destination(text: str, start: int) -> tuple[str, int] | None:
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return None

    if text[index] == "<":
        destination_start = index + 1
        index = destination_start
        while index < len(text):
            if text[index] == "\\" and index + 1 < len(text):
                index += 2
                continue
            if text[index] == ">":
                destination = text[destination_start:index]
                index += 1
                break
            index += 1
        else:
            return None
    else:
        destination_start = index
        nesting = 0
        while index < len(text):
            character = text[index]
            if character == "\\" and index + 1 < len(text):
                index += 2
                continue
            if character.isspace() and nesting == 0:
                break
            if character == "(":
                nesting += 1
            elif character == ")":
                if nesting == 0:
                    return text[destination_start:index], index
                nesting -= 1
            index += 1
        else:
            return None
        if nesting:
            return None
        destination = text[destination_start:index]

    while index < len(text) and text[index].isspace():
        index += 1
    if index < len(text) and text[index] == ")":
        return destination, index
    if index >= len(text):
        return None

    quote = text[index] if text[index] in {"'", '"'} else ""
    if quote:
        index += 1
        while index < len(text):
            if text[index] == "\\" and index + 1 < len(text):
                index += 2
                continue
            if text[index] == quote:
                index += 1
                break
            index += 1
        else:
            return None
        while index < len(text) and text[index].isspace():
            index += 1
    elif text[index] == "(":
        nesting = 1
        index += 1
        while index < len(text) and nesting:
            if text[index] == "\\" and index + 1 < len(text):
                index += 2
                continue
            if text[index] == "(":
                nesting += 1
            elif text[index] == ")":
                nesting -= 1
            index += 1
        if nesting:
            return None
        while index < len(text) and text[index].isspace():
            index += 1
    if index < len(text) and text[index] == ")":
        return destination, index
    return None


def _protect_markdown_link_targets(text: str, slots: dict[str, str]) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "[" or (index > 0 and text[index - 1] == "\\"):
            output.append(text[index])
            index += 1
            continue
        label_end = _find_closing_bracket(text, index + 1)
        if label_end is None or label_end + 1 >= len(text) or text[label_end + 1] != "(":
            output.append(text[index])
            index += 1
            continue
        parsed = _parse_link_destination(text, label_end + 2)
        if parsed is None:
            output.append(text[index])
            index += 1
            continue
        destination, closing_index = parsed
        label = text[index + 1:label_end].strip()
        suffix = f"（{destination.strip()}）" if destination.strip() else ""
        output.extend((label, _store_masked_value(suffix, slots)))
        index = closing_index + 1
    return "".join(output)


def _restore_masked_values(text: str, slots: dict[str, str]) -> str:
    return _MASK_TOKEN.sub(lambda match: slots.get(match.group(0), match.group(0)), text)


def clean_report_text(value: Any, limit: int = 20000, *, keep_headings: bool = False) -> str:
    """清理人类可读文本中的 Markdown 装饰，保留原有文字与裸星号。"""
    text = str(value or "").strip()[:limit]
    prose, slots = _protect_markdown_code(text, keep_fences=False)
    prose = _clean_execution_noise(prose)
    prose = _protect_markdown_link_targets(prose, slots)
    prose = re.sub(r"(?<!\\)(\*\*|__|~~)(.+?)\1", r"\2", prose)
    prose = re.sub(r"(?<!\*)\*(?!\s)([^*\n]*?\S)\*(?!\*)", r"\1", prose)
    if not keep_headings:
        prose = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", prose)
    prose = re.sub(r"(?m)^\s*[-*+]\s+", "", prose)
    return _restore_masked_values(prose.replace("`", ""), slots)[:limit]


def clean_report_markdown(value: Any, limit: int = 200000) -> str:
    """清理报告正文的格式标记；代码围栏内的证据原样保留。"""
    text = str(value or "").strip()[:limit]
    prose, slots = _protect_markdown_code(text, keep_fences=True)
    prose = _clean_execution_noise(prose)
    prose = _protect_markdown_link_targets(prose, slots)
    prose = re.sub(r"(?<!\\)(\*\*|__|~~)(.+?)\1", r"\2", prose)
    prose = re.sub(r"(?<!\*)\*(?!\s)([^*\n]*?\S)\*(?!\*)", r"\1", prose)
    # Historical agent output sometimes opens bold text at the start of a line
    # without a matching closing marker (for example, "**3.- Location").
    # Remove only that unmatched decorative prefix; fenced code is masked above.
    prose = re.sub(r"(?m)^(\s{0,3})\*\*(?=\S)", r"\1", prose)
    prose = re.sub(r"(?m)^(\s{0,3})\*(\s+)", r"\1-\2", prose)
    return _restore_masked_values(prose.replace("`", ""), slots).strip() + ("\n" if text.endswith("\n") else "")


def clean_report_payload(value: Any, *, key: str | None = None) -> Any:
    """递归清理报告数据中的 Markdown，按代码字段名保留代码原文。"""
    if isinstance(value, dict):
        return {str(name): clean_report_payload(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [clean_report_payload(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [clean_report_payload(item, key=key) for item in value]
    if isinstance(value, str) and key not in _CODE_KEYS:
        return clean_report_markdown(value)
    return value
