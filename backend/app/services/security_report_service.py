"""Persist Security Sentinel results in the account-scoped report center."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.user import User
from app.services.project_member_service import require_project_access
from app.services.report_text import clean_report_payload, clean_report_text

_SEVERITIES = ("严重", "高", "中", "低")


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(_json_safe(value), ensure_ascii=False, allow_nan=False, default=str))


def _json_safe(value: Any) -> Any:
    """Replace non-finite model numbers before storing portable JSON snapshots."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _text(value: Any, limit: int = 20000) -> str:
    return str(value or "").strip()[:limit]


def _plain_report_text(value: Any, limit: int = 20000) -> str:
    """Render model prose as readable text instead of exposing Markdown markers."""
    return clean_report_text(_text(value, limit), limit)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (0.0 <= number <= 1.0):
        return None
    return number


def infer_coverage_status(execution_status: str, coverage: dict) -> str:
    stated = str(coverage.get("verification_status") or coverage.get("coverage_status") or "").lower()
    if execution_status == "failed" or stated == "failed":
        return "failed"
    if execution_status == "partial" or stated == "partial":
        return "partial"
    if stated == "unknown":
        return "unknown"

    # A successful HTTP/Agent result does not imply full scope coverage. Preserve
    # explicit omissions, truncation, bounded triage and failed sub-stages.
    if str(coverage.get("scan_mode") or "").lower() == "triage":
        return "partial"
    for field in (
        "truncated",
        "findings_truncated",
        "result_payload_truncated",
        "graph_items_truncated",
        "response_graph_truncated",
    ):
        if coverage.get(field) is True:
            return "partial"
    for field in (
        "static_complete",
        "semantic_complete",
        "semantic_execution_complete",
        "audit_complete",
        "scan_complete",
        "aggregate_inputs_complete",
    ):
        if coverage.get(field) is False:
            return "partial"
    for field in (
        "skipped_file_count",
        "skipped_project_count",
        "semantic_failed_file_count",
        "semantic_unscheduled_file_count",
        "semantic_truncated_file_count",
    ):
        if _safe_int(coverage.get(field)) > 0:
            return "partial"
    for field in ("coverage_ratio", "semantic_char_coverage_ratio", "semantic_archive_coverage_ratio"):
        value = coverage.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 1:
            return "partial"
    for field in ("unscanned", "skipped", "project_errors", "stage_errors"):
        value = coverage.get(field)
        if isinstance(value, list) and value:
            return "partial"

    # A success status only proves that the request returned. Require affirmative
    # scope evidence before claiming that the full audit completed.
    count_pairs = (
        ("scanned_file_count", "total_file_count"),
        ("scanned_project_count", "project_count"),
    )
    for scanned_key, total_key in count_pairs:
        scanned = coverage.get(scanned_key)
        total = coverage.get(total_key)
        if (
            isinstance(scanned, int) and not isinstance(scanned, bool)
            and isinstance(total, int) and not isinstance(total, bool)
            and total >= 0 and scanned != total
        ):
            return "partial"
    if stated in {"complete", "completed", "verified"}:
        return "complete"
    completion_flags = (
        "static_complete",
        "semantic_complete",
        "semantic_execution_complete",
        "audit_complete",
        "scan_complete",
        "aggregate_inputs_complete",
    )
    if any(coverage.get(field) is True for field in completion_flags):
        return "complete"
    for scanned_key, total_key in count_pairs:
        scanned = coverage.get(scanned_key)
        total = coverage.get(total_key)
        if (
            isinstance(scanned, int) and not isinstance(scanned, bool)
            and isinstance(total, int) and not isinstance(total, bool)
            and total > 0 and scanned == total
        ):
            return "complete"
    for field in ("coverage_ratio", "semantic_char_coverage_ratio", "semantic_archive_coverage_ratio"):
        value = coverage.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 1:
            return "complete"
    return "unknown"


