"""Personal model usage must never include another account's calls."""
from datetime import datetime, timezone

from app.api.v1.admin_overview import _daily_token_usage
from app.models.ai_call_log import AiCallLog


def test_personal_usage_filters_each_account_and_preserves_unknown_usage(db):
    now = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    db.add_all([
        AiCallLog(user_id=1, model_name="test", prompt_tokens=100, completion_tokens=20,
                  total_tokens=120, status="success", create_time=now),
        AiCallLog(user_id=1, model_name="test", status="failed", create_time=now),
        AiCallLog(user_id=2, model_name="test", prompt_tokens=900, completion_tokens=100,
                  total_tokens=1000, status="success", create_time=now),
    ])
    db.commit()

    first = _daily_token_usage(db, days=1, now=now, user_id=1)
    second = _daily_token_usage(db, days=1, now=now, user_id=2)
    assert (first["total_tokens"], first["known_calls"], first["unknown_calls"]) == (120, 1, 1)
    assert (second["total_tokens"], second["known_calls"], second["unknown_calls"]) == (1000, 1, 0)
    assert (first["input_tokens"], first["output_tokens"], first["component_unknown_calls"]) == (100, 20, 1)
    assert (second["input_tokens"], second["output_tokens"], second["component_unknown_calls"]) == (900, 100, 0)


def test_endpoint_uses_authenticated_user_not_caller_query():
    from fastapi import FastAPI, Header
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.api.v1.user_profile import router
    from app.core.database import Base, get_db
    from app.core.dependencies import get_current_user
    from app.models.user import User

    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, AiCallLog.__table__])
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add_all([User(id=1, username='usage1', password='x', role='user', status=1),
                User(id=2, username='usage2', password='x', role='user', status=1)])
    now = datetime.now(timezone.utc)
    db.add_all([
        AiCallLog(user_id=1, model_name='test', total_tokens=10, status='success', create_time=now),
        AiCallLog(user_id=2, model_name='test', total_tokens=99, status='success', create_time=now),
    ])
    db.commit()
    app = FastAPI()
    app.include_router(router, prefix='/me')
    app.dependency_overrides[get_db] = lambda: db
    # Override with a real request header to model two distinct authenticated identities.
    def current_user(x_user: int = Header()):
        return db.get(User, x_user)
    app.dependency_overrides[get_current_user] = current_user
    with TestClient(app) as client:
        first = client.get('/me/token-usage?days=1&user_id=2', headers={'x-user': '1'})
        second = client.get('/me/token-usage?days=1', headers={'x-user': '2'})
        assert first.status_code == 200 and first.json()['data']['total_tokens'] == 10
        assert second.status_code == 200 and second.json()['data']['total_tokens'] == 99
        assert client.get('/me/token-usage?days=91', headers={'x-user': '1'}).status_code == 422
    db.close()
    engine.dispose()
