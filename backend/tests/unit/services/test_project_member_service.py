"""单元测试:project_member_service 数据隔离(v2.4)

验证:
1. get_visible_project_ids:admin 全量 / 非 admin: owner ∪ member
2. is_project_member:admin/owner/reviewer/无权限 四种情况
3. require_project_access:读权限/写权限校验
4. add_member / remove_member / update_member_role CRUD
5. ensure_owner_member:幂等性
"""
from __future__ import annotations

import pytest

from app.core.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User
from app.services.project_member_service import (
    add_member,
    ensure_owner_member,
    get_visible_project_ids,
    is_project_member,
    list_members,
    remove_member,
    require_project_access,
    update_member_role,
)


# ============ 辅助函数 ============

def _make_user(db, uid, username, role="user"):
    """构造并持久化用户

    Args:
        db: 数据库会话
        uid: 用户ID
        username: 用户名
        role: 角色(admin/user)

    Returns:
        User: 已持久化的用户
    """
    user = User(id=uid, username=username, password="x", role=role, status=1, email=f"{username}@t.com")
    db.add(user)
    db.commit()
    return user


def _make_project(db, pid, owner_user_id, name="proj"):
    """构造并持久化项目

    Args:
        db: 数据库会话
        pid: 项目ID
        owner_user_id: 拥有者用户ID
        name: 项目名

    Returns:
        Project: 已持久化的项目
    """
    project = Project(id=pid, user_id=owner_user_id, project_name=f"{name}_{pid}", status="active")
    db.add(project)
    db.commit()
    return project


# ============ get_visible_project_ids 测试 ============


class TestGetVisibleProjectIds:
    """get_visible_project_ids 数据隔离测试"""

    def test_admin_sees_all_projects(self, db):
        """admin 视角应看到全部非删除项目,scope='global'"""
        admin = _make_user(db, 1, "admin", role="admin")
        _make_project(db, 101, owner_user_id=2, name="p1")
        _make_project(db, 102, owner_user_id=3, name="p2")
        # 删除的项目不应可见
        deleted_proj = Project(id=103, user_id=2, project_name="deleted", status="deleted")
        db.add(deleted_proj)
        db.commit()

        visible_ids, scope = get_visible_project_ids(db, admin)
        assert scope == "global"
        assert 101 in visible_ids
        assert 102 in visible_ids
        assert 103 not in visible_ids  # 已删除

    def test_normal_user_sees_owner_and_member_projects(self, db):
        """普通用户视角应看到 owner 项目 ∪ member 项目,scope='self'"""
        user = _make_user(db, 10, "user1")
        other = _make_user(db, 11, "user2")

        # user 是 201 的 owner
        _make_project(db, 201, owner_user_id=10, name="own")
        # user 不是 202 的 owner,但被加入为 member
        _make_project(db, 202, owner_user_id=11, name="other")
        db.add(ProjectMember(project_id=202, user_id=10, role_in_project="reviewer"))
        # user 完全无关的项目
        _make_project(db, 203, owner_user_id=11, name="no_access")
        db.commit()

        visible_ids, scope = get_visible_project_ids(db, user)
        assert scope == "self"
        assert 201 in visible_ids  # owner
        assert 202 in visible_ids  # member
        assert 203 not in visible_ids  # 无关

    def test_none_user_treated_as_admin(self, db):
        """user=None 应视为管理员视角"""
        _make_project(db, 301, owner_user_id=1, name="p")
        visible_ids, scope = get_visible_project_ids(db, None)
        assert scope == "global"
        assert 301 in visible_ids


# ============ is_project_member 测试 ============


