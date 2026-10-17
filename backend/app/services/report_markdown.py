"""Render the small, predictable Markdown subset used by generated reports.

This module intentionally has no runtime dependency beyond MarkupSafe. Report
summaries are model-authored Markdown, so every text fragment is escaped before
HTML tags are emitted; only the tags built here are considered safe.
"""
from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlsplit


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
            rendered.append(f"<{tag}>" + "".join(f"<li>{_inline_html(item)}</li>" for item in block["items"]) + f"</{tag}>")
        elif kind == "table":
            rows = block["rows"]
            header = "".join(f"<th>{_inline_html(cell)}</th>" for cell in rows[0])
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline_html(cell)}</td>" for cell in row) + "</tr>"
                for row in rows[1:]
            )
            rendered.append(f'<table class="report-summary-table"><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>')
        elif kind == "quote":
            rendered.append(f"<blockquote>{_inline_html(block['runs'])}</blockquote>")
        elif kind == "code":
            rendered.append(f"<pre><code>{html.escape(block['text'])}</code></pre>")
        elif kind == "rule":
            rendered.append("<hr>")
    return "\n".join(rendered)
