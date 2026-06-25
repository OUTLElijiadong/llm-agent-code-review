"""
项目成员管理 API 路由

v2.4: 提供项目成员关系的 CRUD 接口
    - GET    /projects/{project_id}/members        列出成员(需读权限)
    - POST   /projects/{project_id}/members        添加成员(需写权限)
    - PUT    /projects/{project_id}/members/{uid}  修改成员角色(需写权限)
    - DELETE /projects/{project_id}/members/{uid}  移除成员(需写权限)

权限规则:
    - admin: 全部操作
    - owner: 可读写本项目成员
    - reviewer: 可读成员列表,不可写
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.project_member import MemberAddIn, MemberOut, MemberRoleUpdateIn
from app.services import project_member_service

router = APIRouter()


@router.get("", response_model=Resp[list[MemberOut]])
def list_members(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出项目成员

    Args:
        project_id: 项目ID
        db: 数据库会话
        user: 当前用户

    Returns:
        Resp[list[MemberOut]]: 成员列表
    """
    project_member_service.require_project_access(db, project_id, user, need_write=False)
    members = project_member_service.list_members(db, project_id)
    return Resp(data=[MemberOut(**m) for m in members])


@router.post("", response_model=Resp[MemberOut])
def add_member(
    project_id: int,
    payload: MemberAddIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """添加项目成员(需 owner/admin 权限)

    Args:
        project_id: 项目ID
        payload: 添加成员请求体
        db: 数据库会话
        user: 当前用户

    Returns:
        Resp[MemberOut]: 新成员信息
    """
    member = project_member_service.add_member(
        db,
        project_id=project_id,
        user_id=payload.user_id,
        role=payload.role_in_project,
        operator=user,
    )
    # 复用 list_members 拼装用户基本信息
    rows = project_member_service.list_members(db, project_id)
    for r in rows:
        if r["user_id"] == payload.user_id:
            return Resp(data=MemberOut(**r))
    return Resp(data=MemberOut(
        id=member.id,
        user_id=payload.user_id,
        username="",
        nickname=None,
        role_in_project=member.role_in_project,
        create_time=member.create_time,
    ))


@router.put("/{user_id}", response_model=Resp[None])
def update_member_role(
    project_id: int,
    user_id: int,
    payload: MemberRoleUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """更新成员角色(需 owner/admin 权限)

    Args:
        project_id: 项目ID
        user_id: 被修改的用户ID
        payload: 新角色请求体
        db: 数据库会话
        user: 当前用户
    """
    project_member_service.update_member_role(
        db,
        project_id=project_id,
        user_id=user_id,
        new_role=payload.role_in_project,
        operator=user,
    )
    return Resp(data=None)


@router.delete("/{user_id}", response_model=Resp[None])
def remove_member(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """移除项目成员(需 owner/admin 权限)

    Args:
        project_id: 项目ID
        user_id: 被移除的用户ID
        db: 数据库会话
        user: 当前用户
    """
    project_member_service.remove_member(
        db,
        project_id=project_id,
        user_id=user_id,
        operator=user,
    )
    return Resp(data=None)
