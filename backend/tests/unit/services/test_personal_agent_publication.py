"""Personal publication identity, version binding, privacy and charged execution."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models.billing_ledger import BillingReservation
from app.models.personal_agent import PersonalAgentPublication
from app.models.rbac import Role, UserRole
from app.models.system_config import SystemConfig
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service as catalog
from app.services import personal_agent_execution_service as execution
from app.services import personal_agent_publication_service as publication
from app.services import personal_agent_service
from app.utils.api_resolver import ApiConfig


@pytest.fixture
def context(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'publication.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        db.add_all(
            [
                User(id=1, username="admin", role="super_admin", password="x", status=1),
                User(id=2, username="author", role="user", password="x", status=1),
                User(id=3, username="caller", role="user", password="x", status=1),
            ]
        )
        db.add_all([Role(id=1, code="super_admin", name="超级管理员"), Role(id=2, code="user", name="普通用户")])
        db.add_all([UserRole(user_id=1, role_id=1), UserRole(user_id=2, role_id=2), UserRole(user_id=3, role_id=2)])
        db.add(SystemConfig(config_key="llm_provider", config_value='{"active":false}'))
        catalog.seed_test_baseline(db)
        wallet = ledger.get_or_create_wallet(db, "user", 3)
        ledger.grant(db, wallet_id=wallet.id, grant_key="caller-credit", kind="purchase", amount_milli=2_000_000)
    with factory() as db:
        agent_id = personal_agent_service.create(db, 2, "原始名称", "私有提示词", "create").id
    monkeypatch.setattr(execution.settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(
        execution,
        "resolve_api_config",
        lambda _db: ApiConfig(
            api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-flash", source="system"
        ),
    )
    yield factory, agent_id, monkeypatch
    engine.dispose()


def _submit_and_approve(factory, agent_id):
    with factory() as db:
        row = publication.submit(db, owner_id=2, agent_id=agent_id, expected_version=1, request_key="submit")
        publication_id = row.id
    with factory() as db:
        publication.decide(db, actor_id=1, token_version=0, publication_id=publication_id, approve=True, reason="通过")
    return publication_id


def test_database_allows_only_one_active_publication_per_version(context):
    factory, agent_id, _ = context
    with factory() as db:
        first = publication.submit(db, owner_id=2, agent_id=agent_id, expected_version=1, request_key="first")
        first_id = first.id
    with factory() as db:
        original = db.query(PersonalAgentPublication).filter_by(id=first_id).one()
        duplicate = PersonalAgentPublication(
            owner_id=original.owner_id, agent_id=original.agent_id, version_id=original.version_id,
            version_number=original.version_number, title=original.title, prompt=original.prompt,
            content_hash=original.content_hash, request_key="other", status="pending",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    with factory() as db:
        publication.withdraw(db, owner_id=2, publication_id=first_id)
    with factory() as db:
        assert publication.submit(db, owner_id=2, agent_id=agent_id, expected_version=1,
                                  request_key="other").status == "pending"


def test_request_approval_catalog_and_snapshot(context):
    factory, agent_id, _ = context
    with factory() as db:
        row = publication.submit(db, owner_id=2, agent_id=agent_id, expected_version=1, request_key="submit")
        publication_id = row.id
        assert publication.catalog(db) == []
    with factory() as db:
        with pytest.raises(ConflictError):
            publication.submit(db, owner_id=2, agent_id=agent_id, expected_version=1, request_key="duplicate")
    with factory() as db:
        with pytest.raises(ForbiddenError):
            publication.decide(
                db, actor_id=2, token_version=0, publication_id=publication_id, approve=True, reason="自审"
            )
    with factory() as db:
        personal_agent_service.revise(db, 2, agent_id, "新名称", "新提示词", 1, "revise")
    with factory() as db:
        retry = publication.submit(db, owner_id=2, agent_id=agent_id, expected_version=1, request_key="submit")
        assert retry.id == publication_id
    with factory() as db:
        publication.decide(db, actor_id=1, token_version=0, publication_id=publication_id, approve=True, reason="通过")
    with factory() as db:
        row = publication.catalog(db)[0]
        assert row["title"] == "原始名称" and "prompt" not in row and "owner_id" not in row


def test_global_call_uses_caller_wallet_and_one_attempt(context):
    factory, agent_id, monkeypatch = context
    publication_id = _submit_and_approve(factory, agent_id)
    calls = []

    def send(_config, payload):
        calls.append(payload)
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": "结果"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        }

    monkeypatch.setattr(execution, "_send_once", send)
    result = execution.execute_publication(
        factory, owner_id=3, publication_id=publication_id, request_key="public", input_text="你好"
    )
    assert result["status"] == "succeeded" and result["output"] == "结果"
    again = execution.execute_publication(
        factory, owner_id=3, publication_id=publication_id, request_key="public", input_text="你好"
    )
    assert again["id"] == result["id"] and len(calls) == 1
    assert calls[0]["messages"][0]["content"] == "私有提示词"
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.user_id == 3 and reservation.status == "settled"
    with pytest.raises(NotFoundError):
        execution.get_run(factory, owner_id=2, run_id=result["id"])


def test_publication_revoked_during_call_settles_but_suppresses_result(context):
    factory, agent_id, monkeypatch = context
    publication_id = _submit_and_approve(factory, agent_id)

    def send(*_args):
        with factory() as db:
            publication.revoke(db, actor_id=1, token_version=0, publication_id=publication_id, reason="下架")
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": "迟到结果"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        }

    monkeypatch.setattr(execution, "_send_once", send)
    result = execution.execute_publication(
        factory, owner_id=3, publication_id=publication_id, request_key="public", input_text="你好"
    )
    assert result["status"] == "result_unavailable" and result["output"] is None
    with factory() as db:
        assert db.query(BillingReservation).one().status == "settled"
        assert publication.catalog(db) == []
    with pytest.raises((ConflictError, NotFoundError)):
        execution.execute_publication(
            factory, owner_id=3, publication_id=publication_id, request_key="new", input_text="你好"
        )


def test_same_key_cannot_cross_private_and_public_sources(context):
    factory, agent_id, monkeypatch = context
    public_id = _submit_and_approve(factory, agent_id)
    with factory() as db:
        personal_id = personal_agent_service.create(db, 3, "私有助手", "私有指令", "mine").id
    calls = []
    def send(*_args):
        calls.append(1)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "完成"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    monkeypatch.setattr(execution, "_send_once", send)
    execution.execute(factory, owner_id=3, agent_id=personal_id, version_number=1,
                      request_key="collision", input_text="内容")
    with pytest.raises(ConflictError):
        execution.execute_publication(factory, owner_id=3, publication_id=public_id,
                                      request_key="collision", input_text="内容")
    execution.execute_publication(factory, owner_id=3, publication_id=public_id,
                                  request_key="collision-reverse", input_text="内容")
    with pytest.raises(ConflictError):
        execution.execute(factory, owner_id=3, agent_id=personal_id, version_number=1,
                          request_key="collision-reverse", input_text="内容")
    assert len(calls) == 2


def test_revoked_session_cannot_submit_or_withdraw(context):
    factory, agent_id, _ = context
    with factory() as db:
        with pytest.raises(ForbiddenError):
            publication.submit(db, owner_id=2, agent_id=agent_id, expected_version=1,
                               request_key="stale", token_version=1)
    with factory() as db:
        row = publication.submit(db, owner_id=2, agent_id=agent_id, expected_version=1, request_key="ok")
        publication_id = row.id
    with factory() as db:
        with pytest.raises(ForbiddenError):
            publication.withdraw(db, owner_id=2, publication_id=publication_id, token_version=1)


def test_withdraw_reject_and_snapshot_tampering_never_publish(context):
    factory, agent_id, _ = context
    with factory() as db:
        first = publication.submit(db, owner_id=2, agent_id=agent_id, expected_version=1, request_key="withdraw")
        first_id = first.id
    with factory() as db:
        with pytest.raises(NotFoundError):
            publication.withdraw(db, owner_id=3, publication_id=first_id)
    with factory() as db:
        publication.withdraw(db, owner_id=2, publication_id=first_id)
    with factory() as db:
        with pytest.raises(ConflictError):
            publication.decide(db, actor_id=1, token_version=0, publication_id=first_id, approve=True, reason="x")
    with factory() as db:
        second_id = publication.submit(db, owner_id=2, agent_id=agent_id,
                                       expected_version=1, request_key="reject").id
    with factory() as db:
        publication.decide(db, actor_id=1, token_version=0, publication_id=second_id, approve=False, reason="x")
    with factory.begin() as db:
        third_id = publication.submit(db, owner_id=2, agent_id=agent_id,
                                      expected_version=1, request_key="tamper").id
    with factory.begin() as db:
        db.query(PersonalAgentPublication).filter_by(id=third_id).one().prompt = "伪造内容"
    with factory() as db:
        with pytest.raises(ConflictError):
            publication.decide(db, actor_id=1, token_version=0, publication_id=third_id, approve=True, reason="x")
    with factory() as db:
        assert publication.catalog(db) == []


def test_approved_historical_result_survives_later_revocation(context):
    factory, agent_id, monkeypatch = context
    pub_id = _submit_and_approve(factory, agent_id)
    monkeypatch.setattr(execution, "_send_once", lambda *_: {
        "choices": [{"finish_reason": "stop", "message": {"content": "历史结果"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    })
    result = execution.execute_publication(factory, owner_id=3, publication_id=pub_id,
                                           request_key="historical", input_text="你好")
    with factory() as db:
        publication.revoke(db, actor_id=1, token_version=0, publication_id=pub_id, reason="下架")
    with factory.begin() as db:
        db.get(User, 2).status = 0
    recovered = execution.get_publication_run(factory, owner_id=3, publication_id=pub_id, run_id=result["id"])
    assert recovered["output"] == "历史结果" and recovered["actual_milli"] is not None
    by_key = execution.get_publication_run(factory, owner_id=3, publication_id=pub_id, request_key="historical")
    assert by_key["id"] == result["id"] and "prompt" not in by_key and "owner_id" not in by_key


def test_real_api_approval_and_public_call_are_owner_scoped(context):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from app.api.v1.personal_agent_publications import router
    from app.core.database import get_db
    from app.core.exceptions import AppError
    from app.core.security import create_access_token

    factory, agent_id, monkeypatch = context
    monkeypatch.setattr(
        execution,
        "_send_once",
        lambda *_: {
            "choices": [{"finish_reason": "stop", "message": {"content": "结果"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        },
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")

    @app.exception_handler(AppError)
    async def error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    author = {"Authorization": "Bearer " + create_access_token(2, "user", 0)}
    caller = {"Authorization": "Bearer " + create_access_token(3, "user", 0)}
    admin = {"Authorization": "Bearer " + create_access_token(1, "super_admin", 0)}
    with TestClient(app) as client:
        submitted = client.post(
            f"/api/me/agents/{agent_id}/publication-requests",
            headers=author,
            json={"expected_version": 1, "request_key": "submit"},
        )
        assert submitted.status_code == 200
        pub_id = submitted.json()["data"]["id"]
        assert client.get("/api/personal-agent-catalog", headers=caller).json()["data"] == []
        assert (
            client.post(
                f"/api/admin/personal-agent-publications/{pub_id}/decision",
                headers=author,
                json={"approve": True, "reason": "x"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/admin/personal-agent-publications/{pub_id}/decision",
                headers=admin,
                json={"approve": True, "reason": "ok"},
            ).status_code
            == 200
        )
        public = client.get("/api/personal-agent-catalog", headers=caller).json()["data"]
        assert len(public) == 1 and "prompt" not in public[0] and "owner_id" not in public[0]
        response = client.post(
            f"/api/personal-agent-catalog/{pub_id}/runs",
            headers=caller,
            json={"request_key": "call", "input_text": "你好"},
        )
        assert response.status_code == 200 and response.json()["data"]["output"] == "结果"
        result = response.json()["data"]
        assert result["agent_id"] is None and result["publication_id"] == pub_id
        assert (
            client.get(f"/api/personal-agent-catalog/{pub_id}/runs/{result['id']}", headers=author).status_code == 404
        )