def _report_markdown(
    *,
    task_name: str,
    project_name: str,
    execution_status: str,
    summary: str,
    score: int | None,
    findings: list[dict],
    coverage: dict,
    result: dict,
    error_message: str,
) -> str:
    status_labels = {"success": "执行完成", "partial": "部分完成", "failed": "执行失败"}
    coverage_status = str(coverage.get("verification_status") or coverage.get("coverage_status") or "unknown")
    coverage_labels = {"complete": "完整", "partial": "部分", "failed": "未完成", "unknown": "未知"}
    lines = [
        "棱镜 Prism 安全审计报告",
        "",
        "执行概况",
        f"项目：{project_name}",
        f"任务：{task_name}",
        f"执行状态：{status_labels.get(execution_status, '状态未知')}",
        f"风险评分：{score}/100" if score is not None else "风险评分：未评估",
        f"扫描文件数：{max(0, _safe_int(result.get('file_count')))}",
        f"报告生成时间：{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
        "",
        "审计结论",
        summary or "服务未返回审计摘要；本报告仅保留已取得的结果与覆盖记录。",
        "",
        "验证覆盖",
        f"覆盖范围：{coverage_labels.get(coverage_status, '未知')}",
    ]
    for key, label in (
        ("requested_mode", "请求模式"),
        ("scan_mode", "实际扫描模式"),
        ("total_file_count", "项目文件总数"),
        ("scanned_file_count", "已扫描文件数"),
        ("static_scanned_file_count", "静态检查文件数"),
        ("semantic_candidate_file_count", "语义分析候选文件数"),
        ("semantic_verified_file_count", "语义分析完成文件数"),
        ("scanned_project_count", "已扫描项目数"),
        ("project_count", "目标项目数"),
        ("skipped_file_count", "跳过文件数"),
        ("skipped_project_count", "跳过项目数"),
        ("coverage_ratio", "文件覆盖比例"),
        ("semantic_char_coverage_ratio", "语义字符覆盖比例"),
        ("semantic_archive_coverage_ratio", "归档文本语义覆盖比例"),
        ("semantic_failed_file_count", "语义分析失败文件数"),
        ("semantic_unscheduled_file_count", "未进入语义分析文件数"),
        ("semantic_truncated_file_count", "语义分析截断文件数"),
    ):
        if coverage.get(key) is not None:
            lines.append(f"{label}：{coverage[key]}")
    for field, label in (
        ("truncated", "扫描文件发生裁剪"),
        ("findings_truncated", "发现列表发生裁剪"),
        ("result_payload_truncated", "结果载荷发生裁剪"),
        ("graph_items_truncated", "攻击面关系发生裁剪"),
    ):
        if coverage.get(field) is not None:
            lines.append(f"{label}：{'是' if coverage[field] else '否'}")
    unscanned = coverage.get("unscanned")
    if isinstance(unscanned, list) and unscanned:
        lines.append("未扫描或未验证范围：")
        lines.extend(f"{index}. {_plain_report_text(item, 1000)}" for index, item in enumerate(unscanned, start=1))
    else:
        lines.append("未扫描或未验证范围：未记录")
    skipped = coverage.get("skipped")
    if isinstance(skipped, list) and skipped:
        lines.append("跳过范围：")
        lines.extend(f"{index}. {item}" for index, item in enumerate(skipped, start=1))
    for key, label in (("project_errors", "项目执行错误"), ("stage_errors", "阶段执行错误")):
        errors = coverage.get(key)
        if isinstance(errors, list) and errors:
            lines.extend(["", label])
            lines.extend(f"{index}. {_plain_report_text(item, 1000)}" for index, item in enumerate(errors, start=1))
    if error_message:
        lines.extend(["", "执行问题", error_message])
    lines.extend(["", "发现问题", f"本次返回 {len(findings)} 条可查看记录。"])
    for index, finding in enumerate(findings, start=1):
        title = _plain_report_text(finding.get("title"), 500) or "未命名安全问题"
        severity = _plain_report_text(finding.get("severity"), 32) or "未分级"
        path = _plain_report_text(finding.get("file_path"), 1000) or "文件位置未提供"
        line = finding.get("lines") or finding.get("line_number") or "行号未提供"
        lines.extend(
            [
                "",
                f"{index}. {title}",
                f"严重度：{severity}",
                f"位置：{path} · {line}",
            ]
        )
        if finding.get("cwe"):
            lines.append(f"CWE：{_plain_report_text(finding.get('cwe'), 100)}")
        if finding.get("owasp"):
            lines.append(f"OWASP：{_plain_report_text(finding.get('owasp'), 200)}")
        if finding.get("evidence"):
            lines.append(f"证据：{_plain_report_text(finding.get('evidence'))}")
        if finding.get("exploit_scenario"):
            lines.append(f"影响场景：{_plain_report_text(finding.get('exploit_scenario'))}")
        if finding.get("fix_suggestion") or finding.get("suggestion"):
            lines.append("修复建议：" + _plain_report_text(finding.get("fix_suggestion") or finding.get("suggestion")))
        if finding.get("verification"):
            lines.append(f"复核状态：{_plain_report_text(finding.get('verification'), 100)}")
    threat_model = result.get("threat_model")
    if isinstance(threat_model, dict):
        for key, label, path in (
            ("api_endpoints", "接口检查结果", ("method", "path", "file_path", "line_number", "auth_hint")),
            ("data_flows", "跨文件数据流", ("from", "via", "to", "risk_type", "severity")),
            ("code_links", "代码联动关系", ("from", "to", "relation", "risk_type", "severity")),
            ("entry_points", "外部入口", ("file", "function", "line", "risk")),
        ):
            rows = threat_model.get(key)
            if not isinstance(rows, list) or not rows:
                continue
            lines.extend(["", label])
            for index, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    continue
                values = [row.get(field) for field in path]
                text = "；".join(_plain_report_text(value, 1000) for value in values if value not in (None, "", []))
                if text:
                    lines.append(f"{index}. {text}")
    fullchain = result.get("fullchain")
    if isinstance(fullchain, dict):
        lines.extend(["", "全链路审计阶段"])
        for key, label in (("summary", "阶段结论"), ("verdict", "审计判断"), ("verdict_detail", "判断说明")):
            value = fullchain.get(key)
            if value not in (None, "", []):
                lines.append(f"{label}：{_plain_report_text(value, 2000)}")
        plain = fullchain.get("plain_report")
        if isinstance(plain, dict):
            for key, label in (("verdict", "结论"), ("verdict_detail", "结论说明")):
                if plain.get(key):
                    lines.append(f"{label}：{_plain_report_text(plain[key], 2000)}")
            severity_rows = plain.get("severity_table")
            if isinstance(severity_rows, list) and severity_rows:
                lines.append("风险分布：")
                for row in severity_rows:
                    if isinstance(row, dict):
                        lines.append(
                            f"{_plain_report_text(row.get('level'), 32)}：{_safe_int(row.get('count'))} 项；"
                            f"{_plain_report_text(row.get('meaning'), 500)}"
                        )
            actions = plain.get("next_steps")
            if isinstance(actions, list) and actions:
                lines.append("建议处置：")
                lines.extend(f"{index}. {_plain_report_text(item, 1000)}" for index, item in enumerate(actions, 1))
            stage_findings = plain.get("findings")
            if isinstance(stage_findings, list) and stage_findings:
                lines.append("全链路重点发现：")
                for index, item in enumerate(stage_findings, 1):
                    if isinstance(item, dict):
                        lines.append(
                            f"{index}. {_plain_report_text(item.get('title'), 300)}；"
                            f"严重度：{_plain_report_text(item.get('severity'), 32)}；"
                            f"位置：{_plain_report_text(item.get('where'), 500)}；"
                            f"说明：{_plain_report_text(item.get('what_it_means'), 1000)}"
                        )
        recon = fullchain.get("recon")
        if isinstance(recon, dict):
            lines.extend(["", "侦察阶段"])
            if recon.get("framework_hints"):
                lines.append("识别到的框架：")
                lines.extend(
                    f"{index}. {_plain_report_text(item, 300)}"
                    for index, item in enumerate(recon["framework_hints"], 1)
                )
            if recon.get("hot_files"):
                lines.append("重点检查文件：")
                lines.extend(
                    f"{index}. {_plain_report_text(item, 500)}" for index, item in enumerate(recon["hot_files"], 1)
                )
            if recon.get("tainted_sink_count") is not None:
                lines.append(f"高风险入口数量：{_safe_int(recon['tainted_sink_count'])}")
        verification = fullchain.get("verification")
        if isinstance(verification, dict):
            lines.extend(["", "验证阶段"])
            verification_labels = (
                ("targets", "验证目标数"),
                ("verified", "独立验证完成数"),
                ("llm_confirmed", "模型推理判断为可利用数"),
                ("sandbox_used", "是否运行隔离沙箱"),
                ("sandbox_attempted", "沙箱尝试数"),
                ("sandbox_script_reported_confirmed", "沙箱脚本回报 confirmed 数"),
                ("sandbox_script_reported_total", "沙箱脚本回报总数"),
                ("sandbox_script_report_status", "沙箱回报复核状态"),
            )
            for key, label in verification_labels:
                if verification.get(key) is not None:
                    value = verification[key]
                    if isinstance(value, bool):
                        value = "是" if value else "否"
                    lines.append(f"{label}：{_plain_report_text(value, 300)}")
            if verification.get("sandbox_error"):
                lines.append(f"沙箱执行问题：{_plain_report_text(verification['sandbox_error'], 1000)}")
        board = fullchain.get("board")
        if isinstance(board, dict):
            lines.extend(["", "协作黑板统计"])
            for key, label in (
                ("facts", "事实记录"),
                ("hypotheses", "假设记录"),
                ("confirmed", "已确认记录"),
                ("refuted", "已排除记录"),
                ("intents", "行动意图"),
                ("open_intents", "待处理意图"),
            ):
                if board.get(key) is not None:
                    lines.append(f"{label}：{_safe_int(board[key])}")
    audit_board = result.get("audit_board")
    if isinstance(audit_board, dict):
        lines.extend(["", "审计协作结论"])
        if audit_board.get("summary"):
            lines.append(f"协作摘要：{_plain_report_text(audit_board['summary'], 2000)}")
        if isinstance(audit_board.get("confirmed"), list):
            for index, item in enumerate(audit_board["confirmed"], 1):
                if isinstance(item, dict):
                    lines.append(
                        f"{index}. {_plain_report_text(item.get('title'), 300)}；"
                        f"严重度：{_plain_report_text(item.get('severity'), 32)}；"
                        f"位置：{_plain_report_text(item.get('file_path'), 500)}:{item.get('line') or '未提供'}"
                    )
    discussion = result.get("discussion")
    if isinstance(discussion, dict):
        if discussion.get("consensus"):
            lines.extend(["", "多 Agent 复核结论", _plain_report_text(discussion["consensus"], 4000)])
        actions = discussion.get("action_items")
        if isinstance(actions, list) and actions:
            lines.append("后续行动：")
            lines.extend(f"{index}. {_plain_report_text(item, 1000)}" for index, item in enumerate(actions, 1))
    return "\n".join(lines).strip() + "\n"


