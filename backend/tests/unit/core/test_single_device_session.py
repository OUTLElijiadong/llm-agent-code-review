"""单设备会话回归测试。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.dependencies import authenticate_access_token
from app.core.exceptions import AuthError
from app.core.security import decode_token
from app.models.user import User
from app.services import auth_service


def _user(db, *, token_version: int = 0) -> User:
    user = User(
        username="single-device-user",
        password="stored-hash",
        role="user",
        status=1,
        token_version=token_version,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_new_login_invalidates_previous_token_and_logout_invalidates_latest(db, monkeypatch) -> None:
    user = _user(db)
    monkeypatch.setattr(auth_service, "verify_password", lambda *_args: True)

    first_token, _ = auth_service.login(db, user.username, "password", ip="192.0.2.1")
    second_token, _ = auth_service.login(db, user.username, "password", ip="192.0.2.2")

    with pytest.raises(AuthError) as stale:
        authenticate_access_token(first_token, db)
    assert stale.value.code == 40102
    assert "另一台设备" in stale.value.message

    current = authenticate_access_token(second_token, db)
    assert current.id == user.id
    assert current.last_login_ip == "192.0.2.2"

    auth_service.logout(db, current)
    with pytest.raises(AuthError) as logged_out:
        authenticate_access_token(second_token, db)
    assert logged_out.value.code == 40102


def test_failed_login_does_not_revoke_current_session(db, monkeypatch) -> None:
    user = _user(db, token_version=7)
    monkeypatch.setattr(auth_service, "verify_password", lambda *_args: False)

    with pytest.raises(AuthError):
        auth_service.login(db, user.username, "wrong-password")

    db.expire_all()
    assert db.get(User, user.id).token_version == 7


def test_token_signing_failure_rolls_back_session_version(db, monkeypatch) -> None:
    user = _user(db, token_version=9)
    monkeypatch.setattr(auth_service, "verify_password", lambda *_args: True)
    monkeypatch.setattr(
        auth_service,
        "create_access_token",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("signing failed")),
    )

    with pytest.raises(RuntimeError, match="signing failed"):
        auth_service.login(db, user.username, "password", ip="192.0.2.3")

    db.expire_all()
    persisted = db.get(User, user.id)
    assert persisted.token_version == 9
    assert persisted.last_login is None
    assert persisted.last_login_ip is None


def test_concurrent_sqlite_logins_issue_distinct_cas_versions(tmp_path, monkeypatch) -> None:
    """SQLite 并发登录必须串行化版本，仅最后签发的 token 有效。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent-login.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    setup_db = session_factory()
    try:
        user = User(
            username="concurrent-login-user",
            password="hash",
            role="user",
            status=1,
            token_version=4,
        )
        setup_db.add(user)
        setup_db.commit()
        user_id = user.id
    finally:
        setup_db.close()

    first_verifications = threading.Barrier(2)
    verification_lock = threading.Lock()
    verification_count = 0

    def synchronized_verify(_raw: str, _stored: str) -> bool:
        nonlocal verification_count
        with verification_lock:
            verification_count += 1
            call_number = verification_count
        if call_number <= 2:
            first_verifications.wait(timeout=5)
        return True

    monkeypatch.setattr(auth_service, "verify_password", synchronized_verify)

    def login_from(ip: str) -> tuple[str, str]:
        worker_db = session_factory()
        try:
            token, _ = auth_service.login(
                worker_db,
                "concurrent-login-user",
                "password",
                ip=ip,
            )
            return token, ip
        finally:
            worker_db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(login_from, ["192.0.2.10", "192.0.2.11"]))

        by_version = {
            int(decode_token(token)["ver"]): (token, ip)
            for token, ip in results
        }
        assert sorted(by_version) == [5, 6]

        check_db = session_factory()
        try:
            persisted = check_db.get(User, user_id)
            assert persisted is not None
            assert persisted.token_version == 6
            assert persisted.last_login_ip == by_version[6][1]
            with pytest.raises(AuthError) as stale:
                authenticate_access_token(by_version[5][0], check_db)
            assert stale.value.code == 40102
            assert authenticate_access_token(by_version[6][0], check_db).id == user_id
        finally:
            check_db.close()
    finally:
        engine.dispose()


