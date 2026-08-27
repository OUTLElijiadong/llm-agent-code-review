"""Normalize and N-way merge review findings from every analysis source."""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, replace
from typing import Iterable, List, Optional

from app.ai.cvss import normalize_cvss
from app.ai.result_parser import Issue, normalize_severity
from app.ai.static_analyzer import Finding

_LINE_PROXIMITY = 2
_SEVERITY_RANK = {"低": 1, "中": 2, "高": 3, "严重": 4}
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]")


def merge_findings_and_issues(
    findings: List[Finding],
    issues: List[Issue],
    file_id: int,
    code: Optional[str] = None,
    language: Optional[str] = None,
) -> List[Issue]:
    """N-way deduplicate static and LLM results for one file.

    Matching requires a common vulnerability identity plus a defensible code
    anchor. Merely sharing a CWE on adjacent lines is not enough, which keeps
    separate sinks separate. Every contributing source is retained in
    ``source_details`` while the legacy ``source`` field remains a compact
    static/regex/llm/hybrid value.
    """
    candidates = [finding_to_issue(item) for item in findings]
    candidates.extend(_copy_issue(item) for item in issues)
    if not candidates:
        return []

    anchor_index = (
        _build_anchor_index(code, language=language, candidates=candidates)
        if code
        else _AnchorIndex.empty()
    )
    candidates = _attach_code_anchors(candidates, anchor_index)

    candidates.sort(key=_candidate_sort_key)
    clusters: List[List[Issue]] = []
    for candidate in candidates:
        best_cluster: Optional[List[Issue]] = None
        best_score = -1.0
        for cluster in clusters:
            score = _cluster_match_score(cluster, candidate)
            if score > best_score:
                best_cluster = cluster
                best_score = score
        if best_cluster is not None and best_score >= 1.0:
            best_cluster.append(candidate)
        else:
            clusters.append([candidate])

    merged = [_merge_cluster(cluster, file_id, anchor_index) for cluster in clusters]
    merged.sort(key=lambda item: (item.line_number or 0, item.cwe or "", item.title or ""))
    return merged


def finding_to_issue(finding: Finding) -> Issue:
    """Convert a Finding to Issue without losing provenance or CVSS metadata."""
    cvss_score, cvss_vector, cvss_version, cvss_source = normalize_cvss(
        finding.cvss_score,
        finding.cvss_vector,
    )
    return Issue(
        line_number=finding.line_number,
        end_line=finding.end_line,
        issue_type=finding.issue_type,
        severity=normalize_severity(finding.severity),
        title=finding.title or None,
        description=finding.description,
        suggestion=finding.suggestion or None,
        fixed_code=finding.fixed_code or None,
        owasp=finding.owasp,
        cwe=finding.cwe,
        evidence=finding.evidence,
        exploit_scenario=finding.exploit_scenario,
        references=list(finding.references or []),
        confidence=finding.confidence,
        cvss_score=cvss_score,
        cvss_vector=cvss_vector,
        cvss_version=cvss_version,
        cvss_source=cvss_source,
        compliance_mapping={key: list(values) for key, values in (finding.compliance_mapping or {}).items()},
        remediation=finding.remediation,
        source=finding.source or "static",
        static_rule_hits=max(0, int(finding.static_rule_hits or 0)),
        source_details=[dict(item) for item in (finding.source_details or []) if isinstance(item, dict)],
        confirmation_count=max(1, int(finding.confirmation_count or 1)),
        finding_fingerprint=finding.finding_fingerprint,
        source_anchor=finding.source_anchor or "",
        column_start=finding.column_start,
        column_end=finding.column_end,
    )


