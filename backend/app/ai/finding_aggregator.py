"""Deterministic, evidence-aware aggregation for multi-agent review findings."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

AGGREGATION_VERSION = "finding-aggregation-v1"
RISK_SCORING_VERSION = "claim-risk-v2"

_SCORES = {"严重": 100.0, "高": 75.0, "中": 50.0, "低": 25.0}
_RANKS = {"低": 1, "中": 2, "高": 3, "严重": 4}
_ALIASES = {
    "critical": "严重", "crit": "严重", "p0": "严重", "error": "严重",
    "危急": "严重", "致命": "严重", "严重": "严重",
    "high": "高", "p1": "高", "高危": "高", "高": "高",
    "medium": "中", "moderate": "中", "warning": "中", "p2": "中",
    "中危": "中", "中": "中",
    "low": "低", "info": "低", "informational": "低", "note": "低",
    "p3": "低", "p4": "低", "低危": "低", "低": "低",
}
_EVIDENCE_RANK = {"unsupported": 0, "inferred": 1, "direct": 2, "verified": 3}
_EVIDENCE_CAP = {"unsupported": 0.45, "inferred": 0.60, "direct": 0.80, "verified": 0.98}
_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]")


class AggregationResult(BaseModel):
    schema_version: str = AGGREGATION_VERSION
    risk_scoring_version: str = RISK_SCORING_VERSION
    issues: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


def normalize_severity_with_score(value: object) -> tuple[str, float]:
    """Map agent-specific labels and CVSS-like numbers into one versioned scale."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if 0 <= number <= 10:
            if number >= 9:
                normalized = "严重"
            elif number >= 7:
                normalized = "高"
            elif number >= 4:
                normalized = "中"
            else:
                normalized = "低"
            return normalized, _SCORES[normalized]
    text = str(value or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return normalize_severity_with_score(float(text))
    normalized = _ALIASES.get(text.lower()) or _ALIASES.get(text)
    if normalized is None:
        raise ValueError(f"unknown severity: {text[:40]}")
    return normalized, _SCORES[normalized]


def aggregate_agent_findings(
    findings_by_agent: Mapping[str, Sequence[Any]],
    agent_names: Mapping[str, str],
    *,
    code: str,
    file_name: str,
    chunk_id: str,
) -> AggregationResult:
    """Normalize, cluster and adjudicate claims without asking another model."""
    diagnostics: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    invalid_count = 0
    for agent_code in sorted(findings_by_agent):
        raw_items = findings_by_agent.get(agent_code)
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)):
            diagnostics.append({
                "code": "findings_not_array", "agent": agent_code,
                "message": "agent findings must be an array",
            })
            invalid_count += 1
            continue
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, Mapping):
                diagnostics.append({
                    "code": "finding_not_object", "agent": agent_code, "index": index,
                    "message": "finding must be an object",
                })
                invalid_count += 1
                continue
            try:
                claim = _normalize_claim(
                    raw,
                    agent_code=agent_code,
                    agent_name=str(agent_names.get(agent_code) or agent_code),
                    index=index,
                    code=code,
                    file_name=file_name,
                    chunk_id=chunk_id,
                )
            except (TypeError, ValueError) as exc:
                diagnostics.append({
                    "code": "finding_invalid", "agent": agent_code, "index": index,
                    "message": str(exc)[:300],
                })
                invalid_count += 1
                continue
            claims.append(claim)

    claims.sort(key=lambda item: item["claim_id"])
    clusters: list[list[dict[str, Any]]] = []
    for claim in claims:
        target = next(
            (cluster for cluster in clusters if all(_same_finding(claim, item) for item in cluster)),
            None,
        )
        if target is None:
            clusters.append([claim])
        else:
            target.append(claim)

    issues = [_aggregate_cluster(cluster, file_name=file_name, chunk_id=chunk_id) for cluster in clusters]
    issues.sort(key=lambda item: (int(item.get("line_number") or 0), item.get("finding_fingerprint") or ""))
    output_ids = sorted(
        claim["claim_id"]
        for issue in issues
        for claim in issue["aggregation"]["claims"]
    )
    input_ids = sorted(item["claim_id"] for item in claims)
    pending = sum(item["human_review_status"] == "pending" for item in issues)
    return AggregationResult(
        issues=issues,
        diagnostics=diagnostics,
        coverage={
            "input_claim_ids": input_ids,
            "output_claim_ids": output_ids,
            "discarded_claim_ids": [],
            "invalid_input_count": invalid_count,
            "input_claim_count": len(input_ids),
            "output_claim_count": len(output_ids),
            "coverage_ratio": 1.0 if input_ids else 1.0,
        },
        summary={
            "issue_count": len(issues),
            "pending_human_review_count": pending,
            "conflict_count": sum(item["conflict_status"] == "unresolved" for item in issues),
            "diagnostic_count": len(diagnostics),
        },
    )


