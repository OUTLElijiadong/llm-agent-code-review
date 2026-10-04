"""管理员安全中心 API；所有数据均限制为唯一超级管理员。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_super_admin
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.security_center import SecurityMonitorPolicyIn
from app.services import security_center_service

router = APIRouter()


@router.get("/overview", response_model=Resp[dict])
def get_security_center_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """读取持久化巡检状态、告警计数和有效监控策略，不触发采集。"""
    return Resp(data=security_center_service.get_overview(db))


@router.get("/events", response_model=Resp[dict])
def get_security_center_events(
    hours: int = Query(24, ge=1, le=720),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """按记录时间分页读取脱敏的告警、采集、巡检和策略变更摘要。"""
    return Resp(data=security_center_service.list_events(db, hours=hours, page=page, page_size=page_size))


@router.get("/policy", response_model=Resp[dict])
def get_security_center_policy(
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """读取有效的安全监控阈值，永不回显主机密钥或环境变量。"""
    return Resp(data=security_center_service.get_policy(db))


@router.put("/policy", response_model=Resp[dict])
def put_security_center_policy(
    payload: SecurityMonitorPolicyIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    """保存仅可提高或保持监控灵敏度的策略，并在同一事务写入操作审计。"""
    data = security_center_service.update_policy(db, actor, payload.model_dump())
    return Resp(data=data)
