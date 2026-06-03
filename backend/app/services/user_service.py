"""
用户管理服务模块
"""
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
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
    q = db.query(User)
    if keyword:
        q = q.filter(User.username.contains(keyword) | User.nickname.contains(keyword))
    if role:
        q = q.filter(User.role == role)
    if status != "":
        q = q.filter(User.status == int(status) if status.isdigit() else 1)
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
    db.commit()


def set_role(db: Session, user_id: int, role: str) -> None:
    """管理员设置用户角色

    Args:
        db: 数据库会话
        user_id: 目标用户ID
        role: 角色(admin/user/reviewer)
    """
    user = db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在", code=40400)
    user.role = role
    db.commit()
