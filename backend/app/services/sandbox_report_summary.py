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
_EMPTY = re.compile(r"^(?:无(?:问题|发现|漏洞)?|未发现(?:确定性执行失败|问题|漏洞)|没有发现(?:问题|漏洞))[。.!！\s]*$")


def _heading_text(title: str) -> str:
    return re.sub(r"^\d+[.、)）]\s*", "", title.replace("**", "").strip()).strip()


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
    section = []
    active = False
    fence = None
    for line in report_md.splitlines():
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        heading = re.match(r"^#{1,2}\s+(.+?)\s*#*\s*$", line)
        if heading:
            if active:
                break
            if re.match(r"^问题清单(?:\s|[（(:：]|$)", heading.group(1)):
                active = True
            continue
        if active:
            section.append(line)
    if not active:
        return result

    headings = []
    for index, line in enumerate(section):
        match = re.match(r"^(#{3,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2)))
    entries = []
    if headings:
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
