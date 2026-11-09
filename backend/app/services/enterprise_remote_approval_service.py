"""企业远测审批独立域；只有审批通过的固定范围可进入现有只读执行器。"""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.enterprise_org import Enterprise, EnterpriseAuditEvent, EnterpriseGroup, EnterpriseGroupMember
from app.models.enterprise_project import EnterpriseProject
from app.models.enterprise_remote_approval import EnterpriseRemoteApproval
from app.models.remote_pentest import RemotePentestEngagement
from app.models.user import User
from app.services import remote_pentest_service
from app.services.enterprise_chat_service import _lock_enterprise, _require_locked_member
from app.services.remote_pentest_service import normalize_target_url

SCOPE_VERSION = "enterprise-remote-v1"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _group(db: Session, enterprise_id: int, group_id: int):
    row = (db.query(EnterpriseGroup).filter_by(id=group_id, enterprise_id=enterprise_id, status="active")
           .populate_existing().with_for_update().first())
    if not row:
        raise NotFoundError("企业小组不存在")
    return row


def _group_membership(db: Session, enterprise_id: int, group_id: int, user_id: int):
    return (db.query(EnterpriseGroupMember).filter_by(
        enterprise_id=enterprise_id, group_id=group_id, user_id=user_id, status="active"
    ).populate_existing().with_for_update().first())


def _require_request_group(db: Session, enterprise_id: int, group_id: int, user_id: int, role: str):
    if role in {"owner", "admin"}:
        return
    if _group_membership(db, enterprise_id, group_id, user_id) is None:
        raise ForbiddenError("申请人不属于该企业小组")


def _require_approver(db: Session, row: EnterpriseRemoteApproval, actor_id: int):
    actor = _require_locked_member(db, row.enterprise_id, actor_id)
    if actor.role in {"owner", "admin"}:
        return actor
    group_member = _group_membership(db, row.enterprise_id, row.group_id, actor_id)
    if not group_member or group_member.role != "leader":
        raise ForbiddenError("只有企业管理员或该小组组长可以审批")
    return actor


def _locked_request(db: Session, enterprise_id: int, request_id: int):
    row = (db.query(EnterpriseRemoteApproval).filter_by(id=request_id, enterprise_id=enterprise_id)
           .populate_existing().with_for_update().first())
    if not row:
        raise NotFoundError("企业远测申请不存在")
    return row


def _digest(row: EnterpriseRemoteApproval) -> str:
    payload = {"version": row.scope_version, "enterprise_id": row.enterprise_id, "group_id": row.group_id,
               "owner_id": row.owner_id, "target_url": row.target_url, "target_host": row.target_host,
               "authorization_basis": row.authorization_basis, "scope_notes": row.scope_notes,
               "requested_window_hours": row.requested_window_hours}
    if row.project_id is not None:
        payload["project_id"] = int(row.project_id)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _require_project_access(
    db: Session, enterprise_id: int, group_id: int, project_id: int | None, user_id: int, action: str,
) -> None:
    if type(project_id) is not int or project_id <= 0:
        raise ForbiddenError("该远测申请没有有效的企业项目归属")
    binding = db.query(EnterpriseProject).filter_by(
        enterprise_id=enterprise_id, project_id=project_id, group_id=group_id, status="active",
    ).first()
    if binding is None:
        raise ForbiddenError("远测项目不属于当前企业小组或已停用")
    from app.services.enterprise_project_policy import require_enterprise_project_access

    require_enterprise_project_access(db, enterprise_id, project_id, user_id, action)


def _audit(db: Session, row: EnterpriseRemoteApproval, actor_id: int, action: str):
    db.add(EnterpriseAuditEvent(enterprise_id=row.enterprise_id, actor_id=actor_id, action=action,
                                subject_user_id=row.owner_id, group_id=row.group_id,
                                detail={"request_id": row.id, "scope_version": row.scope_version,
                                        "scope_digest": row.scope_digest}))


