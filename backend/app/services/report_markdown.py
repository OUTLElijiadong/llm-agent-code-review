"""将报告 Markdown 安全转换为 HTML、PDF 与 Word 可消费的结构。"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html import escape
from typing import Any, Iterable
from urllib.parse import urlsplit

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
    rows: tuple[tuple[tuple[MarkdownSegment, ...], ...], ...] = ()


def markdown_to_html(value: str | None) -> str:
    """以安全、可读的子集渲染报告 Markdown；不输出原始 HTML 或残留强调符号。"""
    return markdown_to_safe_html(value)


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
            # Markdown-It leaves unmatched emphasis delimiters as plain text.
            # They are formatting debris in generated reports, so never leak
            # them into PDF/Word as literal AI-style asterisks. Inline code is
            # handled separately and remains byte-for-byte intact.
            text = _strip_dangling_emphasis_markers(token.content)
            if text:
                segments.append(MarkdownSegment(text, bold=bold, italic=italic))
        elif token.type in {"softbreak", "hardbreak"}:
            segments.append(MarkdownSegment("\n", bold=bold, italic=italic))
        elif token.type == "s_open":
            italic = True
        elif token.type == "s_close":
            italic = False
    return tuple(segments)


def markdown_to_blocks(value: str | None) -> list[MarkdownBlock]:
    """解析标题、段落、列表、引用、表格与代码块，供 PDF 和 Word 原生排版。"""
    blocks: list[MarkdownBlock] = []
    source, tables = _replace_markdown_tables(value or "")
    list_stack: list[dict[str, int | bool | None]] = []
    quote_depth = 0
    current_kind = "paragraph"
    current_level = 0
    current_list_depth = 0
    current_number: int | None = None
    for token in _MARKDOWN.parse(source):
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
            table_rows = tables.get(token.content.strip())
            if table_rows is not None:
                blocks.append(MarkdownBlock(kind="table", rows=table_rows))
                continue
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
        wrapped_text = _wrap_long_ascii_tokens(segment.text)
        value = "<br/>".join(escape(part) for part in wrapped_text.split("\n"))
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


def _wrap_long_ascii_tokens(value: str, limit: int = 40) -> str:
    """Add PDF line breaks to long identifiers and URLs with no wrap points."""
    def wrap(match: re.Match[str]) -> str:
        token = match.group(0)
        return "\n".join(token[index:index + limit] for index in range(0, len(token), limit))

    token_pattern = re.compile(rf"[A-Za-z0-9_./:-]{{{limit + 1},}}")
    return token_pattern.sub(wrap, value)


def _strip_dangling_emphasis_markers(value: str) -> str:
    """Remove unmatched bold delimiters without changing literal star syntax."""
    value = re.sub(r"(^|\s)\*{2,}(?=\S)", r"\1", value)
    return re.sub(r"(?<=\S)\*{2,}(?=$|\s)", "", value)


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


_INLINE = re.compile(
    r"(?P<bold>\*\*(?P<bold_star>.+?)\*\*|__(?P<bold_under>.+?)__)"
    r"|(?P<italic>(?<!\*)\*(?!\*)(?P<italic_star>.+?)(?<!\*)\*(?!\*)"
    r"|(?<!_)_(?!_)(?P<italic_under>.+?)(?<!_)_(?!_))"
    r"|(?P<code>`(?P<code_text>[^`]+)`)|(?P<link>\[(?P<link_text>[^\]]+)\]\((?P<link_href>(?:[^()]|\([^()]*\))+?)\))"
)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_UNORDERED = re.compile(r"^\s{0,3}[-*+]\s+(.+)$")
_ORDERED = re.compile(r"^\s{0,3}\d+[.)、]\s+(.+)$")
_TABLE_SEPARATOR_CELL = re.compile(r"^\s*:?-{3,}:?\s*$")


def _split_table_row(line: str) -> list[str] | None:
    """Split a pipe-delimited Markdown row, respecting escaped pipes and code spans."""
    value = line.strip()
    if "|" not in value:
        return None
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and not value.endswith("\\|"):
        value = value[:-1]
    cells: list[str] = []
    cell: list[str] = []
    escaped = False
    code_ticks = 0
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and not escaped:
            escaped = True
            cell.append(char)
            index += 1
            continue
        if char == "`" and not escaped:
            tick_end = index + 1
            while tick_end < len(value) and value[tick_end] == "`":
                tick_end += 1
            ticks = tick_end - index
            code_ticks = 0 if code_ticks == ticks else (ticks if code_ticks == 0 else code_ticks)
            cell.extend(value[index:tick_end])
            index = tick_end
            escaped = False
            continue
        if char == "|" and not escaped and code_ticks == 0:
            cells.append("".join(cell).replace("\\|", "|").strip())
            cell.clear()
        else:
            cell.append(char)
        escaped = False
        index += 1
    cells.append("".join(cell).replace("\\|", "|").strip())
    return cells


def _replace_markdown_tables(
    markdown: str,
) -> tuple[str, dict[str, tuple[tuple[tuple[MarkdownSegment, ...], ...], ...]]]:
    """Protect GFM-style pipe tables from the CommonMark parser as table blocks."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    tables: dict[str, tuple[tuple[tuple[MarkdownSegment, ...], ...], ...]] = {}
    fence_char: str | None = None
    fence_length = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker, remainder = fence.group(1), fence.group(2)
            if fence_char is None:
                if marker[0] != "`" or "`" not in remainder:
                    fence_char, fence_length = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length and not remainder.strip():
                fence_char, fence_length = None, 0
            output.append(line)
            index += 1
            continue
        if fence_char is not None:
            output.append(line)
            index += 1
            continue

        header = _split_table_row(line)
        separator = _split_table_row(lines[index + 1]) if index + 1 < len(lines) else None
        if (
            header
            and separator
            and len(header) == len(separator)
            and all(_TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in separator)
        ):
            raw_rows = [header]
            index += 2
            while index < len(lines):
                row = _split_table_row(lines[index])
                if not lines[index].strip() or not row or len(row) != len(header):
                    break
                raw_rows.append(row)
                index += 1
            marker = f"PRISM_REPORT_TABLE_BLOCK_{len(tables)}_END"
            tables[marker] = tuple(
                tuple(
                    tuple(
                        MarkdownSegment(
                            text=str(run.get("text") or ""),
                            bold=bool(run.get("bold")),
                            italic=bool(run.get("italic")),
                            code=bool(run.get("code")),
                        )
                        for run in parse_inline(cell)
                    )
                    for cell in row
                )
                for row in raw_rows
            )
            output.append(marker)
            continue
        output.append(line)
        index += 1
    return "\n".join(output), tables