def _copy_issue(issue: Issue) -> Issue:
    cvss_score, cvss_vector, cvss_version, cvss_source = normalize_cvss(
        issue.cvss_score,
        issue.cvss_vector,
    )
    return Issue(
        line_number=issue.line_number,
        end_line=issue.end_line,
        issue_type=issue.issue_type,
        severity=normalize_severity(issue.severity),
        title=issue.title,
        description=issue.description,
        suggestion=issue.suggestion,
        fixed_code=issue.fixed_code,
        owasp=issue.owasp,
        cwe=issue.cwe,
        evidence=issue.evidence,
        exploit_scenario=issue.exploit_scenario,
        references=list(issue.references or []),
        confidence=issue.confidence,
        cvss_score=cvss_score,
        cvss_vector=cvss_vector,
        cvss_version=cvss_version,
        cvss_source=cvss_source,
        compliance_mapping={key: list(values) for key, values in (issue.compliance_mapping or {}).items()},
        remediation=issue.remediation,
        source=issue.source or "llm",
        static_rule_hits=max(0, int(issue.static_rule_hits or 0)),
        source_details=[dict(item) for item in (issue.source_details or []) if isinstance(item, dict)],
        confirmation_count=max(1, int(issue.confirmation_count or 1)),
        finding_fingerprint=issue.finding_fingerprint,
        source_anchor=issue.source_anchor or "",
        column_start=issue.column_start,
        column_end=issue.column_end,
    )


def _candidate_sort_key(issue: Issue) -> tuple:
    family_priority = {"static": 0, "regex": 0, "llm": 1}.get(_source_family(issue.source), 2)
    return (
        issue.line_number or 0,
        family_priority,
        issue.source_anchor or "",
        _normalize_evidence(issue.evidence),
        -float(issue.confidence or 0.0),
        issue.title or "",
        issue.description or "",
    )


@dataclass(frozen=True)
class _CodeAnchor:
    """源码中的一个可审计接收点。"""

    anchor: str
    line_number: int
    end_line: int
    column_start: Optional[int]
    column_end: Optional[int]
    normalized_segment: str
    cwe: str


@dataclass(frozen=True)
class _AnchorIndex:
    records: tuple[_CodeAnchor, ...]
    by_anchor: dict[str, _CodeAnchor]

    @classmethod
    def empty(cls) -> "_AnchorIndex":
        return cls(records=(), by_anchor={})


def _build_anchor_index(
    code: Optional[str],
    *,
    language: Optional[str] = None,
    candidates: Iterable[Issue] = (),
) -> _AnchorIndex:
    """建立确定性源码索引：Python AST 优先，其次是完整源码证据。"""
    if not code:
        return _AnchorIndex.empty()
    candidate_list = list(candidates)
    records: list[_CodeAnchor] = []
    normalized_language = (language or "").strip().lower()
    if not normalized_language or normalized_language in {"py", "python", "python3"}:
        try:
            tree = ast.parse(code)
        except (SyntaxError, ValueError, TypeError):
            tree = None
        if tree is not None:
            aliases = _python_import_aliases(tree)
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
            calls.sort(
                key=lambda node: (
                    int(getattr(node, "lineno", 0) or 0),
                    int(getattr(node, "col_offset", 0) or 0),
                    int(getattr(node, "end_lineno", 0) or 0),
                    int(getattr(node, "end_col_offset", 0) or 0),
                )
            )
            occurrences: dict[str, int] = {}
            for node in calls:
                name = _canonical_call_name(_ast_call_name(node.func), aliases)
                cwe = _call_cwe(name, node)
                if not cwe:
                    continue
                segment = ast.get_source_segment(code, node) or ""
                normalized = _normalize_source_anchor(segment)
                if not normalized:
                    continue
                occurrence = occurrences.get(normalized, 0) + 1
                occurrences[normalized] = occurrence
                line_number = int(getattr(node, "lineno", 0) or 0)
                end_line = int(getattr(node, "end_lineno", line_number) or line_number)
                records.append(
                    _CodeAnchor(
                        anchor=f"py:{normalized[:240]}#{max(1, occurrence)}",
                        line_number=line_number,
                        end_line=end_line,
                        column_start=getattr(node, "col_offset", None),
                        column_end=getattr(node, "end_col_offset", None),
                        normalized_segment=normalized,
                        cwe=cwe,
                    )
                )

    records.extend(_reported_anchor_records(candidate_list, records))
    records.extend(_evidence_anchor_records(code, candidate_list, records))
    return _AnchorIndex(
        records=tuple(records),
        by_anchor={record.anchor: record for record in records},
    )