def submit_request(db: Session, enterprise_id: int, group_id: int, user_id: int, *, project_id: int, target_url: str,
                   authorization_basis: str, scope_notes: str, requested_window_hours: int, ack_rules: bool):
    _lock_enterprise(db, enterprise_id)
    company = db.query(Enterprise).filter_by(id=enterprise_id).populate_existing().with_for_update().one()
    if company.status != "active":
        raise ForbiddenError("企业当前暂停远程测试申请")
    actor = _require_locked_member(db, enterprise_id, user_id)
    _group(db, enterprise_id, group_id)
    _require_request_group(db, enterprise_id, group_id, user_id, actor.role)
    _require_project_access(db, enterprise_id, group_id, project_id, user_id, "execute")
    if ack_rules is not True or type(requested_window_hours) is not int or not 1 <= requested_window_hours <= 24:
        raise ValidationError("必须确认规则，申请时间窗须为 1 至 24 小时")
    if (not isinstance(authorization_basis, str) or not 12 <= len(authorization_basis.strip()) <= 1000
            or not isinstance(scope_notes, str) or len(scope_notes.strip()) > 2000):
        raise ValidationError("授权依据或范围说明无效")
    if not isinstance(target_url, str) or len(target_url) > 512:
        raise ValidationError("远程目标无效")
    origin, host = normalize_target_url(target_url)
    normalized_basis = authorization_basis.strip()
    normalized_notes = scope_notes.strip()
    prior = (db.query(EnterpriseRemoteApproval)
             .filter_by(enterprise_id=enterprise_id, group_id=group_id, project_id=project_id,
                        owner_id=user_id, target_url=origin,
                        authorization_basis=normalized_basis, scope_notes=normalized_notes,
                        requested_window_hours=requested_window_hours, status="pending")
             .populate_existing().with_for_update().order_by(EnterpriseRemoteApproval.id.desc()).first())
    if prior is not None:
        db.commit()
        return prior
    row = EnterpriseRemoteApproval(enterprise_id=enterprise_id, group_id=group_id, project_id=project_id,
                                   owner_id=user_id,
                                   target_url=origin, target_host=host, authorization_basis=normalized_basis,
                                   scope_notes=normalized_notes, requested_window_hours=requested_window_hours,
                                   scope_version=SCOPE_VERSION, status="pending", scope_digest="pending")
    row.scope_digest = _digest(row)
    db.add(row)
    db.flush()
    _audit(db, row, user_id, "remote_approval_submitted")
    db.commit()
    return row


def _create_authorized_execution(db: Session, row: EnterpriseRemoteApproval, actor_id: int):
    """Use the approval timestamp as the executor's immutable authorization start."""
    return remote_pentest_service.create_enterprise_approved_task(db, row, approver_id=actor_id)


def decide_request(db: Session, enterprise_id: int, request_id: int, actor_id: int, *, decision: str, reason: str):
    _lock_enterprise(db, enterprise_id)
    row = _locked_request(db, enterprise_id, request_id)
    _group(db, enterprise_id, row.group_id)
    approver = _require_approver(db, row, actor_id)
    if row.owner_id == actor_id and approver.role not in {"owner", "admin"}:
        raise ForbiddenError("企业小组长不得审批自己的远程测试申请")
    applicant = _require_locked_member(db, enterprise_id, row.owner_id)
    _require_request_group(db, enterprise_id, row.group_id, row.owner_id, applicant.role)
    if row.status != "pending":
        raise ConflictError("申请已被处理")
    if row.scope_version != SCOPE_VERSION or row.scope_digest != _digest(row):
        raise ConflictError("申请范围摘要不一致")
    if decision not in {"approved", "rejected"} or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise ValidationError("审批决定或原因无效")
    if decision == "approved":
        _require_project_access(db, enterprise_id, row.group_id, row.project_id, row.owner_id, "execute")
        _require_project_access(db, enterprise_id, row.group_id, row.project_id, actor_id, "execute")
    now = _now()
    row.status, row.decided_by, row.decision_reason, row.decided_at = decision, actor_id, reason.strip(), now
    if decision == "approved":
        _create_authorized_execution(db, row, actor_id)
    _audit(db, row, actor_id, f"remote_approval_{decision}")
    db.commit()
    return row


