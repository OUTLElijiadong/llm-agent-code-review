"""
报告服务模块
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

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
from app.services.report_exporter import build_report_score_facts


def is_report_available(task: ReviewTask | None) -> bool:
    """报告已生成且可查看；沙箱失败报告也必须保留可读性。"""
    return bool(
        task
        and (
            task.status == "success"
            or (task.status == "failed" and task.review_type == "sandbox_test")
        )
    )


def list_reports(db: Session, user: User, project_id: int = None,
                 start_date: str = "", end_date: str = "",
                 page: int = 1, page_size: int = 20) -> dict:
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
    q = db.query(ReviewTask).filter(
        (ReviewTask.status == "success")
        | ((ReviewTask.status == "failed") & (ReviewTask.review_type == "sandbox_test"))
    )
    if not rbac_service.is_admin_user(db, int(user.id)):
        q = q.filter(ReviewTask.user_id == user.id)
    if project_id:
        q = q.filter(ReviewTask.project_id == project_id)
    if start_date:
        q = q.filter(ReviewTask.create_time >= start_date)
    if end_date:
        q = q.filter(ReviewTask.create_time <= end_date + " 23:59:59")

    total = q.count()
    pagination = Pagination(page, page_size, total)
    rows = q.order_by(ReviewTask.create_time.desc()).offset(pagination.offset).limit(pagination.page_size).all()
    issue_stats = load_task_issue_stats(db, rows)

    items = []
    for row in rows:
        project = db.get(Project, row.project_id)
        issue_count, score, _breakdown, _severity_count = _build_task_score_facts(
            row,
            issue_stats[row.id],
        )
        items.append({
            "id": row.id, "task_id": row.id, "task_name": row.task_name,
            "project_name": project.project_name if project else "",
            "total_issues": issue_count, "score": score, "status": row.status,
            "source": issue_stats[row.id]["source"],
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
    task = db.get(ReviewTask, task_id)
    if not is_report_available(task):
        raise NotFoundError("报告不存在", code=40400)
    if task.user_id != user.id and not rbac_service.is_admin_user(db, int(user.id)):
        raise NotFoundError("报告不存在", code=40400)

    project = db.get(Project, task.project_id)

    issue_stats = load_task_issue_stats(db, [task])[task_id]
    severity_from_issues = issue_stats["severity"]
    issue_count, score, score_breakdown, severity_count = _build_task_score_facts(
        task,
        issue_stats,
    )

    from app.services.review_service import _task_agent_release_summaries

    return {
        "project": {"id": project.id, "project_name": project.project_name,
                     "language": project.language} if project else {},
        "task": {"id": task.id, "name": task.task_name, "task_name": task.task_name,
                 "review_type": task.review_type,
                 "total_files": task.total_files,
                 "duration_ms": task.duration_ms,
                 "total_issues": issue_count,
                 "score": score,
                 "score_version": score_breakdown.get("version"),
                 "score_breakdown": score_breakdown,
                 "status": task.status,
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
        "source": issue_stats["source"],
        "summary": task.summary,
        "files": [] if task.review_type in {"sandbox_test", "pentest"} else _build_file_summaries(db, task_id),
        "rules_snapshot": task.rules_snapshot or [],
    }


def _count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def get_domain_report_export(db: Session, user: User, task: ReviewTask, format: str) -> dict | None:
    """领域导出使用真实原始数据，不能落入空 ReviewIssue 的标准报告模板。"""
    if task.review_type not in {"sandbox_test", "pentest"}:
        return None
    detail = get_report_detail(db, user, task.id)
    json_api = f"/api/reports/tasks/{task.id}/export?format=json"
    if format != "json":
        raise ConflictError(
            f"该领域报告不支持等价的 {format.upper()} 导出，不能生成空标准报告",
            code=40941,
            detail={"source": detail["source"], "supported_formats": ["json"], "download_api": json_api},
            next_action=f"请下载真实领域 JSON：GET {json_api}；沙箱原生报告下载地址见 native_exports",
        )

    native_exports = []
    if task.review_type == "sandbox_test":
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
        domain_data = dict(content)
        native_exports = _sandbox_native_exports(db, user, task, content)
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
        if len(domain_data["findings"]) != detail["stats"]["total_issues"]:
            raise ConflictError(
                "领域发现正在变化，导出计数不一致", code=40943, retryable=True,
                next_action=f"请等待领域任务结束后重试 GET {json_api}",
            )

    return {
        "source": detail["source"], "task_info": detail["task"], "project": detail["project"],
        "statistics": detail["stats"], "score": detail["task"]["score"],
        "summary": detail["summary"] or "", "domain_data": domain_data, "native_exports": native_exports,
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
            from app.services.sandbox_report_summary import summarize_sandbox_report

            summary = summarize_sandbox_report(content.get("report_md"))
            item["source"]["report_issue_summary"] = summary
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
) -> tuple[int, int, dict, dict[str, int]]:
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
            "risk_level": risk_level,
        }, severity_count
    score, score_breakdown = build_report_score_facts(
        severity_count,
        issue_count,
        task.score,
    )
    return issue_count, score, score_breakdown, severity_count


def _build_file_summaries(db: Session, task_id: int) -> list[dict]:
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

    linked_files = db.query(CodeFile).join(
        ReviewTaskFile,
        ReviewTaskFile.file_id == CodeFile.id,
    ).filter(
        ReviewTaskFile.task_id == task_id,
    ).order_by(
        ReviewTaskFile.id.asc(),
    ).all()

    summaries = []
    emitted_keys: set[tuple[str, object]] = set()
    for code_file in linked_files:
        key = ("id", code_file.id)
        emitted_keys.add(key)
        summaries.append(_file_summary(code_file, counts.get(key)))

    fallback_ids = [key[1] for key in counts if key not in emitted_keys and key[0] == "id"]
    fallback_files = {
        row.id: row for row in db.query(CodeFile).filter(CodeFile.id.in_(fallback_ids)).all()
    } if fallback_ids else {}
    for key, item in counts.items():
        if key in emitted_keys:
            continue
        summaries.append(_file_summary(fallback_files.get(key[1]), item))
    return summaries


def _file_summary(code_file: CodeFile | None, counts: dict | None) -> dict:
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
    return {
        "file_id": code_file.id if code_file else item["file_id"],
        "file_name": code_file.file_name if code_file else item["file_name"],
        "language": code_file.language if code_file else "",
        "issue_count": sum(severity.values()),
        "severe_count": severity.get("严重", 0),
        "score": compute_score(severity),
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