def test_stale_change_password_cannot_revoke_new_login(tmp_path, monkeypatch) -> None:
    """旧设备改密请求若晚于新登录拿锁，必须拒绝且不得误伤新会话。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'single-device-race.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    stale_db = session_factory()
    login_db = session_factory()
    try:
        user = User(
            username="password-race-user",
            password="old-hash",
            role="user",
            status=1,
            token_version=4,
        )
        stale_db.add(user)
        stale_db.commit()
        stale_user = stale_db.get(User, user.id)
        assert stale_user is not None and stale_user.token_version == 4

        monkeypatch.setattr(auth_service, "verify_password", lambda *_args: True)
        monkeypatch.setattr(auth_service, "hash_password", lambda raw: f"hash:{raw}")
        _new_token, _ = auth_service.login(login_db, user.username, "old-password")

        with pytest.raises(AuthError) as stale:
            auth_service.change_password(stale_db, stale_user, "old-password", "new-password")
        assert stale.value.code == 40102
        stale_db.expire_all()
        persisted = stale_db.get(User, user.id)
        assert persisted is not None
        assert persisted.token_version == 5
        assert persisted.password == "old-hash"
    finally:
        stale_db.close()
        login_db.close()
        engine.dispose()


def test_stale_logout_cannot_revoke_new_login(tmp_path, monkeypatch) -> None:
    """旧设备退出请求若晚于新登录拿锁，必须拒绝且保留新会话。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'logout-race.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    stale_db = session_factory()
    login_db = session_factory()
    try:
        user = User(
            username="logout-race-user",
            password="hash",
            role="user",
            status=1,
            token_version=2,
        )
        stale_db.add(user)
        stale_db.commit()
        stale_user = stale_db.get(User, user.id)
        assert stale_user is not None and stale_user.token_version == 2

        monkeypatch.setattr(auth_service, "verify_password", lambda *_args: True)
        new_token, _ = auth_service.login(login_db, user.username, "password")

        with pytest.raises(AuthError) as stale:
            auth_service.logout(stale_db, stale_user)
        assert stale.value.code == 40102
        stale_db.expire_all()
        persisted = stale_db.get(User, user.id)
        assert persisted is not None and persisted.token_version == 3
        assert authenticate_access_token(new_token, stale_db).id == user.id
    finally:
        stale_db.close()
        login_db.close()
        engine.dispose()


def test_role_assignment_refreshes_stale_identity_before_revoking(tmp_path) -> None:
    """管理员改角色不得覆盖已由并发登录提升的会话版本。"""

    from app.models.rbac import Role
    from app.services import rbac_service

    engine = create_engine(
        f"sqlite:///{tmp_path / 'role-race.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    stale_db = session_factory()
    login_db = session_factory()
    try:
        user = User(
            username="role-race-user",
            password="hash",
            role="user",
            status=1,
            token_version=8,
        )
        reviewer = Role(name="评审员", code="reviewer", status="active", is_builtin=1)
        stale_db.add_all([user, reviewer])
        stale_db.commit()
        stale_user = stale_db.get(User, user.id)
        assert stale_user is not None and stale_user.token_version == 8

        concurrent = login_db.get(User, user.id)
        assert concurrent is not None
        concurrent.token_version = 9
        login_db.commit()

        rbac_service.assign_roles_to_user(stale_db, user.id, [reviewer.id])
        stale_db.expire_all()
        persisted = stale_db.get(User, user.id)
        assert persisted is not None
        assert persisted.token_version == 10
        assert persisted.role == "reviewer"
    finally:
        stale_db.close()
        login_db.close()
        engine.dispose()