def cancel_request(db: Session, enterprise_id: int, request_id: int, actor_id: int):
    _lock_enterprise(db, enterprise_id)
    actor = _require_locked_member(db, enterprise_id, actor_id)
    row = _locked_request(db, enterprise_id, request_id)
    _group(db, enterprise_id, row.group_id)
    if row.owner_id != actor_id:
        raise ForbiddenError("只有申请人可以撤销申请")
    _require_request_group(db, enterprise_id, row.group_id, actor_id, actor.role)
    if row.status == "approved":
        if not row.execution_task_public_id:
            raise ConflictError("已批准申请缺少执行任务，需由平台管理员核对")
        task = db.query(RemotePentestEngagement).filter_by(
            public_id=row.execution_task_public_id, owner_id=row.owner_id
        ).populate_existing().with_for_update().first()
        if task is None or task.status != "authorized":
            raise ConflictError("远测任务已开始或终结，不能撤销授权")
        row.status = "cancelled"
        _audit(db, row, actor_id, "remote_approval_cancelled")
        remote_pentest_service.cancel_task(db, db.get(User, actor_id), task.public_id)
        return row
    if row.status != "pending":
        raise ConflictError("申请已被处理")
    row.status = "cancelled"
    _audit(db, row, actor_id, "remote_approval_cancelled")
    db.commit()
    return row


def require_execution_authorization(db: Session, task: RemotePentestEngagement, user_id: int,
                                    *, now: datetime | None = None) -> bool:
    """Revalidate the linked enterprise approval on every start, including legacy task routes."""
    # All enterprise task transitions lock in the same order: enterprise,
    # applicant membership, approval row, then group/task. A non-locking lookup
    # supplies the tenant key before taking that canonical lock sequence.
    candidate = db.query(EnterpriseRemoteApproval).filter_by(
        execution_task_public_id=task.public_id
    ).first()
    if candidate is None:
        if bool(getattr(task, "enterprise_approval_required", False)):
            raise ForbiddenError("企业远程测试审批关联缺失，拒绝继续执行")
        return False
    _lock_enterprise(db, candidate.enterprise_id)
    _require_locked_member(db, candidate.enterprise_id, user_id)
    row = (db.query(EnterpriseRemoteApproval)
           .filter_by(id=candidate.id, execution_task_public_id=task.public_id)
           .populate_existing().with_for_update().first())
    if row is None:
        raise ForbiddenError("企业远程测试审批关联已变化，拒绝启动")
    observed_at = now or _now()
    if observed_at.tzinfo is not None:
        observed_at = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    company = (db.query(Enterprise).filter_by(id=row.enterprise_id)
               .populate_existing().with_for_update().first())
    if company is None or company.status != "active":
        raise ForbiddenError("企业已暂停，不能启动远程测试")
    if (row.status != "approved" or row.decided_by is None
            or row.execution_task_public_id != task.public_id):
        raise ForbiddenError("企业远程测试审批已撤销或无效")
    if row.owner_id != user_id:
        raise ForbiddenError("只有企业远测申请人可以启动关联任务")
    applicant = _require_locked_member(db, row.enterprise_id, row.owner_id)
    _group(db, row.enterprise_id, row.group_id)
    _require_request_group(db, row.enterprise_id, row.group_id, row.owner_id, applicant.role)
    approver = _require_approver(db, row, row.decided_by)
    if row.decided_by == row.owner_id and approver.role not in {"owner", "admin"}:
        raise ForbiddenError("企业小组长不得审批自己的远程测试申请")
    _require_project_access(db, row.enterprise_id, row.group_id, row.project_id, row.owner_id, "execute")
    _require_project_access(db, row.enterprise_id, row.group_id, row.project_id, row.decided_by, "execute")
    if row.scope_version != SCOPE_VERSION or row.scope_digest != _digest(row):
        raise ForbiddenError("企业审批范围已变化，拒绝启动远程测试")
    if not row.decided_at:
        raise ForbiddenError("企业远程测试缺少有效授权时间")
    expected_end = row.decided_at + timedelta(hours=row.requested_window_hours)
    if observed_at < row.decided_at or observed_at >= expected_end:
        raise ForbiddenError("企业远程测试授权时间窗已结束")
    if (task.owner_id != row.owner_id or task.project_id != row.project_id
            or task.target_url != row.target_url or task.target_host != row.target_host
            or task.authorization_basis != row.authorization_basis or task.scope_notes != row.scope_notes
            or task.authorized_by_user_id != row.decided_by or task.authorized_at != row.decided_at
            or task.requested_window_hours != row.requested_window_hours or task.window_start != row.decided_at
            or task.window_end != expected_end or task.rules_ack != 1
            or task.scope_digest != remote_pentest_service._scope_digest(task)):
        raise ForbiddenError("远程执行任务与企业审批快照不一致，拒绝启动")
    return True


