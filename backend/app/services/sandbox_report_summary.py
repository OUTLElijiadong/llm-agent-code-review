"""按沙箱报告章节统计发现条目；不从描述推断严重度或确诊漏洞。"""
from __future__ import annotations

import re

_LEVELS = {"严重": "严重", "危急": "严重", "critical": "严重", "高": "高", "高危": "高",
           "high": "高", "中": "中", "中危": "中", "medium": "中", "低": "低", "低危": "低", "low": "低"}
_LEVEL_PATTERN = "|".join(sorted(_LEVELS, key=len, reverse=True))
_FIELD = re.compile(
    r"^(?:漏洞位置|位置|证据|描述|影响|修复建议|建议|POC|复现|vulnerable code|严重程度|严重度)\s*[:：/ ]", re.I,
)
_FIELD_HEADINGS = {"漏洞位置", "位置", "证据", "描述", "影响", "修复建议", "建议", "poc", "poc/复现",
                   "复现", "vulnerable code", "严重程度", "严重度"}
_EMPTY = re.compile(
    r"^(?:无(?:问题|发现|漏洞)?|未发现(?:确定性执行失败|问题|漏洞)|"
    r"没有发现(?:问题|漏洞)|暂无(?:问题|发现|漏洞)?)[。.!！\s]*$"
)


def _heading_text(title: str) -> str:
    normalized = title.replace("**", "").strip().rstrip("# ").strip()
    return re.sub(r"^\d+(?:[.．、)）]\d+)*[.．、)）]?\s*", "", normalized).strip()


def _markdown_h2_sections(report_md: str) -> list[dict[str, object]]:
    """Return real H2 sections and source offsets, ignoring headings inside fenced code."""
    sections: list[dict[str, object]] = []
    active: dict[str, object] | None = None
    fence_char: str | None = None
    fence_length = 0
    offset = 0

    for raw_line in report_md.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            token, remainder = marker.group(1), marker.group(2)
            if fence_char is None:
                if token[0] != "`" or "`" not in remainder:
                    fence_char = token[0]
                    fence_length = len(token)
                offset += len(raw_line)
                continue
            if token[0] == fence_char and len(token) >= fence_length and not remainder.strip():
                fence_char = None
                fence_length = 0
            offset += len(raw_line)
            continue
        if fence_char is None:
            heading = re.match(r"^ {0,3}##[ \t]+(?!#)(.+?)\s*#*[ \t]*$", line)
            if heading:
                if active is not None:
                    active["end"] = offset
                    sections.append(active)
                active = {
                    "title": heading.group(1).strip(),
                    "start": offset,
                    "body_start": offset + len(raw_line),
                    "end": len(report_md),
                }
        offset += len(raw_line)

    if active is not None:
        active["end"] = len(report_md)
        sections.append(active)
    return sections


def _matches_report_section(title: str, expected: str) -> bool:
    normalized = _heading_text(title).rstrip(":：").strip()
    if normalized == expected:
        return True
    if not normalized.startswith(expected):
        return False
    return normalized[len(expected):len(expected) + 1] in {" ", "（", "(", "：", ":", "-", "—"}


def _outside_fenced_lines(text: str) -> list[str]:
    """Return lines outside fenced code blocks for semantic report parsing."""
    result: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            token, remainder = marker.group(1), marker.group(2)
            if fence_char is None:
                if token[0] != "`" or "`" not in remainder:
                    fence_char = token[0]
                    fence_length = len(token)
                continue
            if token[0] == fence_char and len(token) >= fence_length and not remainder.strip():
                fence_char = None
                fence_length = 0
            continue
        if fence_char is None:
            result.append(line)
    return result


def _is_field_heading(title: str) -> bool:
    normalized = _heading_text(title).rstrip(":：").lower()
    return normalized in _FIELD_HEADINGS or bool(_FIELD.match(normalized))


def _severity(title: str, body: list[str]) -> str | None:
    text = _heading_text(title)
    labels = []
    # 只接受标题开头的独立标签或明示严重度字段，描述中的“高频”不能分级。
    match = re.match(rf"^(?:[\[【（(]\s*({_LEVEL_PATTERN})\s*[\]】）)]|({_LEVEL_PATTERN})\s*[:：·—-])", text, re.I)
    if match:
        labels.append(_LEVELS[(match.group(1) or match.group(2)).lower()])
    for line in body:
        plain = re.sub(r"^\s*[-*+]\s+", "", line).replace("**", "").strip()
        match = re.match(rf"^严重(?:程度|度)\s*[:：]\s*({_LEVEL_PATTERN})\s*[。.]?$", plain, re.I)
        if match:
            labels.append(_LEVELS[match.group(1).lower()])
    return labels[0] if labels and len(set(labels)) == 1 else None


def summarize_sandbox_report(report_md: object) -> dict:
    """返回报告声明的条目数；未知格式保留 None，字段行、围栏不参与计数。"""
    result = {"source": "sandbox_report", "basis": "unavailable", "total": None,
              "severity_counts": {level: 0 for level in ("严重", "高", "中", "低")}, "unclassified": None}
    if not isinstance(report_md, str):
        return result
    issue_section = next(
        (item for item in _markdown_h2_sections(report_md)
         if _matches_report_section(str(item["title"]), "问题清单")),
        None,
    )
    if issue_section is None:
        return result
    section = _outside_fenced_lines(report_md[int(issue_section["body_start"]):int(issue_section["end"])])

    headings = []
    for index, line in enumerate(section):
        match = re.match(r"^(#{3,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2)))
    entries = []
    if headings:
        heading_entry_ranges: list[tuple[int, int]] = []
        for offset, (index, level, title) in enumerate(headings):
            normalized_title = _heading_text(title).rstrip(":：").lower()
            if _is_field_heading(title):
                continue
            end = len(section)
            descendants = []
            for child in headings[offset + 1:]:
                if child[1] <= level:
                    end = child[0]
                    break
                descendants.append(child)
            # 字段子标题属于当前发现；真正含子发现的分组标题不重复计数。
            if any(not _is_field_heading(child[2]) for child in descendants):
                continue
            if normalized_title in _LEVELS or _EMPTY.fullmatch(title.strip()):
                continue
            entries.append((title, section[index + 1:end]))
            heading_entry_ranges.append((index, end))
        # Some model reports mix titled findings and root-level list findings.
        # Count only list items outside a finding heading's body; bullets inside
        # a heading remain evidence/fields for that finding.
        for index, line in enumerate(section):
            match = re.match(r"^(?:[-*+] |\d+[.)]\s+)(.+)", line)
            if not match or any(start < index < end for start, end in heading_entry_ranges):
                continue
            title = match.group(1).replace("**", "").strip()
            if not _FIELD.match(title) and not _EMPTY.fullmatch(title):
                entries.append((title, []))
        result["basis"] = "report_headings"
    else:
        for line in section:
            match = re.match(r"^(?:[-*+] |\d+[.)]\s+)(.+)", line)
            if match:
                title = match.group(1).replace("**", "").strip()
                if not _FIELD.match(title) and not _EMPTY.fullmatch(title):
                    entries.append((title, []))
        result["basis"] = "report_list"
    meaningful = [line.strip() for line in section if line.strip()]
    if not entries and not any(_EMPTY.fullmatch(re.sub(r"^[-*+]\s+", "", line)) for line in meaningful):
        result["basis"] = "unavailable"
        return result
    result["total"] = len(entries)
    result["unclassified"] = 0
    for title, body in entries:
        level = _severity(title, body)
        if level:
            result["severity_counts"][level] += 1
        else:
            result["unclassified"] += 1
    return result
