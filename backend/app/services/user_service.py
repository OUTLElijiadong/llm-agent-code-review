"""
用户管理服务模块
"""
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.pagination import Pagination
from app.core.security import hash_password
from app.models.user import User


def list_users(db: Session, keyword: str = "", role: str = "", status: str = "",
               page: int = 1, page_size: int = 20) -> dict:
    """管理员查询用户列表(支持搜索过滤与分页)

    Args:
        db: 数据库会话
        keyword: 用户名搜索关键字
        role: 角色过滤
        status: 状态过滤(1启用/0禁用)
        page: 页码
        page_size: 每页数量

    Returns:
        dict: 分页响应
    """
    q = db.query(User).filter(User.status != -1)  # 软删用户默认不显示
    if keyword:
        q = q.filter(User.username.contains(keyword) | User.nickname.contains(keyword))
    if role:
        q = q.filter(User.role == role)
    if status != "":
        q = q.filter(User.status == (int(status) if status.lstrip("-").isdigit() else 1))
    total = q.count()
    pagination = Pagination(page, page_size, total)
    items = q.order_by(User.id.asc()).offset(pagination.offset).limit(pagination.page_size).all()
    return pagination.to_dict(items)


def reset_password(db: Session, user_id: int) -> dict:
    """管理员重置用户密码为默认密码123456

    Args:
        db: 数据库会话
        user_id: 目标用户ID

    Returns:
        dict: 含default_password字段

    Raises:
        NotFoundError: 用户不存在
    """
    user = db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在", code=40400)
    default_pwd = "123456"
    user.password = hash_password(default_pwd)
    # 重置密码后吊销该用户此前所有 JWT
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    return {"default_password": default_pwd}


def toggle_status(db: Session, user_id: int, status: int) -> None:
    """管理员启用/禁用用户

    Args:
        db: 数据库会话
        user_id: 目标用户ID
        status: 1启用/0禁用
    """
    user = db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在", code=40400)
    user.status = status
    # 禁用用户时立即吊销其所有 JWT,避免被禁用后旧令牌仍可用
    if status == 0:
        user.token_version = (user.token_version or 0) + 1
    db.commit()


def set_role(
    db: Session,
    user_id: int,
    role: str,
    admin_id: int = 0,
    *,
    commit: bool = True,
) -> None:
    """管理员设置用户角色

    Args:
        db: 数据库会话
        user_id: 目标用户ID
        role: 角色(admin/user/reviewer)
        admin_id: 当前审批管理员 ID；用于阻止系统失去最后一个管理员
    """
    user = db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在", code=40400)
    if role not in ("admin", "user", "reviewer"):
        raise ValidationError("角色不合法", code=40001)
    if user.role in ("admin", "super_admin") and role not in ("admin", "super_admin"):
        remaining = (
            db.query(User)
            .filter(User.role.in_(["admin", "super_admin"]), User.status == 1, User.id != user_id)
            .count()
        )
        if remaining == 0:
            raise ForbiddenError("不能降级最后一个可用管理员账号", code=40320)
    user.role = role
    # 同步新版 RBAC 关联，避免 legacy role 与 user_role 两套事实源分裂。
    from app.models.rbac import Role, UserRole

    target_role = db.query(Role).filter(Role.code == role, Role.status == "active").first()
    if target_role:
        db.query(UserRole).filter(UserRole.user_id == user_id).delete()
        db.add(UserRole(user_id=user_id, role_id=target_role.id))
    if commit:
        db.commit()


def delete_user(
    db: Session,
    user_id: int,
    admin_id: int,
    *,
    commit: bool = True,
) -> None:
    """管理员软删除用户(status=-1)。

    软删保留项目与历史数据(审计/帖子/项目仍可读,操作者显示为快照名),
    仅禁止该账号再登录;同时吊销其全部 JWT。

    Args:
        db: 数据库会话
        user_id: 目标用户ID
        admin_id: 操作的管理员ID

    Raises:
        NotFoundError: 用户不存在
        ValidationError: 不能删除自己
        ForbiddenError: 不能删除最后一个可用管理员
    """
    user = db.get(User, user_id)
    if not user or user.status == -1:
        raise NotFoundError("用户不存在", code=40400)
    if user.id == admin_id:
        raise ValidationError("不能删除当前登录的管理员账号", code=42220)
    if user.role in ("admin", "super_admin"):
        # 保护:不允许删掉最后一个可用管理员,否则系统失去管理入口
        remaining = (
            db.query(User)
            .filter(User.role.in_(["admin", "super_admin"]), User.status == 1, User.id != user_id)
            .count()
        )
        if remaining == 0:
            raise ForbiddenError("不能删除最后一个可用管理员账号", code=40320)
    user.status = -1
    # 吊销该用户全部 JWT,删除后立即无法再访问
    user.token_version = (user.token_version or 0) + 1
    if commit:
        db.commit()
