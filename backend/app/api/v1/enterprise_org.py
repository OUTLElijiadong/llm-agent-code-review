"""企业成员和小组 API；企业创建由支付成功的可信服务负责。"""
from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.enterprise_org import (
    Enterprise,
    EnterpriseGroup,
    EnterpriseGroupMember,
    EnterpriseInvitation,
    EnterpriseMember,
)
from app.models.user import User
from app.schemas.common import Resp, StrictInputModel
from app.services import billing_checkout_service
from app.services import enterprise_org_service as service

router = APIRouter()


class GroupCreate(StrictInputModel):
    name: str = Field(min_length=1, max_length=120)


class InvitationCreate(StrictInputModel):
    ttl_hours: int = Field(default=24, ge=1, le=168)


class InvitationAccept(StrictInputModel):
    token: str = Field(min_length=1, max_length=256)


class MemberRole(StrictInputModel):
    role: str = Field(min_length=1, max_length=20)


class GroupMemberSet(StrictInputModel):
    user_id: int = Field(gt=0)
    role: str = Field(min_length=1, max_length=20)


class DefaultGroupSet(StrictInputModel):
    group_id: int | None = Field(default=None, gt=0)


class OwnerTransfer(StrictInputModel):
    user_id: int = Field(gt=0)


def _member(row: EnterpriseMember) -> dict:
    return {"user_id": row.user_id, "role": row.role, "status": row.status,
            "default_group_id": row.default_group_id}


def _group(row: EnterpriseGroup) -> dict:
    return {"id": row.id, "enterprise_id": row.enterprise_id, "name": row.name, "status": row.status}


@router.get("/mine", response_model=Resp[list[dict]])
def my_enterprises(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Enterprise, EnterpriseMember).join(
        EnterpriseMember, EnterpriseMember.enterprise_id == Enterprise.id
    ).filter(EnterpriseMember.user_id == user.id, EnterpriseMember.status == "active",
             Enterprise.status.in_(["active", "suspended"])).order_by(Enterprise.id).all()
    result = []
    for company, member in rows:
        subscription = billing_checkout_service.enterprise_subscription_info(db, company.id)
        active_members = db.query(EnterpriseMember).filter_by(
            enterprise_id=company.id, status="active"
        ).count()
        result.append({
            "id": company.id,
            "name": company.name,
            "status": company.status,
            "role": member.role,
            "default_group_id": member.default_group_id,
            "seat_limit": company.seat_limit,
            "active_member_count": active_members,
            "subscription_status": subscription["status"],
            "subscription_ends_at": subscription["ends_at"].isoformat() if subscription["ends_at"] else None,
            "over_capacity": active_members > company.seat_limit,
        })
    return Resp(data=result)