def start_request(db: Session, enterprise_id: int, request_id: int, user_id: int):
    _lock_enterprise(db, enterprise_id)
    applicant = _require_locked_member(db, enterprise_id, user_id)
    row = _locked_request(db, enterprise_id, request_id)
    _group(db, enterprise_id, row.group_id)
    if row.owner_id != user_id:
        raise NotFoundError("企业远测申请不存在")
    _require_request_group(db, enterprise_id, row.group_id, user_id, applicant.role)
    if row.status != "approved" or not row.execution_task_public_id:
        raise ConflictError("企业远程测试尚未获得有效审批")
    task = (db.query(RemotePentestEngagement)
            .filter_by(public_id=row.execution_task_public_id, owner_id=user_id)
            .populate_existing().with_for_update().first())
    if task is None:
        raise ConflictError("企业远程测试执行任务不存在")
    require_execution_authorization(db, task, user_id)
    _audit(db, row, user_id, "remote_approval_execution_start_requested")
    result = remote_pentest_service.start_task(db, db.get(User, user_id), task.public_id)
    return row, result


def list_requests(db: Session, enterprise_id: int, user_id: int, *, limit: int = 50, offset: int = 0):
    _lock_enterprise(db, enterprise_id)
    actor = _require_locked_member(db, enterprise_id, user_id)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValidationError("分页参数无效")
    query = db.query(EnterpriseRemoteApproval).join(
        EnterpriseGroup, and_(EnterpriseGroup.id == EnterpriseRemoteApproval.group_id,
                              EnterpriseGroup.enterprise_id == enterprise_id, EnterpriseGroup.status == "active")
    ).filter(EnterpriseRemoteApproval.enterprise_id == enterprise_id)
    if actor.role not in {"owner", "admin"}:
        membership = exists().where(and_(
            EnterpriseGroupMember.enterprise_id == enterprise_id,
            EnterpriseGroupMember.group_id == EnterpriseRemoteApproval.group_id,
            EnterpriseGroupMember.user_id == user_id, EnterpriseGroupMember.status == "active",
        ))
        leader = exists().where(and_(
            EnterpriseGroupMember.enterprise_id == enterprise_id,
            EnterpriseGroupMember.group_id == EnterpriseRemoteApproval.group_id,
            EnterpriseGroupMember.user_id == user_id, EnterpriseGroupMember.status == "active",
            EnterpriseGroupMember.role == "leader",
        ))
        query = query.filter(or_(and_(EnterpriseRemoteApproval.owner_id == user_id, membership),
                                 leader))
    total = query.count()
    return query.order_by(EnterpriseRemoteApproval.id.desc()).offset(offset).limit(limit).all(), total


def get_request(db: Session, enterprise_id: int, request_id: int, user_id: int):
    # Reuse the scoped list condition without a global-ID shortcut or platform admin bypass.
    _lock_enterprise(db, enterprise_id)
    actor = _require_locked_member(db, enterprise_id, user_id)
    row = _locked_request(db, enterprise_id, request_id)
    _group(db, enterprise_id, row.group_id)
    if actor.role not in {"owner", "admin"}:
        member = _group_membership(db, enterprise_id, row.group_id, user_id)
        if not member or (row.owner_id != user_id and member.role != "leader"):
            raise NotFoundError("企业远测申请不存在")
    return row