def _safe_href(value: str) -> str | None:
    href = value.strip()
    parsed = urlsplit(href)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return href
    if not parsed.scheme and href.startswith("/") and not href.startswith("//"):
        return href
    return None


def parse_inline(text: str) -> list[dict[str, Any]]:
    """Return plain inline runs, preserving emphasis/code/link semantics."""
    source = str(text or "")
    runs: list[dict[str, Any]] = []
    cursor = 0
    for match in _INLINE.finditer(source):
        if match.start() > cursor:
            runs.append({"text": source[cursor:match.start()]})
        if match.group("bold"):
            content = match.group("bold_star") or match.group("bold_under") or ""
            runs.append({"text": content, "bold": True})
        elif match.group("italic"):
            content = match.group("italic_star") or match.group("italic_under") or ""
            runs.append({"text": content, "italic": True})
        elif match.group("code"):
            runs.append({"text": match.group("code_text") or "", "code": True})
        elif match.group("link"):
            label = match.group("link_text") or ""
            href = _safe_href(match.group("link_href") or "")
            runs.append({"text": label, **({"href": href} if href else {})})
        cursor = match.end()
    if cursor < len(source):
        runs.append({"text": source[cursor:]})
    # Emphasis markers that did not form a valid token are almost always model
    # formatting debris. Keep code spans literal, but never leak raw Markdown
    # asterisks into the reader-facing report.
    for run in runs:
        if not run.get("code"):
            run["text"] = str(run.get("text") or "").replace("*", "")
    return runs


