"""个性化输入、本人数据与工坊角色边界的独立回归。"""

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from app.core.exceptions import ForbiddenError
from app.models.user import User
from app.models.user_profile import UserProfile
from app.services import agent_studio_service, profile_service
from app.utils import image_utils


def test_avatar_header_only_is_not_decodable():
    with pytest.raises(ValueError):
        image_utils.validate_image_bytes(b"\x89PNG\r\n\x1a\n", max_dimension=4096)


def test_avatar_dimensions_and_decoding():
    stream = io.BytesIO()
    Image.new("RGB", (4097, 1)).save(stream, "PNG")
    with pytest.raises(ValueError):
        image_utils.validate_image_bytes(stream.getvalue(), max_dimension=4096)
    for format_, mime in [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")]:
        stream = io.BytesIO()
        Image.new("RGB", (2, 3)).save(stream, format_)
        assert image_utils.validate_image_bytes(stream.getvalue(), max_dimension=4096) == mime


def test_profile_read_does_not_create(db):
    result = profile_service.read_profile(db, 10)
    assert result["user_id"] == 10
    assert db.query(UserProfile).count() == 0


def test_profile_update_keeps_other_fields_and_marks_atomically(db):
    db.add(User(id=10, username="profile-resilience", password="x", role="user", status=1))
    db.commit()
    profile_service.update_profile(db, 10, {"hobbies": "自述兴趣", "goals": "学习测试"})
    profile = profile_service.update_profile(db, 10, {"tech_stack": "Python", "preference_prompted": 1})
    assert profile.hobbies == "自述兴趣"
    assert profile.preference_prompted == 1
    assert "自述兴趣" in profile_service.get_summary_text(db, 10)
    profile.derived_stats = "invalid-json"
    assert profile_service.to_dict(profile)["derived_stats"] == {}


def test_custom_asset_permission_is_not_reviewer(monkeypatch):
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *args: True)
    monkeypatch.setattr(agent_studio_service, "_is_admin", lambda *args: False)
    monkeypatch.setattr("app.services.rbac_service.get_user_roles", lambda *args: [])
    with pytest.raises(ForbiddenError):
        agent_studio_service._assert_reviewer(None, SimpleNamespace(id=1, role="user"))


def test_private_snapshot_is_local_and_user_scoped(db):
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_doc import KnowledgeDoc

    for user_id in (1, 2):
        db.add(User(id=user_id, username=f"private-{user_id}", password="x", role="user", status=1))
    db.commit()
    profile_service.update_profile(db, 1, {"hobbies": "本人兴趣"})
    profile_service.update_profile(db, 2, {"hobbies": "另一账号"})
    chunks = db.query(KnowledgeChunk).filter(KnowledgeChunk.user_id == 1).all()
    assert len(chunks) == 1 and "另一账号" not in chunks[0].content
    assert chunks[0].embedding is None and chunks[0].embed_model == "local:profile-context"
    doc = db.query(KnowledgeDoc).filter(KnowledgeDoc.user_id == 1).one()
    assert doc.source_type == "preference"
    profile_service.update_profile(db, 1, {"hobbies": "更新兴趣"})
    assert db.query(KnowledgeDoc).filter(KnowledgeDoc.user_id == 1).count() == 1


def test_snapshot_transaction_rolls_back_profile_on_failure(db, monkeypatch):
    db.add(User(id=1, username="atomic-profile", password="x", role="user", status=1))
    db.commit()
    profile_service.update_profile(db, 1, {"hobbies": "原有"})
    monkeypatch.setattr(
        profile_service, "_sync_private_snapshot", lambda *args: (_ for _ in ()).throw(RuntimeError("write failed"))
    )
    with pytest.raises(RuntimeError):
        profile_service.update_profile(db, 1, {"hobbies": "新值", "preference_prompted": 1})
    db.rollback()
    row = db.query(UserProfile).one()
    assert row.hobbies == "原有" and row.preference_prompted == 0


def test_profile_context_is_not_sent_to_bulk_embedding(db, monkeypatch):
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.services import embedding_service

    db.add(KnowledgeChunk(doc_id=1, user_id=1, seq=0, content="私有兴趣", embed_model="local:profile-context"))
    db.commit()
    monkeypatch.setattr(
        embedding_service, "embed_texts", lambda *args, **kwargs: pytest.fail("private profile sent remotely")
    )
    stats = {"kb_chunks": 0, "failed_batches": 0}
    embedding_service._reembed_model(db, KnowledgeChunk, "kb_chunks", stats, 10)
    assert stats["kb_chunks"] == 0


def test_data_images_reject_bad_base64_and_truncated_bytes():
    assert image_utils.decode_data_image_url("data:image/png;base64,!!!!", max_bytes=1024) is None
    assert image_utils.decode_data_image_url("data:image/png;base64,iVBORw0KGgo=", max_bytes=1024) is None


def test_every_studio_route_enforces_role_before_custom_permission(monkeypatch):
    import re

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from app.api.v1 import agent_studio
    from app.core.database import get_db
    from app.core.dependencies import get_current_user
    from app.core.exceptions import AppError

    app = FastAPI()
    app.include_router(agent_studio.router, prefix="/studio")
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, role="user")
    monkeypatch.setattr(agent_studio_service, "_is_admin", lambda *args: False)
    monkeypatch.setattr("app.services.rbac_service.get_user_roles", lambda *args: [])
    monkeypatch.setattr("app.core.rbac_dependency.check_permission", lambda *args: True)

    @app.exception_handler(AppError)
    async def denied(_request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.http_status)

    routes = agent_studio.router.routes
    assert len(routes) >= 15
    with TestClient(app) as client:
        for route in routes:
            path = "/studio" + re.sub(r"\{[^}]+\}", "1", route.path)
            for method in route.methods:
                response = client.request(method, path, json={})
                assert response.status_code == 403, (method, path, response.text)


def test_background_learning_is_throttled_and_respects_opt_out(db, monkeypatch):
    from contextlib import nullcontext

    from app.core import database

    db.add(User(id=1, username="background-profile", password="x", role="user", status=1))
    db.commit()
    profile = profile_service.update_profile(db, 1, {"hobbies": "本人的兴趣"})
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(db))
    calls = []

    def learn(_db, user_id):
        calls.append(user_id)
        profile.last_learned_at = profile_service._now()
        db.commit()

    monkeypatch.setattr(profile_service, "refresh_implicit", learn)
    profile_service.refresh_background(1)
    profile_service.refresh_background(1)
    assert calls == [1]
    profile.auto_learn = False
    profile.last_learned_at = None
    db.commit()
    profile_service.refresh_background(1)
    assert calls == [1]