def _reported_anchor_records(
    candidates: Iterable[Issue],
    existing: Iterable[_CodeAnchor],
) -> list[_CodeAnchor]:
    """把静态规则已经给出的精确锚点纳入统一索引。"""
    known = {record.anchor for record in existing}
    records: list[_CodeAnchor] = []
    for candidate in candidates:
        anchor = (candidate.source_anchor or "").strip()
        line_number = int(candidate.line_number or 0)
        if not anchor or anchor in known or line_number <= 0:
            continue
        records.append(
            _CodeAnchor(
                anchor=anchor,
                line_number=line_number,
                end_line=int(candidate.end_line or line_number),
                column_start=candidate.column_start,
                column_end=candidate.column_end,
                normalized_segment=_normalize_source_anchor(candidate.evidence),
                cwe=_normalize_cwe(candidate.cwe),
            )
        )
        known.add(anchor)
    return records


def _compact_source_with_offsets(value: str) -> tuple[str, list[int]]:
    compact: list[str] = []
    offsets: list[int] = []
    for offset, character in enumerate(value):
        if character.isspace():
            continue
        compact.append(character)
        offsets.append(offset)
    return "".join(compact), offsets


def _all_occurrences(haystack: str, needle: str) -> list[int]:
    positions: list[int] = []
    start = 0
    while needle and start <= len(haystack) - len(needle):
        position = haystack.find(needle, start)
        if position < 0:
            break
        positions.append(position)
        start = position + max(1, len(needle))
    return positions


def _source_position(code: str, start: int, end: int) -> tuple[int, int, int, int]:
    line_number = code.count("\n", 0, start) + 1
    end_line = code.count("\n", 0, max(start, end - 1)) + 1
    column_start = start - (code.rfind("\n", 0, start) + 1)
    column_end = end - (code.rfind("\n", 0, end) + 1)
    return line_number, end_line, column_start, column_end


def _evidence_anchor_records(
    code: str,
    candidates: Iterable[Issue],
    existing: Iterable[_CodeAnchor],
) -> list[_CodeAnchor]:
    """用去空白后仍可审计的源码证据补齐非 Python 及解析失败场景。"""
    compact_code, offsets = _compact_source_with_offsets(code)
    if not compact_code:
        return []

    existing_list = list(existing)
    records: list[_CodeAnchor] = []
    seen: set[tuple[str, str, int]] = set()
    for candidate in candidates:
        evidence = str(candidate.evidence or "").strip()
        compact_evidence = "".join(character for character in evidence if not character.isspace())
        if len(compact_evidence) < 8:
            continue
        cwe = _normalize_cwe(candidate.cwe)
        normalized_evidence = _normalize_evidence(evidence)
        if any(
            (not cwe or record.cwe == cwe)
            and (
                normalized_evidence == _normalize_evidence(record.normalized_segment)
                or (
                    len(_normalize_evidence(record.normalized_segment)) >= 8
                    and _normalize_evidence(record.normalized_segment) in normalized_evidence
                )
                or (
                    len(normalized_evidence) >= 8
                    and normalized_evidence in _normalize_evidence(record.normalized_segment)
                )
            )
            for record in existing_list
        ):
            continue
        digest = hashlib.sha256(compact_evidence.encode("utf-8")).hexdigest()[:20]
        cwe_key = (cwe or "generic").lower()
        for occurrence, position in enumerate(
            _all_occurrences(compact_code, compact_evidence),
            1,
        ):
            key = (cwe, digest, occurrence)
            if key in seen:
                continue
            start = offsets[position]
            end = offsets[position + len(compact_evidence) - 1] + 1
            line_number, end_line, column_start, column_end = _source_position(
                code,
                start,
                end,
            )
            records.append(
                _CodeAnchor(
                    anchor=f"src:{cwe_key}:{digest}#{occurrence}",
                    line_number=line_number,
                    end_line=end_line,
                    column_start=column_start,
                    column_end=column_end,
                    normalized_segment=_normalize_source_anchor(code[start:end]),
                    cwe=cwe,
                )
            )
            seen.add(key)
    return records


def _ast_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _ast_call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _python_import_aliases(tree: ast.AST) -> dict[str, str]:
    """提取 Python import 别名，确保源码锚点识别真实 API 身份。"""

    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                local = item.asname or item.name.split(".", 1)[0]
                aliases[local] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                if item.name == "*":
                    continue
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _canonical_call_name(name: str, aliases: dict[str, str]) -> str:
    if not name:
        return ""
    root, separator, suffix = name.partition(".")
    canonical_root = aliases.get(root, root)
    return f"{canonical_root}.{suffix}" if separator else canonical_root