class TestIsProjectMember:
    """is_project_member 角色判断测试"""

    def test_admin_has_access(self, db):
        """admin 对任何项目都有访问权,返回 (True, 'admin')"""
        admin = _make_user(db, 1, "admin", role="admin")
        _make_project(db, 401, owner_user_id=2)
        can, role = is_project_member(db, 401, admin)
        assert can is True
        assert role == "admin"

    def test_owner_has_access(self, db):
        """owner 对自己的项目有访问权,返回 (True, 'owner')"""
        owner = _make_user(db, 2, "owner")
        _make_project(db, 402, owner_user_id=2)
        can, role = is_project_member(db, 402, owner)
        assert can is True
        assert role == "owner"

    def test_reviewer_member_has_access(self, db):
        """reviewer 对被加入的项目有访问权,返回 (True, 'reviewer')"""
        owner = _make_user(db, 3, "owner3")
        reviewer = _make_user(db, 4, "reviewer4")
        _make_project(db, 403, owner_user_id=3)
        db.add(ProjectMember(project_id=403, user_id=4, role_in_project="reviewer"))
        db.commit()
        can, role = is_project_member(db, 403, reviewer)
        assert can is True
        assert role == "reviewer"

    def test_non_member_no_access(self, db):
        """非成员对项目无访问权,返回 (False, '')"""
        owner = _make_user(db, 5, "owner5")
        stranger = _make_user(db, 6, "stranger")
        _make_project(db, 404, owner_user_id=5)
        can, role = is_project_member(db, 404, stranger)
        assert can is False
        assert role == ""

    def test_deleted_project_no_access(self, db):
        """已删除项目对所有人不可访问"""
        owner = _make_user(db, 7, "owner7")
        deleted = Project(id=405, user_id=7, project_name="deleted", status="deleted")
        db.add(deleted)
        db.commit()
        can, role = is_project_member(db, 405, owner)
        assert can is False
        assert role == ""


# ============ require_project_access 测试 ============


class TestRequireProjectAccess:
    """require_project_access 权限校验测试"""

    def test_admin_passes_write(self, db):
        """admin 通过写权限校验,返回 'admin'"""
        admin = _make_user(db, 1, "admin", role="admin")
        _make_project(db, 501, owner_user_id=2)
        role = require_project_access(db, 501, admin, need_write=True)
        assert role == "admin"

    def test_owner_passes_write(self, db):
        """owner 通过写权限校验,返回 'owner'"""
        owner = _make_user(db, 2, "owner")
        _make_project(db, 502, owner_user_id=2)
        role = require_project_access(db, 502, owner, need_write=True)
        assert role == "owner"

    def test_reviewer_fails_write(self, db):
        """reviewer 不能通过写权限校验,抛 ForbiddenError"""
        owner = _make_user(db, 3, "owner")
        reviewer = _make_user(db, 4, "reviewer")
        _make_project(db, 503, owner_user_id=3)
        db.add(ProjectMember(project_id=503, user_id=4, role_in_project="reviewer"))
        db.commit()
        with pytest.raises(ForbiddenError, match="需要项目拥有者权限"):
            require_project_access(db, 503, reviewer, need_write=True)

    def test_reviewer_passes_read(self, db):
        """reviewer 通过读权限校验,返回 'reviewer'"""
        owner = _make_user(db, 5, "owner")
        reviewer = _make_user(db, 6, "reviewer")
        _make_project(db, 504, owner_user_id=5)
        db.add(ProjectMember(project_id=504, user_id=6, role_in_project="reviewer"))
        db.commit()
        role = require_project_access(db, 504, reviewer, need_write=False)
        assert role == "reviewer"

    def test_nonexistent_project_raises_not_found(self, db):
        """不存在的项目抛 NotFoundError(防枚举)"""
        user = _make_user(db, 7, "user")
        with pytest.raises(NotFoundError):
            require_project_access(db, 999, user, need_write=False)

    def test_non_member_raises_not_found(self, db):
        """非成员访问项目抛 NotFoundError(防枚举,不暴露项目存在性)"""
        owner = _make_user(db, 8, "owner")
        stranger = _make_user(db, 9, "stranger")
        _make_project(db, 505, owner_user_id=8)
        with pytest.raises(NotFoundError):
            require_project_access(db, 505, stranger, need_write=False)


# ============ add_member / remove_member / update_member_role 测试 ============