def aggregate_agent_findings_safely(
    findings_by_agent: Mapping[str, Sequence[Any]],
    agent_names: Mapping[str, str],
    *,
    code: str,
    file_name: str,
    chunk_id: str,
) -> AggregationResult:
    """Aggregate claims and preserve each usable claim if aggregation itself fails."""
    try:
        return aggregate_agent_findings(
            findings_by_agent,
            agent_names,
            code=code,
            file_name=file_name,
            chunk_id=chunk_id,
        )
    except Exception as exc:
        return _fallback_agent_findings(
            findings_by_agent,
            agent_names,
            file_name=file_name,
            chunk_id=chunk_id,
            error=exc,
        )


def _fallback_agent_findings(
    findings_by_agent: Mapping[str, Sequence[Any]],
    agent_names: Mapping[str, str],
    *,
    file_name: str,
    chunk_id: str,
    error: Exception,
) -> AggregationResult:
    diagnostics: list[dict[str, Any]] = [{
        "code": "aggregation_internal_error",
        "message": str(error)[:300] or type(error).__name__,
    }]
    issues: list[dict[str, Any]] = []
    claim_ids: list[str] = []
    invalid_count = 0
    for agent_code in sorted(findings_by_agent):
        raw_items = findings_by_agent.get(agent_code)
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)):
            diagnostics.append({
                "code": "fallback_findings_not_array",
                "agent": agent_code,
                "message": "agent findings must be an array",
            })
            invalid_count += 1
            continue
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, Mapping):
                diagnostics.append({
                    "code": "fallback_finding_not_object",
                    "agent": agent_code,
                    "index": index,
                    "message": "finding must be an object",
                })
                invalid_count += 1
                continue
            issue = _fallback_issue(
                raw,
                agent_code=agent_code,
                agent_name=str(agent_names.get(agent_code) or agent_code),
                index=index,
                file_name=file_name,
                chunk_id=chunk_id,
            )
            issues.append(issue)
            claim_ids.append(issue["aggregation"]["claims"][0]["claim_id"])
    issues.sort(key=lambda item: (int(item.get("line_number") or 0), item["finding_fingerprint"]))
    claim_ids.sort()
    return AggregationResult(
        issues=issues,
        diagnostics=diagnostics,
        coverage={
            "input_claim_ids": claim_ids,
            "output_claim_ids": claim_ids,
            "discarded_claim_ids": [],
            "invalid_input_count": invalid_count,
            "input_claim_count": len(claim_ids),
            "output_claim_count": len(claim_ids),
            "coverage_ratio": 1.0,
        },
        summary={
            "issue_count": len(issues),
            "pending_human_review_count": len(issues),
            "conflict_count": len(issues),
            "diagnostic_count": len(diagnostics),
            "fallback": True,
        },
    )