def _call_cwe(name: str, node: ast.Call) -> str:
    normalized = (name or "").lower()
    tail = normalized.rsplit(".", 1)[-1]
    if normalized in {"os.system", "os.popen"}:
        return "CWE-78"
    if normalized.startswith("subprocess.") and tail in {
        "run", "call", "check_call", "check_output", "popen",
        "getoutput", "getstatusoutput",
    }:
        # 只有带 shell=True 的 subprocess 调用才是此规则的明确接收点。
        if tail in {"getoutput", "getstatusoutput"}:
            return "CWE-78"
        if any(
            keyword.arg == "shell"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        ):
            return "CWE-78"
        return ""
    if normalized.endswith((".pickle.loads", ".pickle.load", ".pkl.loads", ".pkl.load")):
        return "CWE-502"
    if normalized in {"pickle.loads", "pickle.load", "pkl.loads", "pkl.load"}:
        return "CWE-502"
    if normalized in {"yaml.load", "yaml.unsafe_load", "yaml.full_load"}:
        return "CWE-502"
    if normalized in {"eval", "exec"}:
        return "CWE-95"
    return ""


def _attach_code_anchors(candidates: List[Issue], index: _AnchorIndex) -> List[Issue]:
    if not index.records:
        return candidates
    output: list[Issue] = []
    for candidate in candidates:
        if candidate.source_anchor:
            output.append(candidate)
            continue
        record = _resolve_anchor(candidate, index)
        if record is None:
            output.append(candidate)
            continue
        output.append(
            replace(
                candidate,
                source_anchor=record.anchor,
                column_start=record.column_start,
                column_end=record.column_end,
            )
        )
    return output


def _resolve_anchor(issue: Issue, index: _AnchorIndex) -> Optional[_CodeAnchor]:
    cwe = _normalize_cwe(issue.cwe)
    evidence = _normalize_evidence(issue.evidence)
    # 没有漏洞身份也没有源码证据的泛化问题（例如可维护性建议）不能
    # 因文件里恰好只有一个安全 sink 就借用该 sink，继而被错误去重。
    if not cwe and not evidence:
        return None
    records = [record for record in index.records if not cwe or record.cwe == cwe]
    if not records:
        return None
    if evidence:
        scored: list[tuple[int, int, _CodeAnchor]] = []
        for record in records:
            normalized_segment = _normalize_evidence(record.normalized_segment)
            if evidence == normalized_segment:
                match_score = 4
            elif len(normalized_segment) >= 8 and normalized_segment in evidence:
                match_score = 3
            elif len(evidence) >= 8 and evidence in normalized_segment:
                match_score = 2
            else:
                continue
            distance = abs(int(issue.line_number or 0) - record.line_number)
            scored.append((match_score, -distance, record))
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1], item[2].anchor), reverse=True)
        best = scored[0]
        tied = [item for item in scored if item[0:2] == best[0:2]]
        return best[2] if len(tied) == 1 else None

    # 没有证据时只有唯一同类 sink，或最近位置唯一，才做自动定位。
    if len(records) == 1:
        return records[0]
    line = int(issue.line_number or 0)
    if line <= 0:
        return None
    ranked = sorted(
        records,
        key=lambda record: (abs(line - record.line_number), record.anchor),
    )
    nearest_distance = abs(line - ranked[0].line_number)
    if nearest_distance > _LINE_PROXIMITY:
        return None
    if len(ranked) > 1 and abs(line - ranked[1].line_number) == nearest_distance:
        return None
    return ranked[0]


def _common_anchor(cluster: List[Issue]) -> str:
    anchors = {item.source_anchor.strip() for item in cluster if item.source_anchor}
    return next(iter(anchors)) if len(anchors) == 1 else ""


def _preferred_column(cluster: List[Issue], field_name: str) -> Optional[int]:
    values = [getattr(item, field_name, None) for item in cluster]
    values = [int(value) for value in values if value is not None]
    return min(values) if values else None


def _looks_like_code(value: Optional[str]) -> bool:
    text = str(value or "")
    if not text.strip():
        return False
    return bool(
        re.search(
            r"(?:\b(?:os|subprocess|pickle|yaml|eval|exec|execute|system|popen)\b|[()\[\]{}=;]|::)",
            text,
            re.IGNORECASE,
        )
    )