class TestMemberCRUD:
    """成员关系 CRUD 测试"""

    def test_add_member_success(self, db):
        """owner 成功添加 reviewer 成员"""
        owner = _make_user(db, 1, "owner")
        target = _make_user(db, 2, "target")
        _make_project(db, 601, owner_user_id=1)
        member = add_member(db, project_id=601, user_id=2, role="reviewer", operator=owner)
        assert member.user_id == 2
        assert member.role_in_project == "reviewer"

    def test_add_member_duplicate_raises(self, db):
        """重复添加成员抛 BadRequestError"""
        owner = _make_user(db, 3, "owner")
        target = _make_user(db, 4, "target")
        _make_project(db, 602, owner_user_id=3)
        add_member(db, project_id=602, user_id=4, role="reviewer", operator=owner)
        with pytest.raises(BadRequestError, match="已是项目成员"):
            add_member(db, project_id=602, user_id=4, role="reviewer", operator=owner)

    def test_add_member_by_reviewer_forbidden(self, db):
        """reviewer 不能添加成员"""
        owner = _make_user(db, 5, "owner")
        reviewer = _make_user(db, 6, "reviewer")
        target = _make_user(db, 7, "target")
        _make_project(db, 603, owner_user_id=5)
        add_member(db, project_id=603, user_id=6, role="reviewer", operator=owner)
        with pytest.raises(ForbiddenError):
            add_member(db, project_id=603, user_id=7, role="reviewer", operator=reviewer)

    def test_remove_member_success(self, db):
        """owner 成功移除 reviewer 成员"""
        owner = _make_user(db, 8, "owner")
        target = _make_user(db, 9, "target")
        _make_project(db, 604, owner_user_id=8)
        add_member(db, project_id=604, user_id=9, role="reviewer", operator=owner)
        assert remove_member(db, project_id=604, user_id=9, operator=owner) is True

    def test_remove_owner_forbidden(self, db):
        """不能移除项目 owner"""
        owner = _make_user(db, 10, "owner")
        _make_project(db, 605, owner_user_id=10)
        # 先 ensure_owner_member 写入 owner 记录
        ensure_owner_member(db, 605, 10)
        with pytest.raises(BadRequestError, match="不能移除项目拥有者"):
            remove_member(db, project_id=605, user_id=10, operator=owner)

    def test_update_member_role(self, db):
        """owner 更新成员角色"""
        owner = _make_user(db, 11, "owner")
        target = _make_user(db, 12, "target")
        _make_project(db, 606, owner_user_id=11)
        add_member(db, project_id=606, user_id=12, role="reviewer", operator=owner)
        updated = update_member_role(db, project_id=606, user_id=12, new_role="owner", operator=owner)
        assert updated.role_in_project == "owner"

    def test_list_members(self, db):
        """list_members 返回成员列表含用户基本信息"""
        owner = _make_user(db, 13, "owner13", role="user")
        reviewer = _make_user(db, 14, "reviewer14", role="user")
        _make_project(db, 607, owner_user_id=13)
        ensure_owner_member(db, 607, 13)
        add_member(db, project_id=607, user_id=14, role="reviewer", operator=owner)
        members = list_members(db, 607)
        assert len(members) == 2
        user_ids = [m["user_id"] for m in members]
        assert 13 in user_ids
        assert 14 in user_ids


# ============ ensure_owner_member 测试 ============


class TestEnsureOwnerMember:
    """ensure_owner_member 幂等性测试"""

    def test_creates_owner_record(self, db):
        """首次调用应创建 owner 成员记录"""
        _make_project(db, 701, owner_user_id=1)
        _make_user(db, 1, "u1")
        ensure_owner_member(db, 701, 1)
        members = list_members(db, 701)
        assert len(members) == 1
        assert members[0]["user_id"] == 1
        assert members[0]["role_in_project"] == "owner"

    def test_idempotent(self, db):
        """多次调用不应创建重复记录"""
        _make_project(db, 702, owner_user_id=2)
        _make_user(db, 2, "u2")
        ensure_owner_member(db, 702, 2)
        ensure_owner_member(db, 702, 2)
        ensure_owner_member(db, 702, 2)
        members = list_members(db, 702)
        assert len(members) == 1

    def test_upgrades_reviewer_to_owner(self, db):
        """已存在的 reviewer 记录应被升级为 owner"""
        owner = _make_user(db, 3, "u3")
        _make_project(db, 703, owner_user_id=3)
        # 先以 reviewer 身份加入(模拟异常状态)
        db.add(ProjectMember(project_id=703, user_id=3, role_in_project="reviewer"))
        db.commit()
        ensure_owner_member(db, 703, 3)
        members = list_members(db, 703)
        assert len(members) == 1
        assert members[0]["role_in_project"] == "owner"
