"""MySQL RR current project authorization for source writes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.enterprise_project import EnterpriseProject, EnterpriseProjectGrant
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.user import User
from app.services.code_file_service import _check_project_access

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


class UnrelatedBindingGapWait(Exception):
    """An unrelated project binding waited on the missing-key lock."""


def _make(db, *, enterprise=False, member_role="member", group_role="member", grant_role=None):
    suffix = uuid4().hex[:15]
    actor = User(username=f"source_actor_{suffix}", password="x", role="user", status=1)
    owner = User(username=f"source_owner_{suffix}", password="x", role="user", status=1)
    db.add_all([actor, owner])
    db.flush()
    project = Project(user_id=owner.id if enterprise else actor.id, project_name=f"source_{suffix}", status="active")
    db.add(project)
    db.flush()
    data = {"actor": actor.id, "owner": owner.id, "project": project.id}
    if enterprise:
        company = Enterprise(
            name=f"company_{suffix}",
            owner_id=owner.id,
            seat_limit=10,
            status="active",
            billing_suspended=False,
            paid_order_id=f"pay_{suffix}",
        )
        db.add(company)
        db.flush()
        group = EnterpriseGroup(enterprise_id=company.id, name=f"team_{suffix}", status="active")
        db.add(group)
        db.flush()
        db.add_all(
            [
                EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"),
                EnterpriseMember(enterprise_id=company.id, user_id=actor.id, role=member_role, status="active"),
                EnterpriseGroupMember(
                    enterprise_id=company.id, group_id=group.id, user_id=actor.id, role=group_role, status="active"
                ),
                EnterpriseProject(enterprise_id=company.id, group_id=group.id, project_id=project.id, status="active"),
            ]
        )
        if grant_role:
            db.add(
                EnterpriseProjectGrant(
                    enterprise_id=company.id, project_id=project.id, user_id=actor.id, role=grant_role, status="active"
                )
            )
        data.update(company=company.id, group=group.id)
    return data


@pytest.mark.parametrize(
    "change", ["owner", "project_hidden", "archive", "grant", "group_role", "group_status", "binding_status", "member"]
)
def test_source_write_rechecks_committed_changes_after_rr_snapshot(fresh_chain, change):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(
            db,
            enterprise=change in {"grant", "group_role", "group_status", "binding_status", "member"},
            group_role="leader" if change == "group_role" else "member",
            grant_role="editor" if change == "grant" else None,
        )
    with factory() as stale:
        actor = stale.get(User, data["actor"])
        # Establish the old InnoDB RR snapshot before the competing commit.
        stale.get(Project, data["project"])
        if change in {"grant", "group_role", "group_status", "binding_status", "member"}:
            stale.query(EnterpriseProject).filter_by(project_id=data["project"]).first()
            stale.query(EnterpriseMember).filter_by(enterprise_id=data["company"], user_id=actor.id).first()
        with factory.begin() as update:
            if change == "owner":
                update.get(Project, data["project"]).user_id = data["owner"]
            elif change == "project_hidden":
                update.get(Project, data["project"]).status = "quarantined"
            elif change == "archive":
                update.add(
                    ProjectSourceArchive(
                        project_id=data["project"],
                        owner_id=data["actor"],
                        original_filename="source.zip",
                        media_type="application/zip",
                        archive_sha256="a" * 64,
                        compressed_size=1,
                        expanded_size=1,
                        file_count=1,
                        max_member_size=1,
                        max_compression_ratio=1,
                        storage_status="active",
                        malware_status="clean",
                        audit_status="not_started",
                        threat_count=0,
                        scan_summary_json="{}",
                        archive_blob=b"x",
                    )
                )
            elif change == "grant":
                update.query(EnterpriseProjectGrant).filter_by(
                    project_id=data["project"], user_id=actor.id
                ).one().status = "inactive"
            elif change == "group_role":
                update.query(EnterpriseGroupMember).filter_by(
                    group_id=data["group"], user_id=actor.id
                ).one().role = "member"
            elif change == "group_status":
                update.get(EnterpriseGroup, data["group"]).status = "inactive"
            elif change == "binding_status":
                update.query(EnterpriseProject).filter_by(project_id=data["project"]).one().status = "inactive"
            else:
                update.query(EnterpriseMember).filter_by(
                    enterprise_id=data["company"], user_id=actor.id
                ).one().status = "removed"
        with pytest.raises((NotFoundError, ForbiddenError, ValidationError)):
            _check_project_access(stale, actor, data["project"])


def test_personal_admin_demotion_does_not_survive_old_snapshot(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db)
        db.get(Project, data["project"]).user_id = data["owner"]
        db.get(User, data["actor"]).role = "admin"
    with factory() as stale:
        actor = stale.get(User, data["actor"])
        stale.get(Project, data["project"])
        with factory.begin() as update:
            update.get(User, data["actor"]).role = "user"
        with pytest.raises(NotFoundError):
            _check_project_access(stale, actor, data["project"])


def test_new_enterprise_binding_cannot_fall_back_to_personal_owner(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db)
    with factory() as stale:
        actor = stale.get(User, data["actor"])
        stale.get(Project, data["project"])
        assert stale.query(EnterpriseProject).filter_by(project_id=data["project"]).first() is None
        with factory.begin() as update:
            company = Enterprise(
                name=f"new_company_{uuid4().hex}",
                owner_id=data["owner"],
                seat_limit=2,
                status="active",
                paid_order_id=f"new_pay_{uuid4().hex}",
            )
            update.add(company)
            update.flush()
            group = EnterpriseGroup(enterprise_id=company.id, name="new team", status="active")
            update.add(group)
            update.flush()
            update.add(
                EnterpriseProject(
                    enterprise_id=company.id, project_id=data["project"], group_id=group.id, status="active"
                )
            )
        with pytest.raises(NotFoundError):
            _check_project_access(stale, actor, data["project"])


def test_real_binding_then_grant_revocation_does_not_restore_personal_owner(fresh_chain):
    from app.services.enterprise_project_service import bind_owned_project, revoke_project_grant

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db)
        company = Enterprise(
            name=f"bind_{uuid4().hex}",
            owner_id=data["owner"],
            seat_limit=4,
            status="active",
            paid_order_id=f"pay_{uuid4().hex}",
        )
        db.add(company)
        db.flush()
        group = EnterpriseGroup(enterprise_id=company.id, name="bind-group", status="active")
        db.add(group)
        db.flush()
        db.add_all(
            [
                EnterpriseMember(enterprise_id=company.id, user_id=data["owner"], role="owner", status="active"),
                EnterpriseMember(enterprise_id=company.id, user_id=data["actor"], role="member", status="active"),
                EnterpriseGroupMember(
                    enterprise_id=company.id, group_id=group.id, user_id=data["actor"], role="member", status="active"
                ),
            ]
        )
        company_id, group_id = company.id, group.id
    with factory() as stale:
        actor = stale.get(User, data["actor"])
        assert stale.query(EnterpriseProject).filter_by(project_id=data["project"]).first() is None
        with factory() as change:
            bind_owned_project(change, company_id, data["project"], group_id=group_id, actor_id=data["actor"])
            revoke_project_grant(change, company_id, data["project"], data["actor"], actor_id=data["owner"])
        with pytest.raises(ForbiddenError):
            _check_project_access(stale, actor, data["project"])


def test_conflicting_company_lock_fails_fast_instead_of_deadlocking(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=True, grant_role="editor")
    with factory() as holder, factory() as writer:
        holder.query(Enterprise).filter_by(id=data["company"]).with_for_update().one()
        actor = writer.get(User, data["actor"])
        started = monotonic()
        with pytest.raises(ConflictError, match="权限正在变更"):
            _check_project_access(writer, actor, data["project"])
        assert monotonic() - started < 3
        writer.rollback()
        holder.rollback()


def test_write_authorized_first_serializes_real_project_grant_revocation(fresh_chain):
    from app.services.enterprise_project_service import revoke_project_grant

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=True, grant_role="editor")
    started, done = Event(), Event()

    def revoke():
        with factory() as db:
            started.set()
            revoke_project_grant(db, data["company"], data["project"], data["actor"], actor_id=data["owner"])
            done.set()

    with factory() as writer, ThreadPoolExecutor(max_workers=1) as pool:
        actor = writer.get(User, data["actor"])
        assert _check_project_access(writer, actor, data["project"]).id == data["project"]
        future = pool.submit(revoke)
        try:
            assert started.wait(3)
            assert not done.wait(0.3)
        finally:
            writer.commit()
        future.result(timeout=8)
    with factory() as after:
        with pytest.raises(ForbiddenError):
            _check_project_access(after, after.get(User, data["actor"]), data["project"])


def test_write_authorized_first_serializes_real_group_removal(fresh_chain):
    from app.services.enterprise_org_service import remove_group_member

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=True, group_role="leader")
    started, done = Event(), Event()

    def remove():
        with factory() as db:
            started.set()
            remove_group_member(db, data["company"], data["group"], data["owner"], data["actor"])
            db.commit()
            done.set()

    with factory() as writer, ThreadPoolExecutor(max_workers=1) as pool:
        actor = writer.get(User, data["actor"])
        _check_project_access(writer, actor, data["project"])
        future = pool.submit(remove)
        try:
            assert started.wait(3)
            assert not done.wait(0.3)
        finally:
            writer.commit()
        future.result(timeout=8)
    with factory() as after:
        with pytest.raises(NotFoundError):
            _check_project_access(after, after.get(User, data["actor"]), data["project"])


def test_distinct_members_can_write_different_projects_in_the_same_group(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=True, grant_role="editor")
        second = User(username=f"source_second_{uuid4().hex}", password="x", role="user", status=1)
        db.add(second)
        db.flush()
        project = Project(user_id=data["owner"], project_name=f"source_second_{uuid4().hex}", status="active")
        db.add(project)
        db.flush()
        db.add_all(
            [
                EnterpriseMember(enterprise_id=data["company"], user_id=second.id, role="member", status="active"),
                EnterpriseGroupMember(
                    enterprise_id=data["company"],
                    group_id=data["group"],
                    user_id=second.id,
                    role="member",
                    status="active",
                ),
                EnterpriseProject(
                    enterprise_id=data["company"], group_id=data["group"], project_id=project.id, status="active"
                ),
                EnterpriseProjectGrant(
                    enterprise_id=data["company"],
                    project_id=project.id,
                    user_id=second.id,
                    role="editor",
                    status="active",
                ),
            ]
        )
        second_id, second_project_id = second.id, project.id

    def second_write():
        with factory() as db:
            actor = db.get(User, second_id)
            return _check_project_access(db, actor, second_project_id).id

    with factory() as first, ThreadPoolExecutor(max_workers=1) as pool:
        _check_project_access(first, first.get(User, data["actor"]), data["project"])
        result = pool.submit(second_write)
        try:
            assert result.result(timeout=3) == second_project_id
        finally:
            first.rollback()


@pytest.mark.xfail(
    strict=True, raises=UnrelatedBindingGapWait, reason="RR 中个人项目缺少绑定的当前读可能阻塞无关项目的绑定"
)
def test_unrelated_project_binding_is_not_blocked_by_missing_binding_read(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db)
        other = Project(user_id=data["owner"], project_name=f"other_{uuid4().hex}", status="active")
        db.add(other)
        db.flush()
        other_id = other.id

    inserting = Event()

    def bind_other():
        with factory() as db:
            db.execute(text("SET innodb_lock_wait_timeout = 3"))
            db.add(EnterpriseProject(enterprise_id=999999, project_id=other_id, group_id=999999, status="active"))
            inserting.set()
            try:
                db.flush()
            except OperationalError as exc:
                if getattr(exc.orig, "args", (None,))[0] == 1205:
                    raise UnrelatedBindingGapWait("无关项目绑定在唯一索引间隙等待") from exc
                raise
            finally:
                db.rollback()

    with factory() as writer, ThreadPoolExecutor(max_workers=1) as pool:
        _check_project_access(writer, writer.get(User, data["actor"]), data["project"])
        future = pool.submit(bind_other)
        try:
            assert inserting.wait(2)
            # Confirm the specific index, not a slow fixture or unrelated lock.
            observed = ""
            deadline = monotonic() + 2
            while monotonic() < deadline:
                if future.done():
                    future.result()
                    return
                with fresh_chain.connect() as connection:
                    observed = connection.execute(text("SHOW ENGINE INNODB STATUS")).one()[2]
                if "uk_enterprise_project_project" in observed and "insert intention waiting" in observed:
                    break
            assert "uk_enterprise_project_project" in observed and "insert intention waiting" in observed
            future.result(timeout=5)
        finally:
            writer.rollback()
