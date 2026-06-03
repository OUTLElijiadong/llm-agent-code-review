"""
审查规则API路由
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.rule import RuleIn, RuleOut, RuleToggleIn, RuleUpdateIn
from app.services import audit_service, rule_service

router = APIRouter()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.get("", response_model=Resp[list[RuleOut]])
def list_rules(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """规则列表(含内置+当前用户自定义)"""
    rules = rule_service.list_rules(db, user.id)
    return Resp(data=[RuleOut.model_validate(r) for r in rules])


@router.post("/{rule_id}/toggle", response_model=Resp[None])
def toggle_rule(rule_id: int, payload: RuleToggleIn, request: Request,
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """启用/禁用规则"""
    rule_service.toggle_rule(db, rule_id, payload.enabled)
    audit_service.log(
        db, user, "rule",
        target_type="rule", target_id=rule_id,
        detail=f"{'启用' if payload.enabled else '禁用'}规则 {rule_id}",
        ip=_client_ip(request),
    )
    return Resp(data=None)


@router.post("", response_model=Resp[dict])
def create_rule(payload: RuleIn, request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """新增自定义规则"""
    rule = rule_service.create_rule(db, user.id, payload)
    audit_service.log(
        db, user, "rule",
        target_type="rule", target_id=rule.id,
        detail=f"新增自定义规则: {payload.rule_name}",
        ip=_client_ip(request),
    )
    return Resp(data={"id": rule.id})


@router.put("/{rule_id}", response_model=Resp[None])
def update_rule(rule_id: int, payload: RuleUpdateIn, request: Request,
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """更新自定义规则"""
    rule_service.update_rule(db, rule_id, payload)
    audit_service.log(
        db, user, "rule",
        target_type="rule", target_id=rule_id,
        detail=f"更新规则 {rule_id}",
        ip=_client_ip(request),
    )
    return Resp(data=None)


@router.delete("/{rule_id}", response_model=Resp[None])
def delete_rule(rule_id: int, request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """删除自定义规则"""
    rule_service.delete_rule(db, rule_id)
    audit_service.log(
        db, user, "rule",
        target_type="rule", target_id=rule_id,
        detail=f"删除规则 {rule_id}",
        ip=_client_ip(request),
    )
    return Resp(data=None)
