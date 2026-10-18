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
    r"没有发现(?:问题|漏洞)|暂无(?:问题|发现|漏洞)?|"
    r"未保留本次请求范围内可确认的问题条目)[。.!！\s]*$"
)
_OFF_SCOPE_ABSENCE = re.compile(
    r"未(?:执行|运行|纳入|包含|提供|采集|检测|覆盖)|未曾(?:执行|运行)|"
    r"(?:无|没有|缺少|缺乏)(?:任何)?[^。；;，,\n]{0,24}(?:证据|结果|覆盖|入口)|"
    r"不可判定|无法判定|not\s+(?:run|executed|performed|included)|missing\s+(?:evidence|coverage)",
    re.I,
)
_OUT_OF_SCOPE_CLAUSE = re.compile(
    r"(?:(?:该|此)(?:结论|问题|发现)\s*)?(?:不属于|不在|超出|超越)(?:本次|当前|此次)?(?:测试)?范围|"
    r"(?:与|和)(?:本次|当前|此次)?范围无关|outside\s+(?:the\s+)?(?:current\s+)?scope",
    re.I,
)
_CONCRETE_FINDING_TITLE = re.compile(
    r"未授权|匿名访问|可(?:读|写|访问|执行|注入)|泄露|暴露|绕过|注入|执行|"
    r"sql|xss|csrf|ssrf|路径穿越|命令|敏感|异常|错误|失败|未发现|不存在|"
    r"超时|返回|拒绝|崩溃|篡改|删除|提权|窃取",
    re.I,
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


def _strip_off_scope_parentheticals(text: str, mode_marker: re.Pattern[str]) -> str:
    """Remove parenthetical notes that explicitly say the unrequested mode was absent."""
    previous = None
    value = text
    while previous != value:
        previous = value
        value = re.sub(
            r"（[^（）]*）|\([^()]*\)|【[^【】]*】|\[[^\[\]]*\]",
            lambda match: (
                ""
                if mode_marker.search(match.group(0))
                and _OFF_SCOPE_ABSENCE.search(match.group(0))
                else match.group(0)
            ),
            value,
        )
    return value


def _scope_off_mode_text(text: str, mode_marker: re.Pattern[str]) -> str:
    """Strip off-scope absence clauses while retaining adjacent in-scope prose."""
    value = _strip_off_scope_parentheticals(text, mode_marker)
    clauses = re.split(r"(?<=[，,；;。.!！])", value)
    kept: list[str] = []
    for clause in clauses:
        if _OUT_OF_SCOPE_CLAUSE.search(clause):
            continue
        if mode_marker.search(clause) and _OFF_SCOPE_ABSENCE.search(clause):
            continue
        if mode_marker.search(clause):
            # A mode mention that is not an absence statement is still out of
            # scope. Keep the requested-mode claim only when it is independently
            # separated into another clause.
            continue
        kept.append(clause)
    return "".join(kept)


def _has_self_sufficient_in_scope_title(
    title: str,
    mode_marker: re.Pattern[str],
    in_scope_marker: re.Pattern[str],
) -> bool:
    """Keep a title-only finding only when it states a concrete in-scope result."""
    if not in_scope_marker.search(title):
        return False
    normalized = _strip_off_scope_parentheticals(title, mode_marker)
    normalized = re.sub(r"白盒|black[\s_-]?box|黑盒|white[\s_-]?box", "", normalized, flags=re.I)
    return bool(_CONCRETE_FINDING_TITLE.search(normalized))


def _title_explicitly_names_off_scope_mode(title: str, mode_marker: re.Pattern[str]) -> bool:
    """Detect an off-scope label outside parenthetical notes about an unrun mode."""
    return bool(mode_marker.search(_strip_off_scope_parentheticals(title, mode_marker)))


def _has_meaningful_finding_body(text: str) -> bool:
    value = re.sub(
        r"(?:证据|说明|模式|requested_mode)\s*[:：]?\s*(?:本次请求模式为\s*)?"
        r"(?:whitebox|blackbox|combined|白盒|黑盒)(?:测试)?[。.!！]?",
        "",
        text,
        flags=re.I,
    )
    value = re.sub(r"^\s*(?:证据|描述|影响|POC/复现|复现)\s*[:：]\s*$", "", value, flags=re.M)
    return bool(value.strip())


def _field_heading_value(title: str) -> str:
    normalized = _heading_text(title).strip()
    if normalized.lower() in _FIELD_HEADINGS:
        return ""
    return re.sub(
        r"^(?:漏洞位置|位置|证据|描述|影响|修复建议|建议|POC(?:/复现)?|复现|"
        r"vulnerable code|严重程度|严重度)\s*[:：/ ]\s*",
        "",
        normalized,
        flags=re.I,
    ).strip()


def _remove_empty_field_headings(text: str) -> str:
    """Drop empty field subheadings left behind after out-of-scope evidence is removed."""
    lines = text.splitlines(keepends=True)
    headings = []
    for index, line in enumerate(lines):
        match = re.match(r"^\s*(#{3,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2)))

    output: list[str] = []
    cursor = 0
    for position, (index, level, title) in enumerate(headings):
        if index < cursor or not _is_field_heading(title):
            continue
        block_end = len(lines)
        for next_index, next_level, _next_title in headings[position + 1:]:
            if next_level <= level:
                block_end = next_index
                break
        output.extend(lines[cursor:index])
        body = _remove_empty_field_headings("".join(lines[index + 1:block_end]))
        if _field_heading_value(title) or _has_meaningful_finding_body(body):
            output.append(lines[index])
            output.append(body)
        cursor = block_end
    output.extend(lines[cursor:])
    return "".join(output)


def _remove_empty_severity_groups(text: str) -> str:
    """Drop severity group headings when filtering removed every finding below them."""
    lines = text.splitlines(keepends=True)
    headings = []
    for index, line in enumerate(lines):
        match = re.match(r"^\s*(#{3,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2)))
    output: list[str] = []
    cursor = 0
    for position, (index, level, title) in enumerate(headings):
        if index < cursor or _heading_text(title).strip().lower() not in _LEVELS:
            continue
        block_end = len(lines)
        for next_index, next_level, _next_title in headings[position + 1:]:
            if next_level <= level:
                block_end = next_index
                break
        output.extend(lines[cursor:index])
        body = _remove_empty_field_headings("".join(lines[index + 1:block_end]))
        if _has_meaningful_finding_body(body):
            output.append(lines[index])
            output.append(body)
        cursor = block_end
    output.extend(lines[cursor:])
    return "".join(output)


def _scope_finding_blocks(
    text: str,
    mode_marker: re.Pattern[str],
    in_scope_marker: re.Pattern[str],
    section_names: set[str],
) -> tuple[str, bool]:
    """Filter only complete finding blocks whose evidence is solely out of scope."""
    replacements: list[tuple[int, int, str]] = []
    removed_any = False
    for section in _markdown_h2_sections(text):
        title = _heading_text(str(section["title"])).rstrip(":：").strip()
        if title not in section_names and not _matches_report_section(title, "问题清单"):
            continue
        start, end = int(section["body_start"]), int(section["end"])
        lines = text[start:end].splitlines(keepends=True)
        headings: list[tuple[int, int, str]] = []
        for index, line in enumerate(lines):
            heading = re.match(r"^\s*(#{3,6})\s+(.+?)\s*#*\s*$", line)
            if heading:
                headings.append((index, len(heading.group(1)), heading.group(2)))

        units: list[tuple[int, int, str, int, str]] = []
        heading_ranges: list[tuple[int, int]] = []
        for position, (index, level, heading_text) in enumerate(headings):
            block_end = len(lines)
            for next_index, next_level, _next_title in headings[position + 1:]:
                if next_level <= level:
                    block_end = next_index
                    break
            descendants = [
                child for child in headings[position + 1:]
                if child[0] < block_end
            ]
            if any(
                not _is_field_heading(child_title)
                for _child_index, _child_level, child_title in descendants
            ):
                continue
            if _heading_text(heading_text).strip().lower() in _LEVELS:
                continue
            units.append((index, block_end, "heading", level, heading_text))
            heading_ranges.append((index, block_end))

        for index, line in enumerate(lines):
            list_item = re.match(r"^(\s*)(?:[-*+]\s+|\d+[.)、]\s+)", line)
            if not list_item or any(left <= index < right for left, right in heading_ranges):
                continue
            indent = len(list_item.group(1).expandtabs(4))
            block_end = index + 1
            while block_end < len(lines):
                next_line = lines[block_end]
                if re.match(r"^\s*#{2,6}\s+", next_line):
                    break
                next_item = re.match(r"^(\s*)(?:[-*+]\s+|\d+[.)、]\s+)", next_line)
                if next_item and len(next_item.group(1).expandtabs(4)) <= indent:
                    break
                if next_line.strip() and len(next_line) - len(next_line.lstrip()) <= indent:
                    break
                block_end += 1
            units.append((index, block_end, "list", indent, ""))

        output: list[str] = []
        cursor = 0
        for index, block_end, kind, level, heading_text in sorted(units, key=lambda unit: unit[0]):
            if index < cursor:
                continue
            output.extend(
                _scope_off_mode_text(line, mode_marker)
                if mode_marker.search(line) and _OFF_SCOPE_ABSENCE.search(line)
                else line
                for line in lines[cursor:index]
            )
            raw_block = "".join(lines[index:block_end])
            if not mode_marker.search(raw_block):
                output.extend(lines[index:block_end])
                cursor = block_end
                continue
            raw_title = heading_text if kind == "heading" else re.sub(
                r"^\s*(?:[-*+]\s+|\d+[.)、]\s+)", "", lines[index]
            ).strip()
            if _title_explicitly_names_off_scope_mode(raw_title, mode_marker):
                removed_any = True
                cursor = block_end
                continue
            sanitized_lines = [
                _scope_off_mode_text(line, mode_marker)
                for line in lines[index:block_end]
            ]
            if kind == "heading":
                sanitized_heading = _scope_off_mode_text(heading_text, mode_marker).strip()
                sanitized_body = _remove_empty_field_headings("".join(sanitized_lines[1:]))
                sanitized_lines = [sanitized_lines[0], *sanitized_body.splitlines(keepends=True)]
                has_in_scope_heading = _has_self_sufficient_in_scope_title(
                    sanitized_heading, mode_marker, in_scope_marker
                )
                if not _has_meaningful_finding_body(sanitized_body) and not has_in_scope_heading:
                    removed_any = True
                    cursor = block_end
                    continue
                sanitized_lines[0] = f"{'#' * level} {sanitized_heading or '本次范围内发现'}\n"
            else:
                sanitized_title = sanitized_lines[0].strip()
                sanitized_body = _remove_empty_field_headings("".join(sanitized_lines[1:]))
                sanitized_lines = [sanitized_lines[0], *sanitized_body.splitlines(keepends=True)]
                has_in_scope_title = _has_self_sufficient_in_scope_title(
                    sanitized_title, mode_marker, in_scope_marker
                )
                if not _has_meaningful_finding_body(sanitized_body) and not has_in_scope_title:
                    removed_any = True
                    cursor = block_end
                    continue
            if not any(line.strip() for line in sanitized_lines):
                removed_any = True
                cursor = block_end
                continue
            output.extend(sanitized_lines)
            cursor = block_end
        output.extend(
            _scope_off_mode_text(line, mode_marker)
            if mode_marker.search(line) and _OFF_SCOPE_ABSENCE.search(line)
            else line
            for line in lines[cursor:]
        )
        body = _remove_empty_severity_groups(_remove_empty_field_headings("".join(output)))
        if removed_any and _matches_report_section(title, "问题清单"):
            remaining = summarize_sandbox_report(f"## 问题清单\n{body}")
            if remaining["total"] is None:
                body = "- 未保留本次请求范围内可确认的问题条目。\n"
        replacements.append((start, end, body))
    for start, end, value in reversed(replacements):
        text = text[:start] + value + text[end:]
    return text, removed_any


def scope_sandbox_report_markdown(report_md: object, requested_mode: object) -> str:
    """Remove pass/fail claims and findings for sandbox modes that were not requested."""
    if not isinstance(report_md, str) or requested_mode not in {"whitebox", "blackbox"}:
        return report_md if isinstance(report_md, str) else ""

    off_scope = "白盒" if requested_mode == "blackbox" else "黑盒"
    off_scope_heading = "白盒结果" if requested_mode == "blackbox" else "黑盒结果"
    replacement = (
        f"## {off_scope_heading}\n\n"
        f"本次请求模式为{'黑盒' if requested_mode == 'blackbox' else '白盒'}，"
        f"{off_scope}验证未纳入范围；不作{off_scope}通过或失败判断，"
        f"未采集{off_scope}证据不计为缺陷。\n\n"
    )

    sections = _markdown_h2_sections(report_md)
    replacements: list[tuple[int, int, str]] = []
    for index, section in enumerate(sections):
        title = _heading_text(str(section["title"])).rstrip(":：").strip()
        if title != off_scope_heading:
            continue
        end = int(section["end"])
        for following in sections[index + 1:]:
            # Replace only this mode's own H2 section. Skipping an unrelated H2
            # (for example a mixed "发现" section) can erase valid in-scope evidence.
            end = int(following["start"])
            break
        replacements.append((int(section["start"]), end, replacement))

    scoped = report_md
    for start, end, value in reversed(replacements):
        scoped = scoped[:start] + value + scoped[end:]

    mode_marker = re.compile(
        r"白盒|white[\s_-]?box" if requested_mode == "blackbox" else r"黑盒|black[\s_-]?box", re.I
    )
    in_scope_marker = re.compile(
        r"黑盒|black[\s_-]?box" if requested_mode == "blackbox" else r"白盒|white[\s_-]?box", re.I
    )
    scoped, _removed_off_scope_findings = _scope_finding_blocks(
        scoped,
        mode_marker,
        in_scope_marker,
        {"问题清单", "发现", "漏洞发现", "安全发现", "测试发现"},
    )

    # Summary paragraphs can repeat an off-scope mode as failed simply because
    # that mode was not run. Remove only the clauses naming that mode, retaining
    # adjacent in-scope conclusions in the same sentence.
    summary_sections = []
    for section in _markdown_h2_sections(scoped):
        title = _heading_text(str(section["title"])).rstrip(":：").strip()
        if title in {"总体结论", "执行结论", "报告摘要", "结论摘要"} or title.endswith("建议"):
            summary_sections.append(section)
    for summary_section in reversed(summary_sections):
        start = int(summary_section["body_start"])
        end = int(summary_section["end"])
        lines = scoped[start:end].splitlines(keepends=True)
        sanitized = [_scope_off_mode_text(line, mode_marker) for line in lines]
        scoped = scoped[:start] + "".join(sanitized) + scoped[end:]
    return scoped


def scope_sandbox_findings(findings: object, requested_mode: object) -> list:
    """Return display-safe legacy structured findings for a single-mode report."""
    if not isinstance(findings, list):
        return []
    if requested_mode not in {"whitebox", "blackbox"}:
        return findings

    off_scope = "whitebox" if requested_mode == "blackbox" else "blackbox"
    mode_marker = re.compile(
        r"白盒|white[\s_-]?box" if requested_mode == "blackbox" else r"黑盒|black[\s_-]?box", re.I
    )
    prose_keys = {
        "title", "name", "summary", "description", "impact", "evidence", "details",
        "steps", "steps_to_reproduce", "repro_steps", "reproduction", "recommendation",
        "remediation", "fix", "fix_advice", "resolution", "proof", "poc",
    }

    def transform(value: object) -> object:
        if isinstance(value, str):
            return _scope_off_mode_text(value, mode_marker)
        if isinstance(value, list):
            return [transform(item) for item in value]
        if isinstance(value, dict):
            return {key: transform(item) for key, item in value.items()}
        return value

    def prose_values(value: object):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in prose_keys:
                    yield from prose_values(item)
        elif isinstance(value, list):
            for item in value:
                yield from prose_values(item)
        elif isinstance(value, str):
            yield value

    scoped = []
    for item in findings:
        if not isinstance(item, dict):
            scoped.append(item)
            continue
        explicit_mode = next(
            (str(item[key]).strip().lower() for key in ("test_mode", "requested_mode", "mode")
             if isinstance(item.get(key), str)),
            None,
        )
        if explicit_mode == off_scope:
            continue
        title = next(
            (item[key] for key in ("title", "name") if isinstance(item.get(key), str)),
            "",
        )
        if title and _title_explicitly_names_off_scope_mode(title, mode_marker):
            continue
        original_prose = list(prose_values(item))
        cleaned = {
            key: transform(value) if str(key).lower() in prose_keys else value
            for key, value in item.items()
        }
        only_off_scope_absence = any(
            mode_marker.search(value) and _OFF_SCOPE_ABSENCE.search(value)
            for value in original_prose
        )
        remaining_detail = any(
            transformed.strip()
            for key in prose_keys - {"title", "name"}
            for transformed in prose_values({key: cleaned.get(key)})
            if isinstance(transformed, str)
        )
        if only_off_scope_absence and not remaining_detail:
            continue
        scoped.append(cleaned)
    return scoped


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
    severity_groups: list[tuple[int, int, int, str]] = []
    for offset, (index, level, title) in enumerate(headings):
        normalized_title = _heading_text(title).rstrip(":：").lower()
        if normalized_title not in _LEVELS:
            continue
        end = len(section)
        for child in headings[offset + 1:]:
            if child[1] <= level:
                end = child[0]
                break
        severity_groups.append((index, end, level, _LEVELS[normalized_title]))

    def inherited_severity(index: int, item_level: int | None = None) -> str | None:
        candidates = [
            group for group in severity_groups
            if group[0] < index < group[1] and (item_level is None or group[2] < item_level)
        ]
        return max(candidates, key=lambda group: (group[2], group[0]))[3] if candidates else None

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
            entries.append((title, section[index + 1:end], inherited_severity(index, level)))
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
                entries.append((title, [], inherited_severity(index)))
        result["basis"] = "report_headings"
    else:
        for line in section:
            match = re.match(r"^(?:[-*+] |\d+[.)]\s+)(.+)", line)
            if match:
                title = match.group(1).replace("**", "").strip()
                if not _FIELD.match(title) and not _EMPTY.fullmatch(title):
                    entries.append((title, [], None))
        result["basis"] = "report_list"
    meaningful = [line.strip() for line in section if line.strip()]
    if not entries and not any(_EMPTY.fullmatch(re.sub(r"^[-*+]\s+", "", line)) for line in meaningful):
        result["basis"] = "unavailable"
        return result
    result["total"] = len(entries)
    result["unclassified"] = 0
    for title, body, group_level in entries:
        level = _severity(title, body) or group_level
        if level:
            result["severity_counts"][level] += 1
        else:
            result["unclassified"] += 1
    return result
