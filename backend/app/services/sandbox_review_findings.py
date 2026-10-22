"""Evidence-bound schema for sandbox Verify output; never infer CVSS from prose.

The verifier makes the semantic vulnerability judgement. This module validates
its references against reconciled runner v2 receipts, not against model drafts,
HTTP reachability, generated test names alone, or compressed evidence summaries.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

from app.ai.cvss import normalize_cvss

SCHEMA_VERSION = "sandbox-security-findings-v1"
_MAX_FINDINGS = 100


def build_cvss_evidence(conclusion: dict[str, Any], mode: str) -> dict[str, dict[str, Any]]:
    """Expose only executed, injected v2 test outputs from the requested mode."""
    evidence = conclusion.get("evidence")
    if not isinstance(evidence, dict) or mode not in {"whitebox", "blackbox", "combined"}:
        return {}
    generation, tests = evidence.get("agent_test_generation"), evidence.get("agent_tests")
    if not isinstance(generation, dict) or generation.get("status") != "generated":
        return {}
    generated_mode = generation.get("mode")
    allowed_modes = {"whitebox", "blackbox", "combined"} if mode == "combined" else {mode}
    if not isinstance(generated_mode, str) or generated_mode not in allowed_modes or not isinstance(tests, dict):
        return {}
    if tests.get("protocol_version") != 2 or tests.get("missing") or tests.get("unexpected"):
        return {}
    files, results = tests.get("files"), tests.get("file_results")
    if not isinstance(files, dict) or not isinstance(results, dict):
        return {}
    catalog = {}
    for name, result in results.items():
        if not isinstance(name, str) or not isinstance(result, dict):
            continue
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("_agent_tests",):
            continue
        scope = "blackbox" if path.stem == "blackbox" else "whitebox"
        if generated_mode != "combined" and scope != generated_mode:
            continue
        status, code = result.get("status"), result.get("exit_code")
        if result.get("phase") != "execute" or files.get(name) != status or type(code) is not int:
            continue
        # Compilation, timeouts and protocol/infrastructure errors cannot prove a vulnerability.
        if not ((status == "pass" and code == 0 and not result.get("failure_kind")) or (
            status == "fail" and code not in {0, 124, 126, 127, 137}
            and result.get("failure_kind") == "execution_failure"
        )):
            continue
        output = result.get("output")
        if not isinstance(output, str) or not output.strip():
            continue
        catalog[name] = {"scope": scope, "output": output, "exit_code": code,
                         "sha256": hashlib.sha256(output.encode()).hexdigest()}
    return catalog


def normalize_review_findings(
    raw: object, conclusion: dict[str, Any], mode: str,
) -> dict[str, Any]:
    """Validate Verify's explicit JSON, retain unconfirmed items, and recompute vectors.

    Legacy JSON without security_findings remains usable for narrative reports,
    but cannot supply a CVSS score. Malformed data never falls back to Markdown
    extraction. Missing/invalid vectors remain explicitly unscored findings.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```json\n") and text.endswith("```"):
            text = text[8:-3].strip()
        elif text.startswith("```\n") and text.endswith("```"):
            text = text[4:-3].strip()
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            raw = None
    rows = raw.get("security_findings") if isinstance(raw, dict) else None
    schema_status = "complete" if isinstance(rows, list) else "unavailable"
    if isinstance(rows, list) and (len(rows) > _MAX_FINDINGS or any(not isinstance(r, dict) for r in rows)):
        rows, schema_status = [], "invalid_schema"
    catalog = build_cvss_evidence(conclusion, mode)
    confirmed, unconfirmed, seen = [], [], set()
    from app.services.agent_responses_service import redact_agent_output_text

    def text(row: dict, key: str, limit: int) -> str:
        value = row.get(key)
        return redact_agent_output_text(value.strip())[:limit] if isinstance(value, str) else ""

    for row in rows or []:
        title = text(row, "title", 300)
        description, impact = text(row, "description", 4000), text(row, "impact", 2000)
        rationale = text(row, "cvss_rationale", 2000)
        status = row.get("status")
        reasons = []
        if not isinstance(status, str) or status not in {"confirmed", "suspected", "refuted"}:
            status = "suspected"
            reasons.append("invalid_status")
        if not title or not description or not impact:
            reasons.append("missing_finding_details")
        refs = row.get("evidence_refs")
        valid_refs = []
        if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
            reasons.append("missing_execution_evidence")
        else:
            for ref in refs:
                name = ref.get("test_file") if isinstance(ref, dict) else None
                quote = ref.get("quote") if isinstance(ref, dict) else None
                source = catalog.get(name) if isinstance(name, str) else None
                if (not source or not isinstance(quote, str) or len(quote.strip()) < 24
                        or quote not in source["output"]):
                    reasons.append("unbound_execution_evidence")
                    continue
                valid_refs.append({"test_file": name, "quote": quote, "scope": source["scope"],
                                   "sha256": source["sha256"]})
        if reasons and status == "confirmed":
            status = "suspected"
        score, vector, version, cvss_source = normalize_cvss(None, row.get("cvss_vector") if rationale else None)
        # No score inferred from severity, a bare number, or the report's Markdown.
        severity = row.get("severity")
        if not isinstance(severity, str) or severity not in {"严重", "高", "中", "低", "提示"}:
            severity = "未分级"
        finding = {
            "title": title or "结构化候选缺少标题", "description": description, "impact": impact,
            "severity": severity,
            "status": status, "cvss_score": score, "cvss_vector": vector, "cvss_version": version,
            "cvss_source": cvss_source, "cvss_rationale": rationale, "evidence_refs": valid_refs,
            "source": "sandbox_verify", "schema_version": SCHEMA_VERSION,
        }
        if reasons:
            finding["validation_errors"] = sorted(set(reasons))
        identity = (status, title.casefold(), json.dumps(valid_refs, sort_keys=True, ensure_ascii=False))
        if identity in seen:
            continue
        seen.add(identity)
        (confirmed if status == "confirmed" else unconfirmed).append(finding)
    return {
        "cvss_findings": confirmed,
        "unconfirmed_findings": unconfirmed,
        "cvss_review": {"schema_version": SCHEMA_VERSION, "status": schema_status,
                        "producer": "test_reviewer.verify", "evidence_basis": "reconciled_runner_v2",
                        "confirmed": len(confirmed), "unconfirmed": len(unconfirmed)},
    }