def persist_security_report(
    db: Session,
    *,
    owner: User,
    project_id: int,
    report_kind: str,
    task_name: str,
    execution_status: str,
    result: dict | None,
    error_message: str = "",
    coverage: dict | None = None,
) -> int:
    """Create a reportable task and immutable result snapshot for the current account.

    Partial and failed executions remain reportable. They are never converted to a
    successful full-coverage claim; missing scope is made explicit in coverage.
    """
    if execution_status not in {"success", "partial", "failed"}:
        raise ValueError("unsupported security report execution status")
    require_project_access(db, int(project_id), owner, need_write=False)
    project = db.get(Project, int(project_id))
    if project is None or project.status == "deleted":
        raise NotFoundError("报告所属项目不存在")

    payload = _json_copy(result if isinstance(result, dict) else {})
    supplied_coverage = coverage if isinstance(coverage, dict) else {}
    compliance = payload.get("compliance") if isinstance(payload.get("compliance"), dict) else {}
    normalized_coverage = _json_copy({**_json_copy(compliance), **_json_copy(supplied_coverage)})
    for field in ("unscanned", "skipped"):
        values = normalized_coverage.get(field)
        if isinstance(values, list):
            normalized_coverage[field] = [_plain_report_text(item, 1000) for item in values]
    verification_status = infer_coverage_status(execution_status, normalized_coverage)
    normalized_coverage["verification_status"] = verification_status
    normalized_coverage["coverage_status"] = verification_status
    if verification_status != "complete" and not normalized_coverage.get("unscanned"):
        normalized_coverage["unscanned"] = ["存在未完成或未能确认的扫描范围"]
    safe_error = _plain_report_text(error_message, 500)
    raw_findings = payload.get("findings")
    raw_findings = [item for item in raw_findings if isinstance(item, dict)] if isinstance(raw_findings, list) else []
    score_value = payload.get("risk_score")
    try:
        score_number = float(score_value) if not isinstance(score_value, bool) else math.nan
    except (TypeError, ValueError, OverflowError):
        score_number = math.nan
    score_available = isinstance(score_value, (int, float)) and math.isfinite(score_number)
    score = max(0, min(100, int(score_number))) if score_available else None
    payload["risk_score"] = score
    payload["summary"] = _plain_report_text(payload.get("summary"))
    findings = []
    for finding in raw_findings:
        safe_finding = dict(finding)
        for field, limit in (
            ("title", 500),
            ("category", 100),
            ("severity", 32),
            ("file_path", 1000),
            ("file_name", 500),
            ("description", 20000),
            ("evidence", 20000),
            ("exploit_scenario", 20000),
            ("fix_suggestion", 20000),
            ("suggestion", 20000),
            ("verification", 100),
            ("verification_reason", 1000),
            ("cwe", 100),
            ("owasp", 200),
            ("source", 100),
        ):
            if safe_finding.get(field) is not None:
                safe_finding[field] = _plain_report_text(safe_finding[field], limit)
        findings.append(safe_finding)
    payload["findings"] = findings
    severity_counts = {severity: 0 for severity in _SEVERITIES}
    for finding in findings:
        severity = str(finding.get("severity") or "")
        if severity in severity_counts and finding.get("verification") != "refuted":
            severity_counts[severity] += 1
    now = datetime.utcnow()
    task = ReviewTask(
        user_id=int(owner.id),
        project_id=int(project_id),
        task_name=_plain_report_text(task_name, 100) or "安全审计",
        review_type=report_kind,
        status="failed" if execution_status == "failed" else "success",
        total_files=max(0, _safe_int(payload.get("file_count") or normalized_coverage.get("total_file_count"))),
        processed_files=max(0, _safe_int(payload.get("file_count") or normalized_coverage.get("scanned_file_count"))),
        total_issues=len(findings),
        severe_issues=severity_counts["严重"],
        high_issues=severity_counts["高"],
        medium_issues=severity_counts["中"],
        low_issues=severity_counts["低"],
        # The snapshot carries score_available=false when no score exists;
        # keep the legacy non-null integer column neutral instead of implying 100.
        score=score if score is not None else 0,
        summary=(
            _plain_report_text(payload.get("summary"))
            or _plain_report_text(safe_error)
            or "扫描未返回摘要，详情见覆盖记录与执行原因。"
        ),
        model_name=_text(payload.get("model"), 50) or None,
        start_time=now,
        end_time=now,
        duration_ms=max(0, _safe_int(payload.get("duration_ms"))),
        error_message=safe_error or None,
        coverage={
            **normalized_coverage,
            "execution_status": execution_status,
            "score_available": score is not None,
        },
        rules_snapshot=[],
    )
    db.add(task)
    db.flush()

    code_files = (
        db.query(CodeFile)
        .filter(
            CodeFile.project_id == int(project_id),
            CodeFile.status == "active",
        )
        .all()
    )
    files_by_path: dict[str, CodeFile] = {}
    files_by_name: dict[str, CodeFile] = {}
    for code_file in code_files:
        if code_file.file_path:
            files_by_path.setdefault(str(code_file.file_path), code_file)
        files_by_name.setdefault(str(code_file.file_name), code_file)

    linked_file_ids: set[int] = set()
    for finding in findings:
        file_path = str(finding.get("file_path") or finding.get("file_name") or "")
        code_file = files_by_path.get(file_path) or files_by_name.get(file_path)
        file_id = finding.get("file_id")
        candidate_file_id = _safe_int(file_id, -1) if file_id is not None else -1
        if code_file is None and candidate_file_id > 0:
            candidate = db.get(CodeFile, candidate_file_id)
            if candidate is not None and candidate.project_id == int(project_id) and candidate.status == "active":
                code_file = candidate
        if code_file is not None and code_file.id not in linked_file_ids:
            linked_file_ids.add(int(code_file.id))
            snapshot = {
                "file_name": code_file.file_name,
                "file_path": code_file.file_path,
                "language": code_file.language,
            }
            db.add(
                ReviewTaskFile(
                    task_id=task.id,
                    file_id=code_file.id,
                    version_no=code_file.version_no,
                    content_sha256=hashlib.sha256((code_file.content or "").encode("utf-8")).hexdigest(),
                    file_snapshot=snapshot,
                )
            )
        verification = str(finding.get("verification") or "")
        status = (
            "refuted"
            if verification == "refuted"
            else ("pending_review" if verification in {"plausible", "unreviewed"} else "unfixed")
        )
        line_number = max(0, _safe_int(finding.get("line_number")))
        end_line = max(line_number, _safe_int(finding.get("end_line"), line_number))
        db.add(
            ReviewIssue(
                task_id=task.id,
                file_id=code_file.id if code_file else None,
                file_name=code_file.file_name if code_file else (file_path.rsplit("/", 1)[-1] if file_path else None),
                line_number=line_number,
                end_line=end_line,
                issue_type=_text(finding.get("category") or "安全漏洞", 50),
                severity=_text(finding.get("severity") or "未分级", 20),
                title=_text(finding.get("title") or "安全问题", 200),
                description=_text(finding.get("exploit_scenario") or finding.get("description") or "未提供影响说明"),
                suggestion=_text(finding.get("fix_suggestion") or finding.get("suggestion")),
                status=status,
                owasp=_text(finding.get("owasp"), 128) or None,
                cwe=_text(finding.get("cwe"), 64) or None,
                evidence=_text(finding.get("evidence")) or None,
                exploit_scenario=_text(finding.get("exploit_scenario")) or None,
                references_json=finding.get("references") if isinstance(finding.get("references"), list) else None,
                confidence=_safe_confidence(finding.get("confidence")),
                source=_text(finding.get("source") or "security_scan", 16),
                human_review_status="pending" if verification in {"plausible", "unreviewed"} else None,
            )
        )

    report_body = _report_markdown(
        task_name=task.task_name or "安全审计",
        project_name=_plain_report_text(project.project_name, 500),
        execution_status=execution_status,
        summary=_plain_report_text(payload.get("summary")),
        score=score,
        findings=findings,
        coverage=normalized_coverage,
        result=payload,
        error_message=safe_error,
    )
    report_content = {
        "source": report_kind,
        "schema_version": "security-audit-report-v1",
        "execution_status": execution_status,
        "score_available": score is not None,
        "risk_score": score,
        "coverage": normalized_coverage,
        "findings": findings,
        "threat_model": clean_report_payload(
            payload.get("threat_model") if isinstance(payload.get("threat_model"), dict) else {}
        ),
        "discussion": clean_report_payload(
            payload.get("discussion") if isinstance(payload.get("discussion"), dict) else {}
        ),
        "fullchain": clean_report_payload(
            payload.get("fullchain") if isinstance(payload.get("fullchain"), dict) else {}
        ),
        "audit_board": clean_report_payload(
            payload.get("audit_board") if isinstance(payload.get("audit_board"), dict) else {}
        ),
        "compliance": compliance,
        "report_md": report_body,
    }
    db.add(
        ReviewReport(
            task_id=task.id,
            user_id=int(owner.id),
            content_json=_json_copy(report_content),
            summary=task.summary,
            score=score if score is not None else 0,
            create_time=now,
        )
    )
    db.commit()
    db.refresh(task)
    return int(task.id)