def _fallback_issue(
    raw: Mapping[str, Any],
    *,
    agent_code: str,
    agent_name: str,
    index: int,
    file_name: str,
    chunk_id: str,
) -> dict[str, Any]:
    try:
        severity, severity_score = normalize_severity_with_score(raw.get("severity") or "中")
    except (TypeError, ValueError):
        severity, severity_score = "中", _SCORES["中"]
    try:
        confidence_raw, confidence = _normalize_confidence(raw.get("confidence"))
    except (TypeError, ValueError, OverflowError):
        confidence_raw, confidence = raw.get("confidence"), 0.5
    evidence = str(raw.get("evidence") or "")[:4000]
    evidence_quality = "direct" if evidence else "unsupported"
    confidence = round(min(confidence, _EVIDENCE_CAP[evidence_quality]), 4)
    line_number = _as_non_negative_int(raw.get("line_number", raw.get("line_start")))
    end_line = _as_non_negative_int(raw.get("end_line", raw.get("line_end"))) or line_number or None
    title = str(raw.get("title") or "")[:200]
    description = str(raw.get("description") or "")[:4000]
    source = str(raw.get("source") or f"llm:{agent_code}")[:80]
    claim_id = hashlib.sha256(
        f"fallback|{file_name}|{chunk_id}|{agent_code}|{index}".encode("utf-8")
    ).hexdigest()
    claim = {
        "claim_id": claim_id,
        "agent_code": agent_code,
        "agent_name": agent_name,
        "source": source,
        "title": title,
        "description": description,
        "severity": {
            "raw": str(raw.get("severity") or ""),
            "normalized": severity,
            "score": severity_score,
            "mapping_version": AGGREGATION_VERSION,
        },
        "confidence": {
            "raw": str(confidence_raw if confidence_raw is not None else ""),
            "calibrated": confidence,
            "method": "fallback-evidence-cap",
        },
        "evidence_quality": evidence_quality,
        "evidence": evidence,
        "line_number": line_number,
        "end_line": end_line,
        "cwe": _normalize_cwe(raw.get("cwe")),
    }
    return {
        "line_number": line_number,
        "end_line": end_line,
        "issue_type": str(raw.get("issue_type") or "其他")[:50],
        "severity": severity,
        "confidence": confidence,
        "title": title or None,
        "description": description or "聚合器异常，已保留原始主张供人工复核",
        "suggestion": str(raw.get("suggestion") or "")[:4000] or None,
        "fixed_code": str(raw.get("fixed_code") or "")[:8000] or None,
        "owasp": str(raw.get("owasp") or "")[:128],
        "cwe": _normalize_cwe(raw.get("cwe")),
        "evidence": evidence,
        "exploit_scenario": str(raw.get("exploit_scenario") or "")[:4000],
        "references": _string_list(raw.get("references")),
        "remediation": str(raw.get("remediation") or "")[:4000],
        "source": "llm",
        "source_details": [{
            "source": source,
            "agent_code": agent_code,
            "agent_name": agent_name,
            "confidence": confidence,
            "severity": severity,
            "evidence_quality": evidence_quality,
            "evidence": evidence,
            "line_number": line_number,
            "title": title,
            "claim_id": claim_id,
        }],
        "confirmation_count": 1,
        "finding_fingerprint": claim_id,
        "source_anchor": str(raw.get("source_anchor") or "")[:300],
        "aggregation_version": AGGREGATION_VERSION,
        "evidence_quality": evidence_quality,
        "conflict_status": "unresolved",
        "human_review_status": "pending",
        "risk_score": round(severity_score * confidence, 2),
        "aggregation": {
            "schema_version": AGGREGATION_VERSION,
            "risk_scoring_version": RISK_SCORING_VERSION,
            "canonical_claim_id": claim_id,
            "claims": [claim],
            "conflicts": ["aggregation_internal_error"],
            "decision": "human_review",
            "fallback": True,
        },
    }


def _normalize_claim(
    raw: Mapping[str, Any],
    *,
    agent_code: str,
    agent_name: str,
    index: int,
    code: str,
    file_name: str,
    chunk_id: str,
) -> dict[str, Any]:
    title = str(raw.get("title") or "").strip()[:200]
    description = str(raw.get("description") or "").strip()[:4000]
    evidence = str(raw.get("evidence") or "").strip()[:4000]
    if not any((title, description, evidence)):
        raise ValueError("finding has no title, description or evidence")
    severity, severity_score = normalize_severity_with_score(raw.get("severity") or "中")
    confidence_raw, confidence = _normalize_confidence(raw.get("confidence"))
    evidence_quality = _evidence_quality(evidence, code, raw)
    calibrated = round(min(confidence, _EVIDENCE_CAP[evidence_quality]), 4)
    line_number = _as_non_negative_int(raw.get("line_number", raw.get("line_start")))
    end_line = _as_non_negative_int(raw.get("end_line", raw.get("line_end"))) or line_number or None
    source = str(raw.get("source") or f"llm:{agent_code}")[:80]
    identity = {
        "agent": agent_code,
        "source": source,
        "index": index,
        "chunk": chunk_id,
        "file": file_name,
        "line": line_number,
        "cwe": _normalize_cwe(raw.get("cwe")),
        "evidence": _normalize_text(evidence),
        "title": _normalize_text(title),
    }
    claim_id = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "claim_id": claim_id,
        "agent_code": agent_code,
        "agent_name": agent_name,
        "source": source,
        "title": title,
        "issue_type": str(raw.get("issue_type") or "其他")[:50],
        "severity": {
            "raw": raw.get("severity"),
            "normalized": severity,
            "score": severity_score,
            "mapping_version": AGGREGATION_VERSION,
        },
        "confidence": {
            "raw": confidence_raw,
            "calibrated": calibrated,
            "method": f"evidence-cap:{evidence_quality}",
        },
        "evidence_quality": evidence_quality,
        "line_number": line_number,
        "end_line": end_line,
        "description": description,
        "suggestion": str(raw.get("suggestion") or "")[:4000],
        "fixed_code": str(raw.get("fixed_code") or "")[:8000],
        "owasp": str(raw.get("owasp") or "")[:128],
        "cwe": _normalize_cwe(raw.get("cwe")),
        "evidence": evidence,
        "exploit_scenario": str(raw.get("exploit_scenario") or "")[:4000],
        "references": _string_list(raw.get("references")),
        "remediation": str(raw.get("remediation") or "")[:4000],
        "source_anchor": str(raw.get("source_anchor") or "")[:300],
        "stance": str(raw.get("stance") or "asserted")[:32],
    }


