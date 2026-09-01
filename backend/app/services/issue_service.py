"""
审查问题服务模块

v2.4(2026-06-25): 数据隔离改为基于 project_member 关系
    - list_issues: admin 全量 / 非 admin: 可见项目(owner ∪ member)下的问题
    - get_issue/update_status: 通过 require_project_access 校验
    - reviewer 可见同项目的问题,非成员返回 404(防枚举)
"""
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.core.pagination import Pagination
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services.project_member_service import (
    get_visible_project_ids,
    require_project_access,
)


def get_issue(db: Session, user: User, issue_id: int) -> ReviewIssue:
    """获取问题详情(基于 project_member 关系校验)

    可见性:reviewer 可见同项目的问题,非成员返回 404(防枚举)。

    Args:
        db: 数据库会话
        user: 当前用户
        issue_id: 问题ID

    Returns:
        ReviewIssue: 问题ORM对象

    Raises:
        NotFoundError: 问题不存在或无访问权限
    """
    issue = db.get(ReviewIssue, issue_id)
    if not issue:
        raise NotFoundError("问题不存在", code=40400)
    task = db.get(ReviewTask, issue.task_id)
    if not task or task.status == "deleted":
        raise NotFoundError("问题不存在", code=40400)
    # v2.4: 用 project_member 关系校验,reviewer 可读同项目的问题
    require_project_access(db, task.project_id, user, need_write=False)
    return issue


def update_status(db: Session, user: User, issue_id: int, status: str) -> None:
    """更新单个问题的处理状态(需 owner/admin 权限)

    Args:
        db: 数据库会话
        user: 操作用户
        issue_id: 问题ID
        status: 新状态 (unfixed/fixed/ignored/pending_review)

    Raises:
        NotFoundError: 问题不存在或无访问权限
        ForbiddenError: 仅 owner/admin 可修改
    """
    issue = db.get(ReviewIssue, issue_id)
    if not issue:
        raise NotFoundError("问题不存在", code=40400)
    task = db.get(ReviewTask, issue.task_id)
    if not task or task.status == "deleted":
        raise NotFoundError("问题不存在", code=40400)
    # v2.4: 修改问题状态视为写操作,仅 owner/admin 可执行
    require_project_access(db, task.project_id, user, need_write=True)
    issue.status = status
    issue.handled_by = user.id
    issue.handled_at = datetime.now(timezone.utc)
    db.commit()