def _cluster_match_score(cluster: List[Issue], candidate: Issue) -> float:
    """Use complete linkage so a broad middle result cannot bridge two sinks."""
    scores = [_match_score(member, candidate) for member in cluster]
    if not scores or any(score < 1.0 for score in scores):
        return -1.0
    return min(scores)


def _match_score(left: Issue, right: Issue) -> float:
    left_cwe = _normalize_cwe(left.cwe)
    right_cwe = _normalize_cwe(right.cwe)
    if left_cwe and right_cwe and left_cwe != right_cwe:
        return -1.0
    if not left_cwe and not right_cwe and left.issue_type != right.issue_type:
        return -1.0

    left_anchor = (left.source_anchor or "").strip()
    right_anchor = (right.source_anchor or "").strip()
    if left_anchor and right_anchor:
        # 已解析到两个明确调用点时，锚点是硬边界；相邻行和相同 CWE
        # 不能覆盖两个不同 sink。
        return 5.0 if left_anchor == right_anchor else -1.0

    left_line = int(left.line_number or 0)
    right_line = int(right.line_number or 0)
    if left_line == 0 or right_line == 0:
        if left_line != right_line:
            return -1.0
        title_score = _similarity(left.title or "", right.title or "")
        evidence_score = _evidence_similarity(left.evidence, right.evidence)
        return 1.0 + max(title_score, evidence_score) if max(title_score, evidence_score) >= 0.8 else -1.0

    line_distance = abs(left_line - right_line)
    if line_distance > _LINE_PROXIMITY:
        # 只有同一个确定性源码锚点才允许跨越模型行号漂移。
        return -1.0

    evidence_score = _evidence_similarity(left.evidence, right.evidence)
    title_score = _similarity(left.title or "", right.title or "")
    description_score = _similarity(left.description or "", right.description or "")
    left_evidence = _normalize_evidence(left.evidence)
    right_evidence = _normalize_evidence(right.evidence)
    if (
        left_evidence
        and right_evidence
        and left_evidence != right_evidence
        and (left.source_anchor or right.source_anchor)
        and _looks_like_code(left.evidence)
        and _looks_like_code(right.evidence)
    ):
        # 两段不同的代码证据，即使标题和行号相同，也代表可能的独立 sink。
        return -1.0
    if line_distance == 0:
        if evidence_score >= 0.8 or title_score >= 0.5 or description_score >= 0.65:
            return 2.0 + max(evidence_score, title_score, description_score)
        return -1.0

    # Adjacent lines merge only when both sources point to the same code anchor.
    if evidence_score >= 0.8:
        return 1.5 + evidence_score
    if not left.evidence and not right.evidence and title_score >= 0.8 and description_score >= 0.7:
        return 1.0 + min(title_score, description_score)
    return -1.0