def _normalize_confidence(value: Any) -> tuple[Any, float]:
    if value is None or value == "":
        return value, 0.5
    if isinstance(value, str):
        aliases = {"high": 0.85, "medium": 0.65, "low": 0.4, "高": 0.85, "中": 0.65, "低": 0.4}
        if value.strip().lower() in aliases:
            return value, aliases[value.strip().lower()]
        text = value.strip().rstrip("%")
        number = float(text)
        if value.strip().endswith("%"):
            number /= 100
    else:
        number = float(value)
    if not math.isfinite(number):
        return value, 0.5
    if 1 < number <= 100:
        number /= 100
    return value, max(0.0, min(1.0, number))


def _evidence_quality(evidence: str, code: str, raw: Mapping[str, Any]) -> str:
    normalized_evidence = _normalize_text(evidence)
    if normalized_evidence and normalized_evidence in _normalize_text(code):
        return "verified"
    if normalized_evidence:
        return "direct"
    if _as_non_negative_int(raw.get("line_number", raw.get("line_start"))) or raw.get("source_anchor"):
        return "inferred"
    return "unsupported"


def _same_finding(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_anchor = str(left.get("source_anchor") or "")
    right_anchor = str(right.get("source_anchor") or "")
    if left_anchor and right_anchor:
        return left_anchor == right_anchor
    left_evidence = _normalize_text(left.get("evidence"))
    right_evidence = _normalize_text(right.get("evidence"))
    left_line, right_line = int(left.get("line_number") or 0), int(right.get("line_number") or 0)
    if left_evidence and right_evidence:
        same_evidence = (
            left_evidence == right_evidence
            or left_evidence in right_evidence
            or right_evidence in left_evidence
        )
        return same_evidence and (not left_line or not right_line or abs(left_line - right_line) <= 2)
    if left_line and right_line and left_line == right_line:
        left_cwe, right_cwe = str(left.get("cwe") or ""), str(right.get("cwe") or "")
        if left_cwe and right_cwe and left_cwe == right_cwe:
            return _text_similarity(str(left.get("title") or ""), str(right.get("title") or "")) >= 0.5
    return False


def _aggregate_cluster(cluster: list[dict[str, Any]], *, file_name: str, chunk_id: str) -> dict[str, Any]:
    ordered = sorted(cluster, key=lambda item: item["claim_id"])
    canonical = max(
        ordered,
        key=lambda item: (
            _EVIDENCE_RANK[item["evidence_quality"]],
            item["confidence"]["calibrated"],
            len(item["evidence"]),
            len(item["description"]),
            item["claim_id"],
        ),
    )
    conflicts: list[str] = []
    severity_ranks = {_RANKS[item["severity"]["normalized"]] for item in ordered}
    if severity_ranks and max(severity_ranks) - min(severity_ranks) >= 2:
        conflicts.append("severity_disagreement")
    cwes = {item["cwe"] for item in ordered if item["cwe"]}
    if len(cwes) > 1:
        conflicts.append("classification_disagreement")
    stances = {item["stance"] for item in ordered}
    if any(value in {"rejected", "disputed", "reject"} for value in stances):
        conflicts.append("stance_disagreement")
    conflict_status = "unresolved" if conflicts else "none"
    needs_human = (
        bool(conflicts)
        or canonical["confidence"]["calibrated"] < 0.6
        or canonical["evidence_quality"] in {"unsupported", "inferred"}
    )
    real_sources: dict[tuple[str, str], dict[str, Any]] = {}
    for item in ordered:
        source_key = (item["agent_code"], item["source"])
        previous = real_sources.get(source_key)
        current_rank = (item["confidence"]["calibrated"], item["claim_id"])
        previous_rank = (
            (previous["confidence"]["calibrated"], previous["claim_id"])
            if previous
            else None
        )
        if previous_rank is None or current_rank > previous_rank:
            real_sources[source_key] = item
    source_claims = [real_sources[key] for key in sorted(real_sources)]
    source_details = [
        {
            "source": item["source"],
            "agent_code": item["agent_code"],
            "agent_name": item["agent_name"],
            "confidence": item["confidence"]["calibrated"],
            "severity": item["severity"]["normalized"],
            "evidence_quality": item["evidence_quality"],
            "evidence": item["evidence"],
            "line_number": item["line_number"],
            "title": item["title"],
            "claim_id": item["claim_id"],
        }
        for item in source_claims
    ]
    weighted_total = sum(
        item["severity"]["score"] * item["confidence"]["calibrated"]
        for item in source_claims
    )
    weight = sum(item["confidence"]["calibrated"] for item in source_claims)
    risk_score = round(weighted_total / weight, 2) if weight else canonical["severity"]["score"]
    fingerprint_seed = "|".join((
        file_name,
        chunk_id,
        canonical["source_anchor"],
        canonical["cwe"],
        str(canonical["line_number"]),
        _normalize_text(canonical["evidence"] or canonical["title"]),
    ))
    fingerprint = hashlib.sha256(fingerprint_seed.encode("utf-8")).hexdigest()
    return {
        "line_number": canonical["line_number"],
        "end_line": canonical["end_line"],
        "issue_type": canonical["issue_type"],
        # Severity and confidence always come from the same canonical claim.
        "severity": canonical["severity"]["normalized"],
        "confidence": canonical["confidence"]["calibrated"],
        "title": canonical["title"] or None,
        "description": canonical["description"],
        "suggestion": canonical["suggestion"] or None,
        "fixed_code": canonical["fixed_code"] or None,
        "owasp": canonical["owasp"],
        "cwe": canonical["cwe"],
        "evidence": canonical["evidence"],
        "exploit_scenario": canonical["exploit_scenario"],
        "references": sorted({value for item in ordered for value in item["references"]}),
        "remediation": canonical["remediation"],
        # ReviewIssue.source is VARCHAR(16); keep the established compact code.
        "source": "llm" if len(source_details) == 1 else "llm_collab",
        "source_details": source_details,
        "confirmation_count": len(source_details),
        "finding_fingerprint": fingerprint,
        "source_anchor": canonical["source_anchor"],
        "aggregation_version": AGGREGATION_VERSION,
        "evidence_quality": canonical["evidence_quality"],
        "conflict_status": conflict_status,
        "human_review_status": "pending" if needs_human else "not_required",
        "risk_score": risk_score,
        "aggregation": {
            "schema_version": AGGREGATION_VERSION,
            "risk_scoring_version": RISK_SCORING_VERSION,
            "canonical_claim_id": canonical["claim_id"],
            "claims": ordered,
            "conflicts": conflicts,
            "decision": "human_review" if needs_human else "accepted",
        },
    }


def _as_non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _normalize_cwe(value: Any) -> str:
    text = str(value or "").strip().upper()
    match = re.search(r"CWE[-_ ]?(\d+)", text)
    return f"CWE-{match.group(1)}" if match else text[:64]


def _normalize_text(value: Any) -> str:
    return _SPACE_RE.sub("", str(value or "")).lower()


def _text_similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = set(_TOKEN_RE.findall(left.lower())), set(_TOKEN_RE.findall(right.lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return sorted({str(item) for item in value if item})
    return []


__all__ = [
    "AGGREGATION_VERSION",
    "RISK_SCORING_VERSION",
    "AggregationResult",
    "aggregate_agent_findings",
    "aggregate_agent_findings_safely",
    "normalize_severity_with_score",
]
