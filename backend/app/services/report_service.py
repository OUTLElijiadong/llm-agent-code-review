"""
报告服务模块
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from sqlalchemy import and_, false, func, or_, select, true
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement

from app.ai.scoring import compute_score
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, PermissionError
from app.core.pagination import Pagination
from app.core.permission_codes import PermissionCode
from app.models.code_file import CodeFile
from app.models.pentest import PentestEngagement, PentestFinding
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.user import User
from app.services import rbac_service
from app.services.project_member_service import (
    get_visible_project_ids,
    project_resource_capabilities,
    require_project_access,
    require_project_execution,
)
from app.services.report_exporter import build_report_score_facts
from app.services.report_text import clean_report_markdown, clean_report_payload, clean_report_text

_REPORT_BODY_WHITESPACE = (
    " ", "\t", "\n", "\r", "\f", "\v", "\x1c", "\x1d", "\x1e", "\x1f",
    "\x85", "\u00a0", "\u1680", *(chr(codepoint) for codepoint in range(0x2000, 0x200B)),
    "\u2028", "\u2029", "\u202f", "\u205f", "\u3000",
)
_REPORT_BODY_WHITESPACE_CHARS = "".join(_REPORT_BODY_WHITESPACE)
_REPORT_BODY_WHITESPACE_PATTERN = f"[{_REPORT_BODY_WHITESPACE_CHARS}]+"
_UNRELIABLE_SANDBOX_DURATION_NOTE = "历史环境总运行时间不能代表本次测试耗时；单次耗时未可靠记录。"
_SANDBOX_SCORE_UNAVAILABLE_REASON = "沙箱测试执行与覆盖证据不是代码质量或漏洞风险评分，当前没有可核验的评分依据。"
_UNAVAILABLE_SCORE_CLAIM = re.compile(
    r"(?P<label>(?:综合|代码质量|代码|风险|总)?评分)\s*(?:为|是|[:：])?\s*"
    r"\d{1,3}(?:\.\d+)?(?:\s*/\s*100)?\s*(?:分)?"
)


def display_report_summary(summary: str | None, *, score_available: bool, score: int | None = None) -> str:
    """Hide legacy prose scores whenever the canonical report score is unavailable."""
    text = summary or ""
    if score_available:
        if score is None:
            return text
        # 摘要里的旧分数必须与规则分（含小菱重判分）一致。
        return _UNAVAILABLE_SCORE_CLAIM.sub(lambda match: f"{match.group('label')} {score}", text)
    return _UNAVAILABLE_SCORE_CLAIM.sub(
        lambda match: f"{match.group('label')}未评估",
        text,
    )


def _non_whitespace_report_body(report_body, database_dialect: str):
    """Build a compact expression that detects bodies containing real text."""
    if database_dialect == "sqlite":
        # SQLite trim accepts a character set. Chained replace() calls expand into
        # a deeply nested SQL expression and can overflow SQLite's parser stack.
        return func.trim(report_body, _REPORT_BODY_WHITESPACE_CHARS)
    if database_dialect in {"mysql", "mariadb"}:
        return func.regexp_replace(report_body, _REPORT_BODY_WHITESPACE_PATTERN, "")
    if database_dialect == "postgresql":
        return func.regexp_replace(report_body, _REPORT_BODY_WHITESPACE_PATTERN, "", "g")

    # Keep a portable fallback for engines without regex replacement.
    normalized = report_body
    for whitespace in _REPORT_BODY_WHITESPACE:
        normalized = func.replace(normalized, whitespace, "")
    return normalized


def _report_duration_ms(task: ReviewTask) -> int | None:
    if task.review_type == "sandbox_test":
        coverage = task.coverage if isinstance(task.coverage, dict) else {}
        if coverage.get("duration_source") != "measured_execution":
            return None
    duration = int(task.duration_ms or 0)
    return duration if duration > 0 else None


def _security_report_snapshot(db: Session, task: ReviewTask) -> dict | None:
    if task.review_type not in {"security_scan", "fullchain_audit"}:
        return None
    report = db.query(ReviewReport).filter(
        ReviewReport.task_id == int(task.id),
        ReviewReport.user_id == int(task.user_id),
    ).first()
    content = report.content_json if report and isinstance(report.content_json, dict) else {}
    if (
        content.get("source") != task.review_type
        or not isinstance(content.get("report_md"), str)
        or not content["report_md"].strip()
    ):
        return None
    return clean_report_payload(content)


def _sandbox_rule_score_available(task: ReviewTask) -> bool:
    """沙箱验证分只认按规则生成、带检查项样本的新版本；历史无依据记录保持不出分。"""
    from app.services.sandbox_service import SANDBOX_SCORE_VERSION

    coverage = task.coverage if isinstance(task.coverage, dict) else {}
    return (
        task.review_type == "sandbox_test"
        and task.status in {"success", "failed"}
        and str(getattr(task, "score_version", "") or "") == SANDBOX_SCORE_VERSION
        and coverage.get("score_available") is True
        and coverage.get("score_basis") == SANDBOX_SCORE_VERSION
    )


def _pentest_rule_score_available(task: ReviewTask) -> bool:
    from app.services.pentest_service import PENTEST_SCORE_VERSION

    coverage = task.coverage if isinstance(task.coverage, dict) else {}
    return (
        task.status in _REPORTABLE_TASK_STATUSES
        and coverage.get("score_available") is True
        and coverage.get("score_basis") == PENTEST_SCORE_VERSION
    )


def _score_is_available(task: ReviewTask) -> bool:
    coverage = task.coverage if isinstance(task.coverage, dict) else {}
    explicit = coverage.get("score_available")
    if task.review_type == "sandbox_test":
        # 沙箱验证分 = 已执行检查项通过率；失败的测试同样按规则给出真实低分。
        return _sandbox_rule_score_available(task)
    if task.review_type == "pentest":
        # 渗透分按已测范围内确认发现扣分；旧记录的 0 只是占位，必须带规则回执。
        if _pentest_rule_score_available(task):
            return True
        coverage_status = str(coverage.get("coverage_status") or "").strip().lower()
        return task.status == "success" and explicit is True and coverage_status == "complete"
    from app.services.score_scope import issue_score_scope

    # 问题集评分：覆盖范围只决定分数的适用范围，不再隐藏分数。
    return bool(issue_score_scope(task)["available"])


# 部分覆盖仍是可交付结果：报告必须展示已完成内容，并明确列出未覆盖范围。
_REPORTABLE_TASK_STATUSES = ("success", "partial", "failed", "cancelled", "expired", "stopped")


def _sandbox_coverage_for_display(coverage: dict) -> dict:
    from app.services.sandbox_service import SANDBOX_SCORE_VERSION

    result = dict(coverage)
    if result.get("score_available") is True and result.get("score_basis") == SANDBOX_SCORE_VERSION:
        return result
    sample = result.pop("score_sample", None)
    result.update({
        "score_available": False,
        "score_basis": None,
        "score_sample": None,
        "score_unavailable_reason": _SANDBOX_SCORE_UNAVAILABLE_REASON,
    })
    if isinstance(sample, dict):
        # Keep the measured counts without presenting them as a score basis.
        result.setdefault("dynamic_test_counts", sample)
    return result


def _task_coverage(task: ReviewTask, requested_mode: str | None = None) -> dict:
    coverage = task.coverage if isinstance(task.coverage, dict) else {}
    if coverage:
        # Verification completion does not prove that the full assessment
        # scope was covered. Keep the coverage status explicit for old or
        # partial payloads so a hidden score has a readable explanation.
        result = dict(coverage)
        if result.get("coverage_status") not in {"complete", "partial", "failed", "unknown"}:
            result["coverage_status"] = "unknown"
    else:
        result = {
            "coverage_status": "unknown",
            "unscanned": ["历史任务未保存覆盖清单，无法确认实际扫描范围"],
        }
    if task.review_type == "sandbox_test":
        result = _sandbox_coverage_for_display(result)
        if requested_mode in {"whitebox", "blackbox", "combined"}:
            result["requested_mode"] = requested_mode
        return result
    return result


def _sandbox_domain_data_for_display(task: ReviewTask, content: dict) -> dict:
    """Preserve sandbox evidence while removing legacy pass-rate score claims."""
    result = dict(content)
    requested_mode = _sandbox_requested_mode(task, content)
    raw_coverage = content.get("coverage")
    coverage = (
        _sandbox_coverage_for_display(raw_coverage)
        if isinstance(raw_coverage, dict)
        else _task_coverage(task, requested_mode)
    )
    if requested_mode in {"whitebox", "blackbox", "combined"}:
        coverage["requested_mode"] = requested_mode
    result["coverage"] = coverage
    if _sandbox_rule_score_available(task):
        result.update({
            "score": task.score,
            "score_available": True,
            "score_basis": coverage.get("score_basis"),
            "score_sample": coverage.get("score_sample"),
        })
    else:
        result.update({
            "score": None,
            "score_available": False,
            "score_basis": None,
            "score_sample": None,
        })
    if isinstance(content.get("report_md"), str):
        from app.services.sandbox_report_summary import scope_sandbox_report_markdown, summarize_sandbox_report

        result["report_md"] = scope_sandbox_report_markdown(content["report_md"], requested_mode)
        result["report_issue_summary"] = summarize_sandbox_report(result["report_md"])
    from app.services.sandbox_report_summary import scope_sandbox_findings

    if isinstance(content.get("findings"), list):
        result["findings"] = scope_sandbox_findings(content["findings"], requested_mode)
    return result


def _sandbox_requested_mode(task: ReviewTask, content: dict | None = None) -> str | None:
    """Resolve the requested sandbox mode from immutable report/task coverage facts."""
    report_coverage = content.get("coverage") if isinstance(content, dict) else None
    report_coverage = report_coverage if isinstance(report_coverage, dict) else {}
    task_coverage = task.coverage if isinstance(task.coverage, dict) else {}
    evidence = content.get("evidence") if isinstance(content, dict) else None
    evidence = evidence if isinstance(evidence, dict) else {}
    deterministic = evidence.get("deterministic_test_execution")
    deterministic = deterministic if isinstance(deterministic, dict) else {}
    generation = evidence.get("agent_test_generation")
    generation = generation if isinstance(generation, dict) else {}
    for candidate in (
        report_coverage.get("requested_mode"),
        report_coverage.get("test_mode"),
        task_coverage.get("requested_mode"),
        task_coverage.get("test_mode"),
        content.get("test_mode") if isinstance(content, dict) else None,
        deterministic.get("requested_mode"),
        generation.get("mode"),
    ):
        if isinstance(candidate, str) and candidate.strip().lower() in {"whitebox", "blackbox", "combined"}:
            return candidate.strip().lower()
    return None


def display_report_score(task: ReviewTask, score: int | None) -> int | None:
    """Display only scores with an explicit basis; coverage remains a separate fact."""
    if not _score_is_available(task):
        return None
    if task.review_type not in {"sandbox_test", "pentest"}:
        from app.services.score_scope import issue_rule_score

        # 旧失败/部分任务的记录分可能是 0 占位，按已保存问题计数复算。
        if score is None or not _stored_score_is_trusted_input(task):
            return issue_rule_score(task)
    return score


def metric_report_score(task: ReviewTask, score: int | None) -> int | None:
    """工作台均分与趋势只统计完整覆盖的分数；部分范围分只在报告中展示。"""
    if task.review_type != "sandbox_test" and _score_scope_of(task) != "complete":
        return None
    return display_report_score(task, score)


def _stored_score_is_trusted_input(task: ReviewTask) -> bool:
    """记录分可作为展示输入：完整覆盖、历史成功任务，或部分覆盖下成功落库的规则分。"""
    from app.services.score_scope import stored_score_is_rule_result

    scope = _score_scope_of(task)
    if scope in {"complete", "historical"}:
        return True
    return scope == "partial" and task.status == "success" and stored_score_is_rule_result(task)


def _score_scope_of(task: ReviewTask) -> str | None:
    coverage = task.coverage if isinstance(task.coverage, dict) else {}
    if task.review_type == "sandbox_test":
        return "complete" if str(coverage.get("verification_status") or "") == "complete" else "partial"
    if task.review_type == "pentest":
        return str(coverage.get("score_scope") or coverage.get("coverage_status") or "partial")
    from app.services.score_scope import issue_score_scope

    return issue_score_scope(task)["scope"]


def score_scope_payload(task: ReviewTask) -> dict:
    """报告与列表共用的评分范围说明。"""
    from app.services.score_scope import NO_SCOPE_REASON, SCOPE_LABELS, issue_score_scope

    if task.review_type not in {"sandbox_test", "pentest"}:
        return issue_score_scope(task)
    available = _score_is_available(task)
    scope = _score_scope_of(task) if available else None
    coverage = task.coverage if isinstance(task.coverage, dict) else {}
    return {
        "available": available,
        "scope": scope,
        "label": SCOPE_LABELS.get(scope or "", "不出分"),
        "reason": None if available else str(coverage.get("score_unavailable_reason") or NO_SCOPE_REASON),
    }


def is_report_available(task: ReviewTask | None) -> bool:
    """终态任务均保留报告视图；执行状态由报告明确呈现，不推断为成功。"""
    return bool(task and task.status in _REPORTABLE_TASK_STATUSES)


def sandbox_verification_status(task: ReviewTask | None) -> str:
    """Return verified test coverage state; old sandbox reports remain explicitly unknown."""
    if task is None or task.review_type != "sandbox_test":
        return "not_applicable"
    coverage = task.coverage if isinstance(task.coverage, dict) else {}
    status = str(coverage.get("verification_status") or "unknown")
    return status if status in {"complete", "partial", "failed"} else "unknown"


_REPORT_SOURCE_PERMISSION_BY_TYPE = {
    "security_scan": PermissionCode.SECURITY_VIEW,
    "fullchain_audit": PermissionCode.SECURITY_VIEW,
    "pentest": PermissionCode.PENTEST_VIEW,
}
_REPORT_BACKED_REVIEW_TYPES = frozenset({
    "sandbox_test", "pentest", "security_scan", "fullchain_audit",
})


def has_source_report_permission(db: Session, user: User, review_type: str | None) -> bool:
    """报告来源权限与报告列表、详情和导出共用；管理员由 RBAC 统一绕过。"""
    required = _REPORT_SOURCE_PERMISSION_BY_TYPE.get(str(review_type or ""))
    return required is None or rbac_service.check_permission(db, int(user.id), required)


def _standard_report_permission_filter(db: Session, user: User) -> bool:
    """Standard reports can include issue evidence and need the matching read permission."""
    return rbac_service.check_permission(db, int(user.id), PermissionCode.ISSUE_VIEW)


def _sandbox_report_snapshot(db: Session, task: ReviewTask) -> ReviewReport | None:
    report = db.query(ReviewReport).filter(
        ReviewReport.task_id == int(task.id),
        ReviewReport.user_id == int(task.user_id),
    ).first()
    content = report.content_json if report and isinstance(report.content_json, dict) else {}
    body = content.get("report_md")
    if content.get("source") != "sandbox_test" or not isinstance(body, str) or not body.strip():
        return None
    return report


def _pentest_report_engagement(db: Session, task: ReviewTask) -> PentestEngagement | None:
    report = db.query(ReviewReport).filter(
        ReviewReport.task_id == int(task.id),
        ReviewReport.user_id == int(task.user_id),
    ).first()
    content = report.content_json if report and isinstance(report.content_json, dict) else {}
    if content.get("source") != "pentest":
        return None
    engagements = db.query(PentestEngagement).filter(
        PentestEngagement.report_task_id == int(task.id),
        PentestEngagement.user_id == int(task.user_id),
        PentestEngagement.project_id == int(task.project_id),
    ).all()
    if len(engagements) != 1 or content.get("engagement_public_id") != engagements[0].public_id:
        return None
    return engagements[0]


def readable_pentest_engagement_ids(db: Session, user: User, engagement_ids: list[int]) -> set[int]:
    """批量返回当前账号实际可读且关联快照完整的渗透委托报告入口。"""
    if not engagement_ids:
        return set()
    if (
        not rbac_service.check_permission(db, int(user.id), PermissionCode.REPORT_VIEW)
        or not has_source_report_permission(db, user, "pentest")
    ):
        return set()

    visible_project_ids, _ = get_visible_project_ids(db, user)
    if not visible_project_ids:
        return set()

    linked_engagement = aliased(PentestEngagement)
    unique_report_link_count = (
        select(func.count(linked_engagement.id))
        .where(
            linked_engagement.report_task_id == ReviewTask.id,
            linked_engagement.user_id == ReviewTask.user_id,
            linked_engagement.project_id == ReviewTask.project_id,
        )
        .correlate(ReviewTask)
        .scalar_subquery()
    )

    query = db.query(PentestEngagement.id, ReviewReport.content_json).join(
        ReviewTask,
        and_(
            ReviewTask.id == PentestEngagement.report_task_id,
            ReviewTask.user_id == PentestEngagement.user_id,
            ReviewTask.project_id == PentestEngagement.project_id,
            ReviewTask.review_type == "pentest",
            ReviewTask.status.in_(_REPORTABLE_TASK_STATUSES),
        ),
    ).join(
        ReviewReport,
        and_(
            ReviewReport.task_id == ReviewTask.id,
            ReviewReport.user_id == ReviewTask.user_id,
            ReviewReport.content_json["source"].as_string() == "pentest",
            ReviewReport.content_json["engagement_public_id"].as_string() == PentestEngagement.public_id,
        ),
    ).filter(
        PentestEngagement.id.in_(engagement_ids),
        PentestEngagement.project_id.in_(visible_project_ids),
        unique_report_link_count == 1,
    )
    if not rbac_service.is_admin_user(db, int(user.id)):
        query = query.filter(PentestEngagement.user_id == int(user.id))
    readable_ids: set[int] = set()
    for engagement_id, content in query.all():
        if not isinstance(content, dict):
            continue
        # 渗透报告的正文由 PentestFinding 领域记录实时生成；快照只负责把
        # ReviewTask 与唯一委托绑定起来，不要求重复存一份 report_md。
        # 只有安全扫描/全链审计等来源型快照才以 report_md 作为正文完整性门槛。
        if content.get("source") == "pentest" and isinstance(content.get("engagement_public_id"), str):
            readable_ids.add(int(engagement_id))
    return readable_ids


def visible_report_review_types(db: Session, user: User) -> frozenset[str]:
    """返回同时满足通用报告权限和来源权限的报告任务类型。"""
    if not rbac_service.check_permission(db, int(user.id), PermissionCode.REPORT_VIEW):
        return frozenset()
    return frozenset(
        review_type
        for review_type in _REPORT_BACKED_REVIEW_TYPES
        if has_source_report_permission(db, user, review_type)
    )


def task_metrics_access_filter(db: Session, user: User | None) -> ColumnElement[bool]:
    """私域摘要查询沿用报告可用性、通用/来源权限与发起人/管理员范围。

    项目可见性仍由调用者限定；普通代码审查指标保留项目成员共享。
    ``None`` 仅供已有内部全局统计调用，HTTP 必须传入当前认证账号。
    """
    if user is None:
        return true()
    private_report_types = ("sandbox_test", "pentest", "security_scan", "fullchain_audit")
    public_metrics = or_(
        ReviewTask.review_type.notin_(private_report_types),
        ReviewTask.review_type.is_(None),
    )
    visible_types = visible_report_review_types(db, user)
    if not visible_types:
        return public_metrics
    available_domain_report = and_(
        ReviewTask.review_type.in_(visible_types),
        ReviewTask.status.in_(_REPORTABLE_TASK_STATUSES),
    )
    if not rbac_service.is_admin_user(db, int(user.id)):
        available_domain_report = and_(available_domain_report, ReviewTask.user_id == user.id)
    return or_(public_metrics, available_domain_report)


def get_readable_report_task(db: Session, user: User, task_id: int) -> ReviewTask:
    """保留发起人/管理员范围，并要求父项目的当前访问权，撤销后不能继续读取。"""
    task = db.get(ReviewTask, task_id)
    if not is_report_available(task):
        raise NotFoundError("报告不存在", code=40400)
    if task.user_id != user.id and not rbac_service.is_admin_user(db, int(user.id)):
        raise NotFoundError("报告不存在", code=40400)
    if not has_source_report_permission(db, user, task.review_type):
        raise NotFoundError("报告不存在", code=40400)
    is_source_report = task.review_type in _REPORT_BACKED_REVIEW_TYPES
    if not is_source_report and not _standard_report_permission_filter(db, user):
        raise NotFoundError("报告不存在", code=40400)
    try:
        require_project_access(db, task.project_id, user, need_write=False)
    except NotFoundError:
        raise NotFoundError("报告不存在", code=40400) from None
    return task


def list_reports(db: Session, user: User, project_id: int = None,
                 start_date: str = "", end_date: str = "",
                 page: int = 1, page_size: int = 20,
                 include_history: bool = False) -> dict:
    """查询报告列表

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID过滤
        start_date: 开始日期
        end_date: 结束日期
        page: 页码
        page_size: 每页数量

    Returns:
        dict: 分页响应
    """
    q = db.query(ReviewTask).filter(ReviewTask.status.in_(_REPORTABLE_TASK_STATUSES))
    if not include_history:
        # 默认列表展示所有已出分的终态报告（完整覆盖或仅覆盖已审范围）；
        # 完全没有已审内容的任务只在“含历史”视图中出现，并显示不出分原因。
        coverage_status = ReviewTask.coverage["coverage_status"].as_string()
        q = q.filter(
            ReviewTask.status.in_(("success", "partial", "failed")),
            ReviewTask.score.is_not(None),
            or_(
                coverage_status.in_(("complete", "completed", "verified")),
                ReviewTask.processed_files > 0,
                ReviewTask.total_issues > 0,
                and_(ReviewTask.status == "success", ReviewTask.coverage.is_(None), ReviewTask.total_files > 0),
                ReviewTask.review_type.in_(("sandbox_test", "pentest")),
            ),
        )
    # API 路由已有 REPORT_VIEW 门禁；服务层仍需独立 fail-closed，避免其他
    # 调用方绕过路由后把无权限任务计入总数或分页卡片。
    report_permission = rbac_service.check_permission(db, int(user.id), PermissionCode.REPORT_VIEW)
    issue_permission = _standard_report_permission_filter(db, user)
    administrator = rbac_service.is_admin_user(db, int(user.id))
    if not report_permission:
        q = q.filter(false())
    visible_ids, _ = get_visible_project_ids(db, user)
    visible_project_ids = {int(project_id) for project_id in visible_ids}
    q = q.filter(ReviewTask.project_id.in_(visible_ids))
    if not administrator:
        q = q.filter(ReviewTask.user_id == user.id)
    if project_id:
        q = q.filter(ReviewTask.project_id == project_id)
    if start_date:
        q = q.filter(ReviewTask.create_time >= start_date)
    if end_date:
        q = q.filter(ReviewTask.create_time <= end_date + " 23:59:59")

    if not rbac_service.check_permission(db, int(user.id), PermissionCode.SECURITY_VIEW):
        q = q.filter(ReviewTask.review_type.notin_(("security_scan", "fullchain_audit")))
    if not rbac_service.check_permission(db, int(user.id), PermissionCode.PENTEST_VIEW):
        q = q.filter(ReviewTask.review_type != "pentest")
    if not issue_permission:
        q = q.filter(ReviewTask.review_type.in_(tuple(_REPORT_BACKED_REVIEW_TYPES)))

    report_body = ReviewReport.content_json["report_md"].as_string()
    database_dialect = db.get_bind().dialect.name
    non_whitespace_report_body = _non_whitespace_report_body(report_body, database_dialect)
    if database_dialect == "sqlite":
        report_body_is_string = func.json_type(ReviewReport.content_json, "$.report_md") == "text"
    elif database_dialect in {"mysql", "mariadb"}:
        report_body_is_string = func.json_type(
            func.json_extract(ReviewReport.content_json, "$.report_md")
        ) == "STRING"
    elif database_dialect == "postgresql":
        report_body_is_string = func.json_typeof(ReviewReport.content_json["report_md"]) == "string"
    else:
        report_body_is_string = report_body.is_not(None)
    valid_markdown_snapshot = db.query(ReviewReport.id).filter(
        ReviewReport.task_id == ReviewTask.id,
        ReviewReport.user_id == ReviewTask.user_id,
        ReviewReport.content_json["source"].as_string() == ReviewTask.review_type,
        report_body_is_string,
        report_body.is_not(None),
        non_whitespace_report_body != "",
    ).correlate(ReviewTask).exists()
    q = q.filter(
        or_(
            ReviewTask.review_type.notin_(("security_scan", "fullchain_audit", "sandbox_test")),
            valid_markdown_snapshot,
        )
    )

    pentest_engagement_count = db.query(func.count(PentestEngagement.id)).filter(
        PentestEngagement.report_task_id == ReviewTask.id,
        PentestEngagement.user_id == ReviewTask.user_id,
        PentestEngagement.project_id == ReviewTask.project_id,
    ).correlate(ReviewTask).scalar_subquery()
    valid_pentest_snapshot = db.query(PentestEngagement.id).join(
        ReviewReport,
        and_(
            ReviewReport.task_id == ReviewTask.id,
            ReviewReport.user_id == ReviewTask.user_id,
            ReviewReport.content_json["source"].as_string() == "pentest",
            ReviewReport.content_json["engagement_public_id"].as_string() == PentestEngagement.public_id,
        ),
    ).filter(
        PentestEngagement.report_task_id == ReviewTask.id,
        PentestEngagement.user_id == ReviewTask.user_id,
        PentestEngagement.project_id == ReviewTask.project_id,
        pentest_engagement_count == 1,
    ).correlate(ReviewTask).exists()
    q = q.filter(or_(ReviewTask.review_type != "pentest", valid_pentest_snapshot))

    total = q.count()
    pagination = Pagination(page, page_size, total)
    rows = q.order_by(ReviewTask.create_time.desc()).offset(pagination.offset).limit(pagination.page_size).all()
    issue_stats = load_task_issue_stats(db, rows)
    security_snapshots = {
        row.id: snapshot
        for row in rows
        if (snapshot := _security_report_snapshot(db, row)) is not None
    }
    project_ids = {row.project_id for row in rows if row.project_id is not None}
    projects_by_id = (
        {
            project.id: project
            for project in db.query(Project).filter(Project.id.in_(project_ids)).populate_existing().all()
        }
        if project_ids
        else {}
    )

    capabilities = project_resource_capabilities(db, user, project_ids, projects=projects_by_id.values())
    can_delete = rbac_service.check_permission(db, int(user.id), PermissionCode.REVIEW_CANCEL)
    source_permissions = {
        review_type: has_source_report_permission(db, user, review_type)
        for review_type in _REPORT_BACKED_REVIEW_TYPES
    }
    report_access = {
        row.id: (
            report_permission
            and (issue_permission or row.review_type in _REPORT_BACKED_REVIEW_TYPES)
            and (administrator or int(row.user_id) == int(user.id))
            and row.project_id in visible_project_ids
            and source_permissions.get(row.review_type, True)
        )
        for row in rows
    }
    # 查询已在 count 和分页前按 REPORT_VIEW、项目可见范围、发起人/管理员
    # 范围及 SECURITY_VIEW/PENTEST_VIEW 过滤。这里再按同一授权范围 fail-closed
    # 剔除行，确保无权报告不会成为列表卡片。
    rows = [row for row in rows if report_access[row.id]]
    items = []
    for row in rows:
        project = projects_by_id.get(row.project_id)
        issue_count, score, _breakdown, _severity_count = _build_task_score_facts(
            row,
            issue_stats[row.id],
        )
        if not _score_is_available(row):
            score = None
        can_view_report = report_access[row.id]
        source = dict(issue_stats[row.id]["source"]) if can_view_report else {}
        snapshot = security_snapshots.get(row.id) if can_view_report else None
        if snapshot is not None:
            source["security_report"] = snapshot
        coverage = None
        if can_view_report:
            if row.review_type == "sandbox_test":
                coverage = _task_coverage(row, source.get("test_mode"))
            else:
                coverage = _task_coverage(row)
        items.append({
            "id": row.id, "task_id": row.id, "task_name": row.task_name,
            "project_name": project.project_name if project else "",
            "can_delete": can_delete and capabilities.get(row.project_id, {}).get("can_execute", False),
            "can_view_report": can_view_report,
            "total_issues": issue_count, "score": score, "status": row.status,
            "score_scope": score_scope_payload(row) if can_view_report else None,
            "error_message": clean_report_text(row.error_message) if can_view_report else None,
            "coverage": clean_report_payload(coverage) if coverage is not None else None,
            "source": source,
            "create_time": row.create_time.isoformat() if row.create_time else None,
        })
    return pagination.to_dict(items)


def get_report_detail(db: Session, user: User, task_id: int) -> dict:
    """获取报告详情

    Args:
        db: 数据库会话
        user: 当前用户
        task_id: 任务ID

    Returns:
        dict: 报告完整数据
    """
    task = get_readable_report_task(db, user, task_id)

    project = db.get(Project, task.project_id)

    issue_stats = load_task_issue_stats(db, [task])[task_id]
    severity_from_issues = issue_stats["severity"]
    issue_count, score, score_breakdown, severity_count = _build_task_score_facts(
        task,
        issue_stats,
    )
    score_available = score is not None and _score_is_available(task)
    if not score_available:
        score = None
    score_scope = score_scope_payload(task)
    security_snapshot = _security_report_snapshot(db, task)
    if task.review_type in {"security_scan", "fullchain_audit"} and security_snapshot is None:
        raise ConflictError(
            "安全审计报告快照缺失或来源不匹配，已停止展示", code=40942,
            next_action=f"请重新生成 GET /api/reports/tasks/{task.id}/generate 的报告后再查看",
        )
    if not score_available:
        score = None
        score_breakdown = {
            "score": None, "risk_level": "未评估", "score_source": "unavailable",
            "score_unavailable_reason": score_scope.get("reason"),
        }
    else:
        score_breakdown = {**score_breakdown, "score_scope": score_scope.get("scope")}
    from app.services.score_credibility import audit_report_score

    # 小菱终审：按规则独立复算，不一致则打回重判并采用规则分。
    score_audit = audit_report_score(task, score, severity_count, issue_count, score_scope=score_scope)
    if score_audit["status"] == "rescored" and score_audit["final_score"] != score:
        score = score_audit["final_score"]
        from app.ai.scoring import score_risk_level

        score_breakdown = {
            **score_breakdown, "score": score, "risk_level": score_risk_level(score),
            "score_source": "xiaoling_rescored",
        }
    elif score_audit["status"] == "unavailable" and score is not None:
        score = None
        score_available = False
        score_breakdown = {"score": None, "risk_level": "未评估", "score_source": "unavailable"}

    from app.services.review_service import _task_agent_release_summaries

    capabilities = project_resource_capabilities(db, user, [task.project_id]).get(task.project_id, {})
    source = dict(issue_stats["source"])
    duration_ms = _report_duration_ms(task)
    report_summary = task.summary
    report_coverage = _task_coverage(task)
    if task.review_type == "sandbox_test" and duration_ms is None:
        source["duration_note"] = _UNRELIABLE_SANDBOX_DURATION_NOTE
    if security_snapshot is not None:
        source["security_report"] = security_snapshot
    elif task.review_type == "sandbox_test":
        snapshot = _sandbox_report_snapshot(db, task)
        content = snapshot.content_json if snapshot and isinstance(snapshot.content_json, dict) else {}
        if snapshot is None:
            raise ConflictError(
                "沙箱领域报告正文缺失或来源不匹配，已停止展示", code=40942,
                next_action=f"请重新生成 GET /api/reports/tasks/{task.id}/generate 的报告后再查看",
            )
        from app.services.domain_report_exporter import build_sandbox_report_markdown

        domain_data = _sandbox_domain_data_for_display(task, content)
        domain_data.setdefault("execution_status", task.status)
        domain_data.setdefault("error_message", task.error_message)
        report_summary = domain_data.get("report_md") or task.summary
        report_coverage = domain_data["coverage"]
        source["domain_report_md"] = build_sandbox_report_markdown(
            domain_data,
            task_name=task.task_name,
            project_name=project.project_name if project else "",
        )
        findings = domain_data.get("findings")
        source["domain_findings"] = clean_report_payload(
            findings if isinstance(findings, list) else []
        )
    elif task.review_type == "pentest":
        engagement = _pentest_report_engagement(db, task)
        if engagement is None:
            raise ConflictError(
                "渗透领域发现与当前报告的关联缺失或不唯一，已停止展示", code=40942,
                next_action=f"请在 GET /api/reports/{task.id} 查看来源并恢复原始关联",
            )
        from app.services.domain_report_exporter import build_pentest_report_markdown
        from app.services.pentest_service import get_engagement_detail

        domain_data = get_engagement_detail(db, user, engagement.public_id)
        source["domain_report_md"] = build_pentest_report_markdown(
            domain_data,
            task_name=task.task_name,
            project_name=project.project_name if project else "",
            execution_status=task.status,
            error_message=task.error_message,
            coverage=clean_report_payload(report_coverage),
        )
    return {
        "can_delete": (rbac_service.check_permission(db, int(user.id), PermissionCode.REVIEW_CANCEL)
                       and capabilities.get("can_execute", False)),
        "can_execute": capabilities.get("can_execute", False),
        "project": {"id": project.id, "project_name": project.project_name,
                     "language": project.language} if project else {},
        "task": {"id": task.id, "name": task.task_name, "task_name": task.task_name,
                 "review_type": task.review_type,
                 "total_files": task.total_files,
                 "processed_files": task.processed_files,
                 "duration_ms": duration_ms,
                 "total_issues": issue_count,
                 "score": score,
                 "score_version": score_breakdown.get("version"),
                 "score_breakdown": score_breakdown,
                 "score_audit": score_audit,
                 "score_scope": score_scope,
                 "status": task.status,
                 "error_message": clean_report_text(task.error_message),
                 "coverage": clean_report_payload(report_coverage),
                 "create_time": task.create_time.isoformat() if task.create_time else None,
                 "agent_releases": _task_agent_release_summaries(db, task_id)},
        "stats": {
            "total_files": task.total_files, "total_issues": issue_count,
            "score": score,
            "severe": severity_count["严重"],
            "high": severity_count["高"],
            "medium": severity_count["中"],
            "low": severity_count["低"],
            "fixed": issue_stats["fixed"],
            "confirmed": issue_stats["confirmed"],
            "refuted": issue_stats["refuted"],
            "severity": severity_from_issues,
            "severity_breakdown": severity_from_issues,
            "by_type": issue_stats["by_type"],
            "score_version": score_breakdown.get("version"),
            "score_breakdown": score_breakdown,
            "risk_level": score_breakdown.get("risk_level"),
        },
        "source": source,
        "summary": clean_report_markdown(
            display_report_summary(
                report_summary, score_available=score_available,
                score=None if task.review_type in {"sandbox_test", "pentest"} else score,
            )
        ),
        "files": [] if task.review_type in {"sandbox_test", "pentest"} else _build_file_summaries(
            db,
            task_id,
            score_available=score_available,
        ),
        "rules_snapshot": task.rules_snapshot or [],
    }


def _count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def get_domain_report_export(db: Session, user: User, task: ReviewTask, format: str) -> dict | None:
    """来源型报告复用原始业务正文，不能落入空问题表的标准报告模板。"""
    if task.review_type not in {"sandbox_test", "pentest", "security_scan", "fullchain_audit"}:
        return None
    detail = get_report_detail(db, user, task.id)
    from app.services.domain_report_exporter import (
        build_pentest_report_markdown,
        build_sandbox_report_markdown,
    )
    native_exports = []
    if task.review_type in {"security_scan", "fullchain_audit"}:
        snapshot = db.query(ReviewReport).filter(
            ReviewReport.task_id == task.id,
            ReviewReport.user_id == task.user_id,
        ).first()
        content = snapshot.content_json if snapshot and isinstance(snapshot.content_json, dict) else {}
        if (
            content.get("source") != task.review_type
            or not isinstance(content.get("report_md"), str)
            or not content["report_md"].strip()
        ):
            raise ConflictError(
                "安全审计报告正文缺失，无法等价导出", code=40942,
                next_action=f"请在 GET /api/reports/{task.id} 查看来源并恢复原始报告快照",
            )
        domain_data = content
        domain_markdown = clean_report_markdown(content["report_md"])
    elif task.review_type == "sandbox_test":
        snapshot = db.query(ReviewReport).filter(
            ReviewReport.task_id == task.id, ReviewReport.user_id == task.user_id,
        ).first()
        content = snapshot.content_json if snapshot and isinstance(snapshot.content_json, dict) else {}
        if (
            content.get("source") != "sandbox_test"
            or not isinstance(content.get("report_md"), str)
            or not content["report_md"].strip()
        ):
            raise ConflictError(
                "沙箱领域原始报告缺失，无法等价导出", code=40942,
                next_action=f"请在 GET /api/reports/{task.id} 查看来源，恢复原始制品或重新生成报告",
            )
        domain_data = _sandbox_domain_data_for_display(task, content)
        domain_data.setdefault("execution_status", task.status)
        domain_data.setdefault("error_message", task.error_message)
        if domain_data.get("report_md") == content["report_md"]:
            native_exports = _sandbox_native_exports(db, user, task, content)
        domain_markdown = build_sandbox_report_markdown(
            domain_data,
            task_name=task.task_name,
            project_name=detail["project"].get("project_name", ""),
        )
    else:
        from app.services.pentest_service import get_engagement_detail
        from app.services.rbac_service import check_permission

        if not check_permission(db, user.id, PermissionCode.PENTEST_VIEW):
            raise PermissionError(
                f"无操作权限: 需要 {PermissionCode.PENTEST_VIEW}",
                detail={"required_permission": PermissionCode.PENTEST_VIEW},
            )

        engagements = db.query(PentestEngagement).filter(
            PentestEngagement.report_task_id == task.id,
            PentestEngagement.user_id == task.user_id,
            PentestEngagement.project_id == task.project_id,
        ).all()
        if len(engagements) != 1 or detail["source"].get("stats_basis") != "pentest_findings":
            raise ConflictError(
                "渗透领域发现与当前报告的关联缺失或不唯一，无法等价导出", code=40942,
                next_action=f"请在 GET /api/reports/{task.id} 查看来源，恢复原始关联或重新生成报告",
            )
        domain_data = get_engagement_detail(db, user, engagements[0].public_id)
        domain_data["execution_status"] = task.status
        domain_data["error_message"] = task.error_message
        domain_data["coverage"] = _task_coverage(task)
        if len(domain_data["findings"]) != detail["stats"]["total_issues"]:
            raise ConflictError(
                "领域发现正在变化，导出计数不一致", code=40943, retryable=True,
                next_action=f"请等待领域任务结束后重试 GET /api/reports/tasks/{task.id}/export",
            )
        domain_markdown = build_pentest_report_markdown(
            domain_data,
            task_name=task.task_name,
            project_name=detail["project"].get("project_name", ""),
            execution_status=task.status,
            error_message=task.error_message,
            coverage=_task_coverage(task),
        )

    if task.review_type in {"sandbox_test", "pentest"}:
        from app.services.domain_report_exporter import insert_section_after_task_info, score_section_lines

        scope_info = detail["task"].get("score_scope") or {}
        stats = detail.get("stats") or {}
        domain_markdown = insert_section_after_task_info(domain_markdown, score_section_lines(
            detail["task"].get("score"),
            available=bool(scope_info.get("available")) and detail["task"].get("score") is not None,
            scope_label=str(scope_info.get("label") or ""),
            reason=str(scope_info.get("reason") or ""),
            finding_count=int(stats.get("total_issues") or 0) if task.review_type == "pentest" else None,
            severity_counts=stats.get("severity") if isinstance(stats.get("severity"), dict) else None,
        ))

    return {
        "source": clean_report_payload(detail["source"]),
        "task_info": detail["task"], "project": detail["project"],
        "statistics": detail["stats"], "score": detail["task"]["score"],
        "summary": clean_report_markdown(detail["summary"] or ""),
        "domain_data": clean_report_payload(domain_data),
        "domain_markdown": clean_report_markdown(domain_markdown),
        "native_exports": native_exports,
    }


def _sandbox_native_exports(db: Session, user: User, task: ReviewTask, content: dict) -> list[dict]:
    """只提供与报告快照一致、归属正确且通过原生完整性校验的 Markdown 制品。"""
    from app.models.agent_capability import SandboxArtifact, SandboxEnvironment
    from app.services.sandbox_service import get_artifact_download

    public_id = content.get("public_id")
    if not isinstance(public_id, str) or not public_id:
        return []
    environment = db.query(SandboxEnvironment).filter(
        SandboxEnvironment.public_id == public_id,
        SandboxEnvironment.owner_id == task.user_id,
        SandboxEnvironment.project_id == task.project_id,
    ).first()
    if environment is None:
        return []
    expected = content["report_md"].encode("utf-8")
    artifacts = db.query(SandboxArtifact).filter(
        SandboxArtifact.environment_id == environment.id,
        SandboxArtifact.artifact_type == "review_report",
        SandboxArtifact.sha256 == hashlib.sha256(expected).hexdigest(),
        SandboxArtifact.byte_size == len(expected),
    ).order_by(SandboxArtifact.id.desc()).all()
    for artifact in artifacts:
        try:
            body, filename, mime_type = get_artifact_download(db, user, public_id, artifact.id)
        except (NotFoundError, RuntimeError):
            continue
        if body == expected:
            return [{
                "format": "markdown", "file_name": filename, "mime_type": mime_type,
                "byte_size": len(body), "sha256": artifact.sha256,
                "download_api": f"/api/sandboxes/{public_id}/artifacts/{artifact.id}",
            }]
    return []


def _empty_issue_stats(task: ReviewTask, basis: str) -> dict:
    return {
        "total_issues": 0, "severity": {}, "by_type": {},
        "fixed": 0, "confirmed": 0, "refuted": 0,
        "source": {"type": task.review_type, "stats_basis": basis},
    }


def _add_issue_stats(stats: dict, severity: str, kind: str, status: str, count: int) -> None:
    stats["total_issues"] += count
    if status == "refuted":
        stats["refuted"] += count
        return
    severity = severity or "未分级"
    stats["severity"][severity] = stats["severity"].get(severity, 0) + count
    stats["by_type"][kind] = stats["by_type"].get(kind, 0) + count
    if status == "fixed":
        stats["fixed"] += count
    if status == "confirmed":
        stats["confirmed"] += count


def load_task_issue_stats(db: Session, tasks: list[ReviewTask], *, since: datetime | None = None) -> dict[int, dict]:
    """批量读取已授权任务的来源事实，不创建虚拟问题或重新运行领域服务。

    标准/圆桌使用 ReviewIssue；沙箱解析报告的问题清单条目，未提供严重度
    时明确标为未分级。渗透优先读取归属一致的领域发现，全部发现计入总数，
    refuted 仅单列而不进入风险分布；旧任务缺少领域关联时保留发布快照。
    since 对明细按发现时间过滤；汇总使用任务完成时间，旧记录回退报告创建时间。
    """
    if not tasks:
        return {}
    task_map = {task.id: task for task in tasks}
    domain_tasks = {task.id: task for task in tasks if task.review_type in {"sandbox_test", "pentest"}}
    stats = {task.id: _empty_issue_stats(task, "review_issues") for task in tasks}
    review_ids = [task.id for task in tasks if task.id not in domain_tasks]
    query = db.query(
        ReviewIssue.task_id,
        ReviewIssue.severity,
        ReviewIssue.issue_type,
        ReviewIssue.status,
        func.count(ReviewIssue.id),
    ).filter(ReviewIssue.task_id.in_(review_ids))
    if since is not None:
        query = query.filter(ReviewIssue.create_time >= since)
    rows = query.group_by(
        ReviewIssue.task_id,
        ReviewIssue.severity,
        ReviewIssue.issue_type,
        ReviewIssue.status,
    ).all() if review_ids else []
    for task_id, severity, kind, status, count in rows:
        _add_issue_stats(stats[task_id], severity, kind, status, int(count))

    reports = {
        row.task_id: row for row in db.query(ReviewReport).filter(ReviewReport.task_id.in_(domain_tasks)).all()
        if row.user_id == task_map[row.task_id].user_id
        and isinstance(row.content_json, dict)
        and row.content_json.get("source") == task_map[row.task_id].review_type
    } if domain_tasks else {}
    for task_id, task in domain_tasks.items():
        item = stats[task_id] = _empty_issue_stats(task, "task_snapshot")
        report = reports.get(task_id)
        content = report.content_json if report else {}
        public_id = content.get("public_id" if task.review_type == "sandbox_test" else "engagement_public_id")
        if isinstance(public_id, str) and public_id and public_id.replace("-", "").replace("_", "").isalnum():
            path = "sandboxes" if task.review_type == "sandbox_test" else "pentest/engagements"
            item["source"].update(public_id=public_id, detail_api=f"/api/{path}/{public_id}")
        created = task.end_time or (report.create_time if report else task.create_time)
        if since is not None and (
            created is None or created.replace(tzinfo=timezone.utc) < since.replace(tzinfo=timezone.utc)
        ):
            continue
        total = _count(task.total_issues)
        if task.review_type == "sandbox_test":
            from app.services.sandbox_report_summary import scope_sandbox_report_markdown, summarize_sandbox_report

            # Preserve the requested mode in the list response so old task cards do not
            # claim that the mode is missing when the report snapshot records it.
            requested_mode = _sandbox_requested_mode(task, content)
            if requested_mode:
                item["source"]["test_mode"] = requested_mode

            report_md = scope_sandbox_report_markdown(content.get("report_md"), requested_mode)
            summary = summarize_sandbox_report(report_md)
            item["source"]["report_issue_summary"] = summary
            ai_review = content.get("ai_review")
            if isinstance(ai_review, dict) and ai_review.get("status") in {"complete", "incomplete", "skipped"}:
                ai_review_summary = {
                    "status": ai_review["status"],
                    "failure_reason": str(ai_review.get("failure_reason") or "") or None,
                }
                if ai_review["status"] == "skipped":
                    ai_review_summary["skip_reason"] = str(ai_review.get("skip_reason") or "") or None
                item["source"]["ai_review"] = ai_review_summary
            if summary["total"] is None:
                # 不回填或把旧快照归零；该分支只能证明历史记录数，不能证明发现条目数。
                item["source"]["stats_basis"] = "legacy_task_snapshot"
                item["source"]["legacy_recorded_total"] = total
                if total:
                    _add_issue_stats(item, "未分级", "沙箱测试", "", total)
            else:
                item["source"]["stats_basis"] = summary["basis"]
                for level, count in summary["severity_counts"].items():
                    if count:
                        _add_issue_stats(item, level, "沙箱测试", "", count)
                if summary["unclassified"]:
                    _add_issue_stats(item, "未分级", "沙箱测试", "", summary["unclassified"])
            continue
        severity = content.get("severity_counts")
        has_severity_snapshot = isinstance(severity, dict)
        if not has_severity_snapshot:
            severity = {
                "严重": task.severe_issues, "高": task.high_issues,
                "中": task.medium_issues, "低": task.low_issues,
            }
        for level, count in severity.items():
            if _count(count):
                _add_issue_stats(item, level, "渗透测试", "", _count(count))
        remaining = max(0, total - item["total_issues"])
        if remaining:
            _add_issue_stats(item, "未分级", "渗透测试", "refuted" if has_severity_snapshot else "", remaining)
        item["total_issues"] = total
        confirmed = content.get("confirmed_severity_counts")
        if isinstance(confirmed, dict):
            item["confirmed"] = sum(_count(count) for count in confirmed.values())

    pentest_ids = [task.id for task in tasks if task.review_type == "pentest"]
    engagements = db.query(
        PentestEngagement.id, PentestEngagement.report_task_id, PentestEngagement.public_id,
        PentestEngagement.user_id, PentestEngagement.project_id,
    ).filter(PentestEngagement.report_task_id.in_(pentest_ids)).all() if pentest_ids else []
    engagement_tasks = {}
    for engagement in engagements:
        task = task_map[engagement.report_task_id]
        if engagement.user_id != task.user_id or engagement.project_id != task.project_id:
            continue
        engagement_tasks[engagement.id] = task.id
        item = stats[task.id] = _empty_issue_stats(task, "pentest_findings")
        item["source"].update(
            public_id=engagement.public_id,
            detail_api=f"/api/pentest/engagements/{engagement.public_id}",
        )
    if engagement_tasks:
        query = db.query(
            PentestFinding.engagement_id, PentestFinding.severity, PentestFinding.category,
            PentestFinding.status, func.count(PentestFinding.id),
        ).filter(PentestFinding.engagement_id.in_(engagement_tasks))
        if since is not None:
            query = query.filter(PentestFinding.create_time >= since)
        for engagement_id, severity, kind, status, count in query.group_by(
            PentestFinding.engagement_id, PentestFinding.severity, PentestFinding.category, PentestFinding.status,
        ).all():
            _add_issue_stats(stats[engagement_tasks[engagement_id]], severity, kind or "渗透测试", status, int(count))
    return stats


def _build_task_score_facts(
    task: ReviewTask,
    issue_stats: dict,
) -> tuple[int, int | None, dict, dict[str, int]]:
    """标准审查按最终问题评分；领域报告保留原评分，禁止伪造扣分权重。"""
    severity_from_issues = issue_stats["severity"]
    severity_count = {
        severity: int(severity_from_issues.get(severity, 0) or 0)
        for severity in ("严重", "高", "中", "低")
    }
    issue_count = issue_stats["total_issues"]
    if task.review_type in {"sandbox_test", "pentest"}:
        risk_level = None
        if task.review_type == "pentest":
            severity_count["低"] += _count(severity_from_issues.get("提示"))
            if not severity_from_issues.get("未分级"):
                severe = severity_count["严重"] + severity_count["高"]
                risk_level = "极高风险" if severe >= 3 else (
                    "高风险" if severe else ("中风险" if task.score < 90 else "低风险")
                )
        return issue_count, task.score, {
            "version": task.score_version,
            "score": task.score,
            "score_source": task.review_type,
            **(
                {"verification_status": sandbox_verification_status(task)}
                if task.review_type == "sandbox_test"
                else {}
            ),
            "risk_level": risk_level,
        }, severity_count
    explicit_score = None
    if issue_count == 0 and _stored_score_is_trusted_input(task):
        explicit_score = task.score
    score, score_breakdown = build_report_score_facts(
        severity_count,
        max(0, issue_count - int(issue_stats.get("refuted", 0) or 0)),
        explicit_score,
    )
    return issue_count, score, score_breakdown, severity_count


def _build_file_summaries(
    db: Session,
    task_id: int,
    *,
    score_available: bool = True,
) -> list[dict]:
    """汇总报告涉及的文件及其问题统计。

    Args:
        db: 数据库会话。
        task_id: 审查任务 ID。

    Returns:
        list[dict]: 文件摘要列表。旧任务没有关联记录时按历史问题回退。
    """
    issue_rows = db.query(
        ReviewIssue.file_id,
        ReviewIssue.file_name,
        ReviewIssue.severity,
        func.count(ReviewIssue.id),
    ).filter(
        ReviewIssue.task_id == task_id,
    ).group_by(
        ReviewIssue.file_id,
        ReviewIssue.file_name,
        ReviewIssue.severity,
    ).all()

    counts: dict[tuple[str, object], dict] = {}
    for file_id, file_name, severity, count in issue_rows:
        key = ("id", file_id) if file_id is not None else ("name", file_name or "未命名文件")
        item = counts.setdefault(key, {
            "file_id": file_id,
            "file_name": file_name or "未命名文件",
            "severity": {"严重": 0, "高": 0, "中": 0, "低": 0},
        })
        item["severity"][severity] = item["severity"].get(severity, 0) + count

    linked_files = db.query(ReviewTaskFile, CodeFile).outerjoin(
        CodeFile,
        CodeFile.id == ReviewTaskFile.file_id,
    ).filter(
        ReviewTaskFile.task_id == task_id,
    ).order_by(
        ReviewTaskFile.id.asc(),
    ).all()

    summaries = []
    emitted_keys: set[tuple[str, object]] = set()
    for link, code_file in linked_files:
        key = ("id", link.file_id)
        emitted_keys.add(key)
        summaries.append(_file_summary(
            code_file, counts.get(key), file_id=link.file_id,
            file_snapshot=link.file_snapshot,
            version_no=link.version_no, content_sha256=link.content_sha256,
            score_available=score_available,
        ))

    fallback_ids = [key[1] for key in counts if key not in emitted_keys and key[0] == "id"]
    fallback_files = {
        row.id: row for row in db.query(CodeFile).filter(CodeFile.id.in_(fallback_ids)).all()
    } if fallback_ids else {}
    for key, item in counts.items():
        if key in emitted_keys:
            continue
        summaries.append(_file_summary(
            fallback_files.get(key[1]),
            item,
            score_available=score_available,
        ))
    return summaries


def _file_summary(
    code_file: CodeFile | None, counts: dict | None, *,
    file_id: int | None = None, file_snapshot: dict | None = None,
    version_no: int | None = None, content_sha256: str | None = None,
    score_available: bool = True,
) -> dict:
    """构造单个报告文件摘要。

    Args:
        code_file: 代码文件记录，旧数据缺失时可为空。
        counts: 按严重度聚合的问题统计。

    Returns:
        dict: 可直接返回给报告详情页的文件摘要。
    """
    item = counts or {
        "file_id": code_file.id if code_file else None,
        "file_name": code_file.file_name if code_file else "未命名文件",
        "severity": {"严重": 0, "高": 0, "中": 0, "低": 0},
    }
    severity = item["severity"]
    # 新任务使用冻结元数据；只有无快照的旧任务才回退到当前文件。
    snapshot = file_snapshot if isinstance(file_snapshot, dict) else {}
    return {
        "file_id": file_id if file_id is not None else (code_file.id if code_file else item["file_id"]),
        "file_name": snapshot.get("file_name") or (code_file.file_name if code_file else item["file_name"]),
        "file_path": snapshot.get("file_path") or (code_file.file_path if code_file else None),
        "version_no": version_no,
        "content_sha256": content_sha256,
        "language": snapshot.get("language") or (code_file.language if code_file else ""),
        "issue_count": sum(severity.values()),
        "severe_count": severity.get("严重", 0),
        "score": compute_score(severity) if score_available else None,
    }


def delete_report(db: Session, user: User, task_id: int) -> None:
    """删除报告(软删除对应的审查任务)

    Args:
        db: 数据库会话
        user: 当前用户
        task_id: 任务ID

    Raises:
        NotFoundError: 报告/任务不存在
        ForbiddenError: 无操作权限
    """
    task = db.get(ReviewTask, task_id)
    if not task:
        raise NotFoundError("报告不存在", code=40400)
    if task.user_id != user.id and not rbac_service.is_admin_user(db, int(user.id)):
        raise ForbiddenError("无权限删除此报告", code=40300)
    # Preserve existing author/admin deletion for reviewer authors, while a
    # later downgrade to project viewer cannot delete the historical report.
    require_project_execution(db, task.project_id, user)
    task.status = "deleted"
    # 渗透报告删除联动: 清空委托的 report_task_id, 防止详情页"查看报告"跳 404
    if task.review_type == "pentest":
        try:
            from app.models.pentest import PentestEngagement

            engagement = (
                db.query(PentestEngagement)
                .filter(PentestEngagement.report_task_id == int(task_id))
                .first()
            )
            if engagement is not None:
                engagement.report_task_id = None
        except Exception:  # noqa: BLE001 - 联动失败不阻断报告删除
            pass
    db.commit()
    try:
        from app.services.dashboard_service import invalidate_dashboard_stats

        invalidate_dashboard_stats()
    except Exception:  # noqa: BLE001 - 缓存失效失败不影响删除主流程
        pass
