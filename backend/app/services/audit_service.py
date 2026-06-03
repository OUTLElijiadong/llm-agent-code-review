"""
操作审计服务

提供 `log(...)` 埋点函数,以及管理员视角的分页查询。所有 DB 写入都包裹 try 以防审计失败拖累主流程。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.core.pagination import Pagination
from app.models.audit_log import AuditLog
from app.models.user import User


def log(
    db: Session,
    actor: Optional[User],
    action: str,
    *,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    detail: Optional[str] = None,
    status: str = "success",
    ip: Optional[str] = None,
    commit: bool = True,
) -> None:
    """记录一条审计日志

    Args:
        db: 数据库会话
        actor: 操作者 (允许 None,表示系统操作)
        action: 操作类型(login/user/rule/ai/project/agent 等)
        target_type: 对象类型
        target_id: 对象主键(字符串,兼容非整型)
        detail: 自由文本说明
        status: success/failed
        ip: 请求来源 IP
        commit: 是否立即 commit;在外层事务中调用时可设 False
    """
    try:
        entry = AuditLog(
            actor_id=actor.id if actor else None,
            actor_name=actor.username if actor else None,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=detail,
            status=status,
            ip=ip,
            create_time=datetime.utcnow(),
        )
        db.add(entry)
        if commit:
            db.commit()
    except Exception:
        # 审计失败不能影响主业务,日志会被全局异常处理捕获
        if commit:
            db.rollback()


def list_logs(
    db: Session,
    action: str = "",
    keyword: str = "",
    actor_id: Optional[int] = None,
    start: str = "",
    end: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """分页查询审计日志

    Args:
        db: 数据库会话
        action: 操作类型过滤
        keyword: 操作描述/操作者模糊匹配
        actor_id: 限定操作者
        start: 起始日期 (YYYY-MM-DD)
        end: 结束日期 (YYYY-MM-DD)
        page: 页码
        page_size: 每页数量

    Returns:
        dict: 分页响应
    """
    q = db.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action == action)
    if actor_id is not None:
        q = q.filter(AuditLog.actor_id == actor_id)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(and_(AuditLog.detail.like(like) | AuditLog.actor_name.like(like)))
    if start:
        try:
            start_dt = datetime.fromisoformat(start)
            q = q.filter(AuditLog.create_time >= start_dt)
        except ValueError:
            pass
    if end:
        try:
            end_dt = datetime.fromisoformat(end)
            q = q.filter(AuditLog.create_time <= end_dt)
        except ValueError:
            pass

    total = q.count()
    pagination = Pagination(page, page_size, total)
    rows = (
        q.order_by(AuditLog.create_time.desc())
        .offset(pagination.offset)
        .limit(pagination.page_size)
        .all()
    )
    return pagination.to_dict(rows)
