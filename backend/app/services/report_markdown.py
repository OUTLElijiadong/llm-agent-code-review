"""将报告 Markdown 安全转换为 HTML、PDF 与 Word 可消费的结构。"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Iterable

from markdown_it import MarkdownIt

_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False})


@dataclass(frozen=True)
class MarkdownSegment:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


@dataclass(frozen=True)
class MarkdownBlock:
    kind: str
    segments: tuple[MarkdownSegment, ...] = ()
    level: int = 0
    list_depth: int = 0
    number: int | None = None


def markdown_to_html(value: str | None) -> str:
    """解析常用 Markdown 结构；禁用原始 HTML，外层导出器仍负责最终净化。"""
    return _MARKDOWN.render(value or "")


def _inline_segments(tokens: Iterable) -> tuple[MarkdownSegment, ...]:
    bold = italic = False
    segments: list[MarkdownSegment] = []
    for token in tokens or ():
        if token.type == "strong_open":
            bold = True
        elif token.type == "strong_close":
            bold = False
        elif token.type == "em_open":
            italic = True
        elif token.type == "em_close":
            italic = False
        elif token.type == "code_inline":
            segments.append(MarkdownSegment(token.content, bold=bold, italic=italic, code=True))
        elif token.type in {"text", "html_inline", "image"} and token.content:
            segments.append(MarkdownSegment(token.content, bold=bold, italic=italic))
        elif token.type in {"softbreak", "hardbreak"}:
            segments.append(MarkdownSegment("\n", bold=bold, italic=italic))
        elif token.type == "s_open":
            italic = True
        elif token.type == "s_close":
            italic = False
    return tuple(segments)


def markdown_to_blocks(value: str | None) -> list[MarkdownBlock]:
    """解析标题、段落、列表、引用与代码块，供 PDF 和 Word 原生排版。"""
    blocks: list[MarkdownBlock] = []
    list_stack: list[dict[str, int | bool | None]] = []
    quote_depth = 0
    current_kind = "paragraph"
    current_level = 0
    current_list_depth = 0
    current_number: int | None = None
    for token in _MARKDOWN.parse(value or ""):
        if token.type == "bullet_list_open":
            list_stack.append({"ordered": False, "next": 1, "item": None})
        elif token.type == "ordered_list_open":
            start = int((token.attrGet("start") or "1"))
            list_stack.append({"ordered": True, "next": start, "item": None})
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            if list_stack:
                list_stack.pop()
        elif token.type == "list_item_open" and list_stack:
            list_stack[-1]["item"] = int(list_stack[-1]["next"] or 1)
            list_stack[-1]["next"] = int(list_stack[-1]["item"] or 1) + 1
        elif token.type == "blockquote_open":
            quote_depth += 1
        elif token.type == "blockquote_close":
            quote_depth = max(0, quote_depth - 1)
        elif token.type == "heading_open":
            current_kind = "heading"
            current_level = int(token.tag[1:]) if token.tag.startswith("h") else 2
        elif token.type == "paragraph_open":
            current_kind = "ordered" if list_stack and list_stack[-1]["ordered"] else (
                "bullet" if list_stack else "quote" if quote_depth else "paragraph"
            )
            current_level = 0
            current_list_depth = quote_depth if current_kind == "quote" else len(list_stack)
            current_number = int(list_stack[-1]["item"] or 1) if list_stack and current_kind == "ordered" else None
        elif token.type == "inline":
            blocks.append(MarkdownBlock(
                kind=current_kind,
                segments=_inline_segments(token.children),
                level=current_level,
                list_depth=current_list_depth,
                number=current_number,
            ))
        elif token.type in {"fence", "code_block"}:
            blocks.append(MarkdownBlock("code", (MarkdownSegment(token.content, code=True),)))
        elif token.type == "hr":
            blocks.append(MarkdownBlock("rule"))
    return blocks


def plain_text(segments: Iterable[MarkdownSegment]) -> str:
    return "".join(segment.text for segment in segments)


def reportlab_markup(
    segments: Iterable[MarkdownSegment],
    *,
    code_font: str = "Courier",
    code_non_ascii_font: str | None = None,
) -> str:
    """生成只含安全文字和固定样式标签的 ReportLab 段落内容。"""
    result: list[str] = []
    for segment in segments:
        value = "<br/>".join(escape(part) for part in segment.text.split("\n"))
        if segment.code:
            if code_non_ascii_font:
                value = "".join(
                    f'<font name="{code_non_ascii_font}">{part}</font>' if not part.isascii() else part
                    for part in _split_ascii(value)
                )
            value = f'<font name="{code_font}">{value}</font>'
        if segment.italic:
            value = f"<i>{value}</i>"
        if segment.bold:
            value = f"<b>{value}</b>"
        result.append(value)
    return "".join(result)


def _split_ascii(value: str) -> list[str]:
    """按 ASCII 与非 ASCII 字符分段，供不支持中文的等宽字体回退使用。"""
    if not value:
        return []
    parts: list[str] = []
    start = 0
    ascii_state = value[0].isascii()
    for index, character in enumerate(value[1:], start=1):
        if character.isascii() != ascii_state:
            parts.append(value[start:index])
            start = index
            ascii_state = character.isascii()
    parts.append(value[start:])
    return parts
