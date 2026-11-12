"""Private Agent execution API with real JWT and owner-scoped billing."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.personal_agents import router
from app.core.database import Base, get_db
from app.core.exceptions import AppError
from app.core.security import create_access_token
from app.models.system_config import SystemConfig
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service as catalog
from app.services import personal_agent_execution_service as execution
from app.utils.api_resolver import ApiConfig


def test_real_api_runs_once_and_only_owner_can_recover_result(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        db.add_all([User(id=1, username="owner", password="x", role="user", status=1, token_version=0),
                    User(id=2, username="other", password="x", role="user", status=1, token_version=0)])
        db.flush()
        db.add(SystemConfig(config_key="llm_provider", config_value='{"active":false}'))
        catalog.seed_test_baseline(db)
        wallet = ledger.get_or_create_wallet(db, "user", 1)
        ledger.grant(db, wallet_id=wallet.id, grant_key="api-credit", kind="purchase", amount_milli=2_000_000)
    monkeypatch.setattr(execution.settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(execution, "resolve_api_config", lambda _db: ApiConfig(
        api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-flash", source="system",
    ))
    sends = []
    def send(_config, payload):
        sends.append(payload)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "结果"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    monkeypatch.setattr(execution, "_send_once", send)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    @app.exception_handler(AppError)
    async def error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})
    def database():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = database
    owner = {"Authorization": "Bearer " + create_access_token(1, "user", 0)}
    other = {"Authorization": "Bearer " + create_access_token(2, "user", 0)}
    with TestClient(app) as client:
        created = client.post("/api/me/agents", headers=owner,
                              json={"title": "助手", "prompt": "简洁回答", "request_key": "create"})
        assert created.status_code == 200
        assert created.json()["data"]["execution_available"] is True
        agent_id = created.json()["data"]["id"]
        url = f"/api/me/agents/{agent_id}/runs"
        body = {"request_key": "one", "version_number": 1, "input_text": "你好"}
        result = client.post(url, headers=owner, json=body)
        assert result.status_code == 200 and result.json()["data"]["output"] == "结果"
        run_id = result.json()["data"]["id"]
        assert client.post(url, headers=owner, json=body).json()["data"]["id"] == run_id
        assert client.get(f"{url}/by-request/one", headers=owner).json()["data"]["id"] == run_id
        assert client.get(f"{url}/{run_id}", headers=other).status_code == 404
        assert client.get(f"{url}/by-request/one", headers=other).status_code == 404
        assert client.post(url, headers=owner, json={**body, "input_text": "不同"}).status_code == 409
        assert client.post(url, headers=owner, json={**body, "model": "arbitrary"}).status_code == 422
        assert len(sends) == 1
    engine.dispose()


def test_execution_availability_requires_config_and_current_rates(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ready.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="user", status=1))
        db.flush()
        catalog.seed_test_baseline(db)
    monkeypatch.setattr(execution.settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(execution, "resolve_api_config", lambda _db: ApiConfig(
        api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-flash", source="system",
    ))
    assert execution.is_available(factory) is False
    with factory.begin() as db:
        db.add(SystemConfig(config_key="llm_provider", config_value='{"active":false}'))
    assert execution.is_available(factory) is True
    monkeypatch.setattr(catalog, "rate_snapshot", lambda *_a, **_kw: (_ for _ in ()).throw(
        catalog.RateCatalogError("来源过期")
    ))
    assert execution.is_available(factory) is False
    engine.dispose()