def _merge_cluster(cluster: List[Issue], file_id: int, anchor_index: "_AnchorIndex") -> Issue:
    canonical = max(cluster, key=_content_quality_key)
    static_candidates = [item for item in cluster if _source_family(item.source) in {"static", "regex"}]
    line_source = max(static_candidates, key=_content_quality_key) if static_candidates else canonical

    details = _merge_source_details(cluster)
    cvss_score, cvss_vector, cvss_version, cvss_source = _select_cvss(cluster)
    severity = max(
        (normalize_severity(item.severity) for item in cluster),
        key=lambda value: _SEVERITY_RANK.get(value, 0),
    )
    references = _unique_strings(value for item in cluster for value in (item.references or []))
    compliance = _merge_compliance(item.compliance_mapping for item in cluster)
    source = _aggregate_source(item.source for item in cluster)
    source_anchor = _common_anchor(cluster)
    anchor_record = anchor_index.by_anchor.get(source_anchor) if source_anchor else None
    evidence = _preferred_evidence(static_candidates or cluster, anchor_record)
    line_number = int(anchor_record.line_number if anchor_record else line_source.line_number or 0)
    end_lines = [int(item.end_line) for item in cluster if item.end_line]
    end_line = (
        int(anchor_record.end_line)
        if anchor_record
        else (max(end_lines) if end_lines else (line_number or None))
    )
    column_start = anchor_record.column_start if anchor_record else _preferred_column(cluster, "column_start")
    column_end = anchor_record.column_end if anchor_record else _preferred_column(cluster, "column_end")
    fingerprint = _build_fingerprint(
        file_id=file_id,
        cwe=canonical.cwe,
        line_number=line_number,
        evidence=evidence,
        title=canonical.title or "",
        description=canonical.description,
        source_anchor=source_anchor,
    )

    return Issue(
        line_number=line_number,
        end_line=end_line,
        issue_type=canonical.issue_type,
        severity=severity,
        title=_longest_text(item.title for item in cluster),
        description=_longest_text(item.description for item in cluster) or "",
        suggestion=_longest_text(item.suggestion for item in cluster),
        fixed_code=_longest_text(item.fixed_code for item in cluster),
        owasp=_longest_text(item.owasp for item in cluster) or "",
        cwe=_normalize_cwe(canonical.cwe) or _normalize_cwe(_longest_text(item.cwe for item in cluster)) or "",
        evidence=evidence,
        exploit_scenario=_longest_text(item.exploit_scenario for item in cluster) or "",
        references=references,
        confidence=max(float(item.confidence or 0.0) for item in cluster),
        cvss_score=cvss_score,
        cvss_vector=cvss_vector,
        cvss_version=cvss_version,
        cvss_source=cvss_source,
        compliance_mapping=compliance,
        remediation=_longest_text(item.remediation for item in cluster) or "",
        source=source,
        static_rule_hits=sum(
            max(0, int(item.static_rule_hits or 0))
            for item in cluster
            if _source_family(item.source) in {"static", "regex"}
        ),
        source_details=details,
        confirmation_count=max(
            len(details),
            *(max(1, int(item.confirmation_count or 1)) for item in cluster),
        ),
        finding_fingerprint=fingerprint,
        source_anchor=source_anchor,
        column_start=column_start,
        column_end=column_end,
    )


def _merge_source_details(cluster: List[Issue]) -> List[dict]:
    details: List[dict] = []
    seen: set[tuple] = set()
    for issue in cluster:
        raw_details = issue.source_details or [{
            "source": issue.source or "llm",
            "confidence": round(float(issue.confidence or 0.0), 4),
            "evidence": (issue.evidence or "")[:2000],
            "line_number": int(issue.line_number or 0),
            "title": (issue.title or "")[:200],
        }]
        for raw in raw_details:
            try:
                confidence = round(float(raw.get("confidence", issue.confidence) or 0.0), 4)
            except (TypeError, ValueError):
                confidence = round(float(issue.confidence or 0.0), 4)
            try:
                line_number = max(0, int(raw.get("line_number") or 0))
            except (TypeError, ValueError):
                line_number = max(0, int(issue.line_number or 0))
            detail = {
                "source": str(raw.get("source") or issue.source or "llm")[:80],
                "confidence": max(0.0, min(1.0, confidence)),
                "evidence": str(raw.get("evidence") or "")[:2000],
                "line_number": line_number,
                "title": str(raw.get("title") or "")[:200],
            }
            if raw.get("source_anchor") or issue.source_anchor:
                detail["source_anchor"] = str(
                    raw.get("source_anchor") or issue.source_anchor or ""
                )[:300]
            key = tuple(detail.values())
            if key not in seen:
                details.append(detail)
                seen.add(key)
    return sorted(
        details,
        key=lambda item: (
            str(item.get("source") or ""),
            int(item.get("line_number") or 0),
            str(item.get("source_anchor") or ""),
            str(item.get("evidence") or ""),
            str(item.get("title") or ""),
            float(item.get("confidence") or 0.0),
        ),
    )


def _select_cvss(cluster: List[Issue]) -> tuple[Optional[float], Optional[str], Optional[str], str]:
    normalized = [normalize_cvss(item.cvss_score, item.cvss_vector) for item in cluster]
    vectors = [item for item in normalized if item[1] is not None and item[0] is not None]
    if vectors:
        return max(
            vectors,
            key=lambda item: (
                float(item[0] or 0.0),
                str(item[1] or ""),
            ),
        )
    scores = [item for item in normalized if item[0] is not None]
    if scores:
        return max(
            scores,
            key=lambda item: (float(item[0] or 0.0), str(item[3] or "")),
        )
    return None, None, None, "unavailable"