@router.post("/invitations/accept", response_model=Resp[dict])
def accept_invitation(payload: InvitationAccept, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    member = service.accept_invitation(db, payload.token, user.id)
    db.commit()
    return Resp(data={"enterprise_id": member.enterprise_id, **_member(member)})


@router.get("/{enterprise_id}/members", response_model=Resp[list[dict]])
def list_members(enterprise_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service.require_enterprise_admin(db, enterprise_id, user.id)
    members = db.query(EnterpriseMember).filter_by(enterprise_id=enterprise_id, status="active").order_by(
        EnterpriseMember.id
    ).all()
    return Resp(data=[_member(row) for row in members])


@router.get("/{enterprise_id}/directory", response_model=Resp[list[dict]])
def member_directory(
    enterprise_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Minimal same-enterprise directory for addressing conversations."""
    service.require_enterprise_member(db, enterprise_id, user.id)
    rows = (
        db.query(User.id, User.nickname, User.username)
        .join(EnterpriseMember, EnterpriseMember.user_id == User.id)
        .filter(EnterpriseMember.enterprise_id == enterprise_id, EnterpriseMember.status == "active", User.status == 1)
        .order_by(User.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return Resp(data=[{"user_id": row.id, "display_name": row.nickname or row.username} for row in rows])


@router.post("/{enterprise_id}/invitations", response_model=Resp[dict])
def create_invitation(enterprise_id: int, payload: InvitationCreate, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    token = service.create_invitation(db, enterprise_id, user.id, ttl_hours=payload.ttl_hours)
    db.commit()
    return Resp(data={"token": token, "expires_in_hours": payload.ttl_hours})


@router.get("/{enterprise_id}/invitations", response_model=Resp[list[dict]])
def list_invitations(enterprise_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service.require_enterprise_admin(db, enterprise_id, user.id)
    rows = db.query(EnterpriseInvitation).filter_by(enterprise_id=enterprise_id).order_by(
        EnterpriseInvitation.id.desc()
    ).limit(200).all()
    return Resp(data=[{"id": row.id, "created_by": row.created_by, "expires_at": row.expires_at,
                       "use_count": row.use_count, "revoked_at": row.revoked_at} for row in rows])


@router.delete("/{enterprise_id}/invitations/{invitation_id}", response_model=Resp[None])
def revoke_invitation(enterprise_id: int, invitation_id: int, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    service.revoke_invitation(db, enterprise_id, user.id, invitation_id)
    db.commit()
    return Resp(data=None)


@router.post("/{enterprise_id}/owner-transfer", response_model=Resp[dict])
def transfer_owner(enterprise_id: int, payload: OwnerTransfer, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    company = service.transfer_enterprise_owner(db, enterprise_id, user.id, payload.user_id)
    db.commit()
    return Resp(data={"enterprise_id": company.id, "owner_id": company.owner_id})


@router.put("/{enterprise_id}/members/{user_id}/role", response_model=Resp[dict])
def set_member_role(enterprise_id: int, user_id: int, payload: MemberRole, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    member = service.set_enterprise_role(db, enterprise_id, user.id, user_id, payload.role)
    db.commit()
    return Resp(data=_member(member))


@router.delete("/{enterprise_id}/members/{user_id}", response_model=Resp[None])
def remove_member(enterprise_id: int, user_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    service.remove_enterprise_member(db, enterprise_id, user.id, user_id)
    db.commit()
    return Resp(data=None)


@router.put("/{enterprise_id}/members/{user_id}/default-group", response_model=Resp[dict])
def set_default_group(enterprise_id: int, user_id: int, payload: DefaultGroupSet,
                      db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    member = service.set_default_group(db, enterprise_id, user.id, user_id, payload.group_id)
    db.commit()
    return Resp(data=_member(member))


@router.get("/{enterprise_id}/groups", response_model=Resp[list[dict]])
def list_groups(enterprise_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    member = service.require_enterprise_member(db, enterprise_id, user.id)
    query = db.query(EnterpriseGroup).filter_by(enterprise_id=enterprise_id, status="active")
    if member.role not in {"owner", "admin"}:
        query = query.join(EnterpriseGroupMember, EnterpriseGroupMember.group_id == EnterpriseGroup.id).filter(
            EnterpriseGroupMember.enterprise_id == enterprise_id,
            EnterpriseGroupMember.user_id == user.id,
            EnterpriseGroupMember.status == "active",
        )
    rows = query.order_by(EnterpriseGroup.id).all()
    roles = dict(db.query(EnterpriseGroupMember.group_id, EnterpriseGroupMember.role).filter_by(
        enterprise_id=enterprise_id, user_id=user.id, status="active"
    ).all())
    return Resp(data=[{**_group(row), "my_role": roles.get(row.id)} for row in rows])


@router.post("/{enterprise_id}/groups", response_model=Resp[dict])
def create_group(enterprise_id: int, payload: GroupCreate, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    group = service.create_group(db, enterprise_id, user.id, payload.name)
    db.commit()
    return Resp(data=_group(group))


@router.get("/{enterprise_id}/groups/{group_id}/members", response_model=Resp[list[dict]])
def list_group_members(enterprise_id: int, group_id: int, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    service.require_group_member(db, enterprise_id, group_id, user.id)
    rows = db.query(EnterpriseGroupMember).filter_by(
        enterprise_id=enterprise_id, group_id=group_id, status="active"
    ).order_by(EnterpriseGroupMember.id).all()
    return Resp(data=[{"user_id": row.user_id, "role": row.role} for row in rows])


@router.put("/{enterprise_id}/groups/{group_id}/members", response_model=Resp[dict])
def set_group_member(enterprise_id: int, group_id: int, payload: GroupMemberSet,
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    member = service.set_group_member(db, enterprise_id, group_id, user.id, payload.user_id, payload.role)
    db.commit()
    return Resp(data={"user_id": member.user_id, "role": member.role})


@router.delete("/{enterprise_id}/groups/{group_id}/members/{user_id}", response_model=Resp[None])
def remove_group_member(enterprise_id: int, group_id: int, user_id: int,
                        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service.remove_group_member(db, enterprise_id, group_id, user.id, user_id)
    db.commit()
    return Resp(data=None)