def parse_markdown_blocks(markdown: str | None) -> list[dict[str, Any]]:
    """Parse report prose into headings, paragraphs, lists, tables, quotes, and code."""
    lines = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []
    list_kind: str | None = None
    list_items: list[list[dict[str, Any]]] = []
    code_lines: list[str] | None = None

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append({"type": "paragraph", "runs": parse_inline("\n".join(paragraph))})
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_kind
        if list_kind and list_items:
            # Store a snapshot: clearing the working buffer must not erase the block.
            blocks.append({"type": list_kind, "items": list_items.copy()})
        list_kind = None
        list_items.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        if code_lines is not None:
            if line.strip().startswith("```"):
                blocks.append({"type": "code", "text": "\n".join(code_lines)})
                code_lines = None
            else:
                code_lines.append(line)
            index += 1
            continue
        if line.strip().startswith("```"):
            flush_paragraph()
            flush_list()
            code_lines = []
            index += 1
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            index += 1
            continue
        header_cells = _split_table_row(line)
        separator_cells = _split_table_row(lines[index + 1]) if index + 1 < len(lines) else None
        if (
            header_cells
            and separator_cells
            and len(header_cells) == len(separator_cells)
            and all(_TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in separator_cells)
        ):
            flush_paragraph()
            flush_list()
            rows = [header_cells]
            index += 2
            while index < len(lines):
                row = _split_table_row(lines[index])
                if not lines[index].strip() or not row or len(row) != len(header_cells):
                    break
                rows.append(row)
                index += 1
            blocks.append({"type": "table", "rows": [[parse_inline(cell) for cell in row] for row in rows]})
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            flush_list()
            blocks.append({"type": "heading", "level": len(heading.group(1)), "runs": parse_inline(heading.group(2))})
            index += 1
            continue
        ordered = _ORDERED.match(line)
        unordered = _UNORDERED.match(line)
        if ordered or unordered:
            flush_paragraph()
            new_kind = "ordered_list" if ordered else "unordered_list"
            if list_kind != new_kind:
                flush_list()
                list_kind = new_kind
            list_items.append(parse_inline((ordered or unordered).group(1)))
            index += 1
            continue
        if line.lstrip().startswith(">"):
            flush_paragraph()
            flush_list()
            blocks.append({"type": "quote", "runs": parse_inline(line.lstrip()[1:].lstrip())})
            index += 1
            continue
        if re.fullmatch(r"\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*", line):
            flush_paragraph()
            flush_list()
            blocks.append({"type": "rule"})
            index += 1
            continue
        flush_list()
        paragraph.append(line)
        index += 1

    if code_lines is not None:
        blocks.append({"type": "code", "text": "\n".join(code_lines)})
    flush_paragraph()
    flush_list()
    return blocks


def _inline_html(runs: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for run in runs:
        text = html.escape(str(run.get("text") or "")).replace("\n", "<br>")
        if "href" in run:
            href = html.escape(str(run["href"]), quote=True)
            text = f'<a href="{href}">{text}</a>'
        if run.get("code"):
            text = f"<code>{text}</code>"
        if run.get("italic"):
            text = f"<em>{text}</em>"
        if run.get("bold"):
            text = f"<strong>{text}</strong>"
        parts.append(text)
    return "".join(parts)


def markdown_to_safe_html(markdown: str | None) -> str:
    """Render supported Markdown into escaped, safe HTML for Jinja/PDF."""
    rendered: list[str] = []
    for block in parse_markdown_blocks(markdown):
        kind = block["type"]
        if kind == "heading":
            level = min(int(block.get("level") or 2), 4)
            rendered.append(f"<h{level}>{_inline_html(block['runs'])}</h{level}>")
        elif kind == "paragraph":
            rendered.append(f"<p>{_inline_html(block['runs'])}</p>")
        elif kind in {"unordered_list", "ordered_list"}:
            tag = "ul" if kind == "unordered_list" else "ol"
            items_html = "".join(f"<li>{_inline_html(item)}</li>" for item in block["items"])
            rendered.append(f"<{tag}>{items_html}</{tag}>")
        elif kind == "table":
            rows = block["rows"]
            header = "".join(f"<th>{_inline_html(cell)}</th>" for cell in rows[0])
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline_html(cell)}</td>" for cell in row) + "</tr>"
                for row in rows[1:]
            )
            rendered.append(
                f'<table class="report-summary-table"><thead><tr>{header}</tr></thead>'
                f"<tbody>{body}</tbody></table>"
            )
        elif kind == "quote":
            rendered.append(f"<blockquote>{_inline_html(block['runs'])}</blockquote>")
        elif kind == "code":
            rendered.append(f"<pre><code>{html.escape(block['text'])}</code></pre>")
        elif kind == "rule":
            rendered.append("<hr>")
    return "\n".join(rendered)
