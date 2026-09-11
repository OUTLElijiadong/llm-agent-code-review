"""用户头像与偏好询问:魔数校验/标识合法性/内联下发/首登标志。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.security import hash_password
from app.main import app
from app.models.user import User
from app.models.user_avatar import UserAvatar
from app.services import auth_service, profile_service

# 1x1 PNG
PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def avatar_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'avatar.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(id=901, username="avatar-user", password=hash_password("password123"),
                    nickname="头像君", role="user", status=1)
        db.add(user)
        db.commit()

        def override_db():
            yield db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: db.query(User).filter(User.id == 901).one()
        try:
            yield TestClient(app), db, factory, user
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_current_user, None)
    engine.dispose()


def test_builtin_avatar_roundtrip(avatar_env):
    client, db, _, user = avatar_env
    resp = client.put("/api/me/avatar", json={"avatar": "cat"})
    assert resp.status_code == 200 and resp.json()["data"]["avatar"] == "builtin:cat"
    assert db.query(User).filter(User.id == user.id).one().avatar == "builtin:cat"

    # 非法 key
    assert client.put("/api/me/avatar", json={"avatar": "../etc/passwd"}).status_code == 400


def test_upload_requires_valid_image(avatar_env):
    client, db, _, user = avatar_env
    # 未上传就选 upload → 拒绝
    assert client.put("/api/me/avatar", json={"avatar": "upload"}).status_code == 400

    # 伪 png(魔数错误)
    resp = client.post(
        "/api/me/avatar/image",
        files={"file": ("fake.png", b"not-a-png-at-all", "image/png")},
    )
    assert resp.status_code == 400

    # 真 png
    resp = client.post(
        "/api/me/avatar/image",
        files={"file": ("me.png", PNG_1PX, "image/png")},
    )
    assert resp.status_code == 200 and resp.json()["data"]["avatar"] == "upload"
    assert db.query(UserAvatar).one().mime == "image/png"

    # 下发(内联 + nosniff)
    resp2 = client.get("/api/users/901/avatar/image")
    assert resp2.status_code == 200
    assert resp2.headers["content-type"].startswith("image/png")
    assert resp2.headers["x-content-type-options"] == "nosniff"

    # 清除 → 404
    assert client.delete("/api/me/avatar").status_code == 200
    assert client.get("/api/users/901/avatar/image").status_code == 404


def test_preference_prompt_state(db):
    user = User(username="pref-user", password="x", role="user", status=1)
    db.add(user)
    db.commit()
    profile = profile_service.get_or_create(db, user.id)
    assert profile.preference_prompted == 0
    marked = profile_service.mark_preference_prompted(db, user.id, 1)
    assert marked.preference_prompted == 1 and marked.preference_prompted_at is not None
    with pytest.raises(Exception):
        profile_service.mark_preference_prompted(db, user.id, 9)


def test_first_login_flag(db):
    user = User(username="first-login-user", password=hash_password("password123"),
                role="user", status=1)
    db.add(user)
    db.commit()
    _, _, first = auth_service.login(db, "first-login-user", "password123")
    assert first is True
    _, _, second = auth_service.login(db, "first-login-user", "password123")
    assert second is False