def _source_family(source: str) -> str:
    normalized = (source or "llm").strip().lower()
    if normalized.startswith("regex"):
        return "regex"
    if normalized.startswith("static"):
        return "static"
    if normalized.startswith("llm") or "collab" in normalized or "roundtable" in normalized:
        return "llm"
    if normalized == "hybrid":
        return "hybrid"
    return normalized


def _aggregate_source(sources: Iterable[str]) -> str:
    families = {_source_family(source) for source in sources}
    if "hybrid" in families or ("llm" in families and families & {"static", "regex"}):
        return "hybrid"
    if families == {"regex"}:
        return "regex"
    if families <= {"static", "regex"}:
        return "static"
    if families == {"llm"}:
        return "llm"
    return sorted(families)[0] if families else "llm"


def _content_quality_key(issue: Issue) -> tuple:
    family = _source_family(issue.source)
    richness = sum(len(str(value or "")) for value in (
        issue.title,
        issue.description,
        issue.suggestion,
        issue.evidence,
        issue.remediation,
    ))
    return (
        1 if family == "llm" else 0,
        richness,
        float(issue.confidence or 0.0),
        issue.title or "",
        issue.description or "",
        issue.evidence or "",
        issue.source or "",
    )


def _preferred_evidence(
    candidates: List[Issue],
    anchor_record: Optional[_CodeAnchor] = None,
) -> str:
    populated = [item for item in candidates if item.evidence]
    if not populated:
        return anchor_record.normalized_segment if anchor_record else ""
    return max(
        populated,
        key=lambda item: (
            float(item.confidence or 0.0),
            len(item.evidence),
            _normalize_evidence(item.evidence),
            item.source or "",
            item.title or "",
        ),
    ).evidence


def _normalize_cwe(value: Optional[str]) -> str:
    text = (value or "").strip().upper()
    if not text:
        return ""
    match = re.search(r"(?:CWE-?)?(\d+)", text)
    return f"CWE-{match.group(1)}" if match else text[:64]


def _normalize_evidence(value: Optional[str]) -> str:
    return re.sub(r"\s+", "", (value or "").strip().lower())


def _normalize_source_anchor(value: Optional[str]) -> str:
    """源码锚点使用保留标点的空白压缩形式。"""
    return re.sub(r"\s+", " ", (value or "").strip())


def _tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in _TOKEN_RE.findall(value or "")
        if len(token) > 1 or "\u4e00" <= token <= "\u9fff"
    }


def _similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _evidence_similarity(left: Optional[str], right: Optional[str]) -> float:
    normalized_left = _normalize_evidence(left)
    normalized_right = _normalize_evidence(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    shorter, longer = sorted((normalized_left, normalized_right), key=len)
    if len(shorter) >= 12 and shorter in longer:
        return 0.95
    return _similarity(normalized_left, normalized_right)


def _longest_text(values: Iterable[Optional[str]]) -> Optional[str]:
    populated = [str(value) for value in values if value]
    return max(populated, key=lambda value: (len(value), value)) if populated else None


def _unique_strings(values: Iterable[str]) -> List[str]:
    output: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            output.append(text)
            seen.add(text)
    return sorted(output)


def _merge_compliance(mappings: Iterable[dict]) -> dict:
    output: dict[str, List[str]] = {}
    for mapping in mappings:
        for standard, controls in (mapping or {}).items():
            output[standard] = _unique_strings([*output.get(standard, []), *(controls or [])])
    return {standard: sorted(output[standard]) for standard in sorted(output)}


def _build_fingerprint(
    *,
    file_id: int,
    cwe: str,
    line_number: int,
    evidence: str,
    title: str,
    description: str,
    source_anchor: str = "",
) -> str:
    anchor = (
        (source_anchor or "").strip()
        or _normalize_evidence(evidence)
        or " ".join(sorted(_tokens(f"{title} {description}")))
    )
    # 锚点存在时不把模型行号混入指纹，保证分片重试和行号漂移不产生新问题。
    line_part = "" if source_anchor else str(int(line_number))
    material = f"{int(file_id)}|{_normalize_cwe(cwe)}|{line_part}|{anchor}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
