"""Append-only report artifacts for project-scoped enterprise security tasks."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.enterprise_security_artifact import EnterpriseSecurityArtifact

_MAX_REPORT_BYTES = 16 * 1024 * 1024
_MAX_SUMMARY_BYTES = 48 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_AUDIT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TERMINATION_REASON = re.compile(r"^[a-z0-9_.-]{0,64}$")


def _encode_json(value: Any, *, label: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"安全报告{label}必须是有效 JSON") from exc


def create_artifact_in_transaction(
    db: Session,
    *,
    team,
    task,
    lease_generation: int,
    payload: dict[str, Any],
    expected_coverage: str,
) -> dict[str, Any]:
    """Insert the immutable body and return a bounded task-result reference.

    Callers must first lock and authorize the current team, task, project, and
    lease in this same transaction. No report can be inserted independently of
    the task state transition that stores its reference.
    """
    if team.project_id is None:
        raise ConflictError("项目安全报告必须绑定企业项目")
    if not isinstance(payload, dict):
        raise ValidationError("安全报告制品必须是对象")
    required = {"audit_run_id", "source_sha256", "coverage_status", "termination_reason", "summary", "report"}
    if set(payload) != required:
        raise ValidationError("安全报告制品字段不完整或包含未声明字段")

    audit_run_id = payload["audit_run_id"]
    source_sha256 = payload["source_sha256"]
    coverage_status = payload["coverage_status"]
    termination_reason = payload["termination_reason"]
    summary = payload["summary"]
    report = payload["report"]
    if not isinstance(audit_run_id, str) or not _AUDIT_ID.fullmatch(audit_run_id):
        raise ValidationError("安全报告 audit_run_id 格式无效")
    if not isinstance(source_sha256, str) or not _SHA256.fullmatch(source_sha256):
        raise ValidationError("安全报告源码摘要必须是小写 SHA-256")
    if not isinstance(coverage_status, str) or coverage_status not in {"complete", "incomplete", "failed"}:
        raise ValidationError("安全报告覆盖状态无效")
    coverage_matches = (
        coverage_status in {"incomplete", "failed"}
        if expected_coverage == "non_success"
        else coverage_status == expected_coverage
    )
    if not coverage_matches:
        raise ConflictError("安全报告覆盖状态与团队任务终态不一致")
    if not isinstance(termination_reason, str) or not _TERMINATION_REASON.fullmatch(termination_reason):
        raise ValidationError("安全报告终止原因格式无效")
    if (coverage_status == "complete") != (termination_reason == ""):
        raise ValidationError("完整报告必须无终止原因，未完成报告必须说明终止原因")
    if not isinstance(summary, dict) or not isinstance(report, dict):
        raise ValidationError("安全报告摘要和正文必须是 JSON 对象")

    summary_json = _encode_json(summary, label="摘要")
    report_json = _encode_json(report, label="正文")
    if len(summary_json.encode("utf-8")) > _MAX_SUMMARY_BYTES:
        raise ValidationError("安全报告摘要超出上限")
    report_bytes = report_json.encode("utf-8")
    if len(report_bytes) > _MAX_REPORT_BYTES:
        raise ValidationError("安全报告正文超过 16 MiB 上限")

    task_id = int(task.id)
    attempt_no = int(task.attempt_count)
    if attempt_no <= 0 or int(task.lease_generation) != int(lease_generation):
        raise ConflictError("安全报告尝试次数或租约代次已变化")
    artifact = EnterpriseSecurityArtifact(
        team_id=int(team.id),
        task_id=task_id,
        enterprise_id=int(team.enterprise_id),
        project_id=int(team.project_id),
        conversation_id=int(team.conversation_id),
        attempt_no=attempt_no,
        lease_generation=int(lease_generation),
        audit_run_id=audit_run_id,
        source_sha256=source_sha256,
        report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        coverage_status=coverage_status,
        termination_reason=termination_reason,
        summary_json=summary_json,
        report_json=report_json,
    )
    db.add(artifact)
    try:
        db.flush()
    except IntegrityError as exc:
        # A duplicate attempt or audit ID is a state conflict, not a partial write.
        raise ConflictError("该团队任务尝试已存在安全报告制品") from exc
    return {
        "security_artifact_id": int(artifact.id),
        "report_sha256": artifact.report_sha256,
        "coverage_status": coverage_status,
        "termination_reason": termination_reason,
        "summary": summary,
    }


def get_team_security_artifact(
    db: Session,
    *,
    enterprise_id: int,
    user_id: int,
    team_id: int,
    artifact_id: int,
) -> dict[str, Any]:
    """Read an artifact only when the live team result and project scopes intersect."""
    _detail, artifact = _authorized_team_artifact(
        db,
        enterprise_id=enterprise_id,
        user_id=user_id,
        team_id=team_id,
        artifact_id=artifact_id,
        load_report=True,
    )
    try:
        summary = json.loads(artifact.summary_json)
        report = json.loads(artifact.report_json)
    except (TypeError, ValueError) as exc:
        raise ConflictError("安全报告制品无法解析") from exc
    if not isinstance(summary, dict) or not isinstance(report, dict):
        raise ConflictError("安全报告制品格式无效")
    if hashlib.sha256(_encode_json(report, label="正文").encode("utf-8")).hexdigest() != artifact.report_sha256:
        raise ConflictError("安全报告制品摘要校验失败")
    return {
        "artifact_id": int(artifact.id),
        "team_id": int(artifact.team_id),
        "task_id": int(artifact.task_id),
        "attempt_no": int(artifact.attempt_no),
        "lease_generation": int(artifact.lease_generation),
        "enterprise_id": int(artifact.enterprise_id),
        "project_id": int(artifact.project_id),
        "audit_run_id": str(artifact.audit_run_id),
        "source_sha256": str(artifact.source_sha256),
        "report_sha256": str(artifact.report_sha256),
        "coverage_status": str(artifact.coverage_status),
        "termination_reason": str(artifact.termination_reason),
        "summary": summary,
        "report": report,
        "created_at": artifact.created_at,
    }


def check_team_security_artifact_access(
    db: Session,
    *,
    enterprise_id: int,
    user_id: int,
    team_id: int,
    artifact_id: int,
) -> None:
    """Revalidate live scopes without loading the potentially large report body."""
    _authorized_team_artifact(
        db,
        enterprise_id=enterprise_id,
        user_id=user_id,
        team_id=team_id,
        artifact_id=artifact_id,
        load_report=False,
    )


def _authorized_team_artifact(
    db: Session,
    *,
    enterprise_id: int,
    user_id: int,
    team_id: int,
    artifact_id: int,
    load_report: bool,
):
    """Resolve one result pointer against current conversation and project grants."""
    from app.services import enterprise_agent_team_service

    detail = enterprise_agent_team_service.get_team_detail(
        db,
        enterprise_id=int(enterprise_id),
        user_id=int(user_id),
        team_id=int(team_id),
    )
    referenced_tasks = {
        int(item["task_id"]): int(item["result"]["security_artifact_id"])
        for item in detail["tasks"]
        if isinstance(item, dict)
        and isinstance(item.get("result"), dict)
        and type(item["result"].get("security_artifact_id")) is int
    }
    artifact_query = (
        db.query(EnterpriseSecurityArtifact)
        if load_report
        else db.query(
            EnterpriseSecurityArtifact.id,
            EnterpriseSecurityArtifact.task_id,
            EnterpriseSecurityArtifact.project_id,
        )
    )
    artifact = artifact_query.filter(
        EnterpriseSecurityArtifact.id == int(artifact_id),
        EnterpriseSecurityArtifact.team_id == int(team_id),
        EnterpriseSecurityArtifact.enterprise_id == int(enterprise_id),
    ).first()
    if artifact is None or referenced_tasks.get(int(artifact.task_id)) != int(artifact.id):
        raise NotFoundError("安全报告制品不存在")
    if detail.get("project_id") != int(artifact.project_id):
        raise NotFoundError("安全报告制品不存在")
    return detail, artifact