def list_issues(
    db: Session,
    user: User,
    project_id: Optional[int] = None,
    task_id: Optional[int] = None,
    severity: str = "",
    issue_type: str = "",
    status: str = "",
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """跨任务/项目分页查询问题(基于 project_member 关系)

    可见范围:
        - admin: 全部问题
        - 非 admin: 可见项目(owner ∪ member)下的问题

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 限定项目
        task_id: 限定审查任务
        severity: 严重程度过滤
        issue_type: 问题类型过滤
        status: 状态过滤
        keyword: 标题或描述模糊匹配
        page: 页码
        page_size: 每页条数

    Returns:
        dict: 分页响应,items 中每条携带 project_id / project_name / task_name 冗余字段
    """
    visible_ids, _ = get_visible_project_ids(db, user)
    q = (
        db.query(ReviewIssue, ReviewTask, Project)
        .join(ReviewTask, ReviewTask.id == ReviewIssue.task_id)
        .join(Project, Project.id == ReviewTask.project_id)
        .filter(
            ReviewTask.status != "deleted",
            Project.status != "deleted",
            ReviewTask.project_id.in_(visible_ids),
        )
    )

    if project_id is not None:
        q = q.filter(ReviewTask.project_id == project_id)
    if task_id is not None:
        q = q.filter(ReviewIssue.task_id == task_id)
    if severity:
        q = q.filter(ReviewIssue.severity == severity)
    if issue_type:
        q = q.filter(ReviewIssue.issue_type == issue_type)
    if status:
        if status != "all":
            q = q.filter(ReviewIssue.status == status)
    else:
        q = q.filter(ReviewIssue.status.in_(["unfixed", "pending_review"]))
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(and_(ReviewIssue.title.like(like) | ReviewIssue.description.like(like)))

    total = q.count()
    pagination = Pagination(page, page_size, total)
    rows = (
        q.order_by(ReviewIssue.create_time.desc())
        .offset(pagination.offset)
        .limit(pagination.page_size)
        .all()
    )

    items = []
    for issue, task, project in rows:
        # R1 修复(2026-06-25):补齐 v2/v3 漏洞元数据字段,
        # 与 IssueOut schema 声明的字段对齐,避免前端拿不到 OWASP/CWE/CVSS 等漏洞元数据。
        # 同类问题参考 AC2:_to_traceable_dict 遗漏 agent_label 导致 API 返回 null。
        items.append(
            {
                "id": issue.id,
                "task_id": issue.task_id,
                "task_name": task.task_name,
                "project_id": project.id,
                "project_name": project.project_name,
                "file_id": issue.file_id,
                "file_name": issue.file_name,
                "line_number": issue.line_number,
                "end_line": issue.end_line,
                "issue_type": issue.issue_type,
                "severity": issue.severity,
                "title": issue.title,
                "description": issue.description,
                "suggestion": issue.suggestion,
                "fixed_code": issue.fixed_code,
                "status": issue.status,
                "create_time": issue.create_time,
                # === v2 漏洞元数据 ===
                "owasp": issue.owasp,
                "cwe": issue.cwe,
                "evidence": issue.evidence,
                "exploit_scenario": issue.exploit_scenario,
                "references_json": issue.references_json,
                "confidence": issue.confidence,
                "source": issue.source,
                "source_details": issue.source_details,
                "confirmation_count": issue.confirmation_count,
                "finding_fingerprint": issue.finding_fingerprint,
                # === v3 全量漏洞元数据 ===
                "cvss_score": issue.cvss_score,
                "cvss_vector": issue.cvss_vector,
                "cvss_version": issue.cvss_version,
                "cvss_source": issue.cvss_source,
                "compliance_mapping": issue.compliance_mapping,
                "remediation": issue.remediation,
                "static_rule_hits": issue.static_rule_hits,
                "aggregation_version": issue.aggregation_version,
                "evidence_quality": issue.evidence_quality,
                "conflict_status": issue.conflict_status,
                "human_review_status": issue.human_review_status,
                "risk_score": issue.risk_score,
                "aggregation_json": issue.aggregation_json,
                # R2 修复:补齐处理人/处理时间/更新时间,与 IssueOut schema 对齐
                "handled_by": issue.handled_by,
                "handled_at": issue.handled_at,
                "update_time": issue.update_time,
            }
        )
    return pagination.to_dict(items)


def batch_update_status(db: Session, user: User, ids: list[int], status: str) -> None:
    """批量更新问题状态(逐条复用 update_status 校验权限)

    Args:
        db: 数据库会话
        user: 操作用户
        ids: 问题ID列表
        status: 新状态
    """
    for issue_id in ids:
        update_status(db, user, issue_id, status)


def review_decision(
    db: Session,
    user: User,
    issue_id: int,
    decision: str,
    note: str = "",
) -> ReviewIssue:
    """记录人工对聚合争议的裁决，保留机器原始声明。"""
    issue = db.get(ReviewIssue, issue_id)
    if not issue:
        raise NotFoundError("问题不存在", code=40400)
    task = db.get(ReviewTask, issue.task_id)
    if not task or task.status == "deleted":
        raise NotFoundError("问题不存在", code=40400)
    require_project_access(db, task.project_id, user, need_write=True)

    now = datetime.now(timezone.utc)
    aggregation: dict[str, Any] = dict(issue.aggregation_json or {})
    history = aggregation.get("human_review_history")
    if not isinstance(history, list):
        history = []
    history.append({
        "decision": decision,
        "note": (note or "")[:1000],
        "reviewer_id": user.id,
        "reviewer_name": user.username,
        "reviewed_at": now.isoformat(),
    })
    aggregation["human_review_history"] = history[-50:]
    aggregation["human_review"] = history[-1]
    issue.aggregation_json = aggregation
    issue.human_review_status = decision
    issue.conflict_status = "resolved" if decision in {"accepted", "rejected"} else "unresolved"
    issue.status = {
        "accepted": "unfixed",
        "rejected": "ignored",
        "evidence_requested": "pending_review",
    }[decision]
    issue.handled_by = user.id
    issue.handled_at = now
    db.commit()
    db.refresh(issue)
    return issue
