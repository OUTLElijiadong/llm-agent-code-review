"""Real JWT contract for personal-wallet enterprise global asset execution."""
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.v1.enterprise_assets import publication_router
from app.api.v1.personal_agent_publications import router as history_router
from app.core.database import get_db
from app.core.exceptions import AppError
from app.core.security import create_access_token
from app.services import personal_agent_execution_service as execution
from tests.unit.services.test_enterprise_public_personal_execution import make_public
from tests.unit.services.test_personal_agent_execution import context as _personal_context


@pytest.fixture(name="context")
def fixture_context(tmp_path, monkeypatch):
    yield from _personal_context.__wrapped__(tmp_path, monkeypatch)


def test_real_jwt_global_assets_have_personal_runs_and_history(context):
    factory, _, monkeypatch = context
    pub_id = make_public(factory)
    monkeypatch.setattr(execution, '_send_once', lambda *_: {
        'choices': [{'finish_reason': 'stop', 'message': {'content': '完成'}}],
        'usage': {'prompt_tokens': 12, 'completion_tokens': 7},
    })
    app = FastAPI()
    app.include_router(publication_router, prefix='/api')
    app.include_router(history_router, prefix='/api')
    @app.exception_handler(AppError)
    async def error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={'message': exc.message})
    def database():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = database
    owner = {'Authorization': 'Bearer ' + create_access_token(1, 'user', 0)}
    other = {'Authorization': 'Bearer ' + create_access_token(2, 'user', 0)}
    url = f'/api/enterprise-asset-catalog/{pub_id}/runs'
    with TestClient(app) as client:
        assert client.post(url, json={'request_key': 'one', 'input_text': '内容'}).status_code == 401
        listing = client.get('/api/enterprise-asset-catalog', headers=owner)
        assert listing.status_code == 200 and listing.json()['data'][0]['personal_execution_available'] is True
        assert client.get(f'/api/enterprise-asset-catalog?before_id={pub_id}', headers=owner).json()['data'] == []
        assert client.get('/api/enterprise-asset-catalog?before_id=0', headers=owner).status_code == 422
        overflow = client.get('/api/enterprise-asset-catalog?before_id=9223372036854775808', headers=owner)
        assert overflow.status_code == 422
        result = client.post(url, headers=owner, json={'request_key': 'one', 'input_text': '内容'})
        assert result.status_code == 200 and result.json()['data']['output'] == '完成'
        run_id = result.json()['data']['id']
        assert client.get(f'{url}/{run_id}', headers=other).status_code == 404
        assert client.get(f'{url}/by-request/one', headers=owner).json()['data']['id'] == run_id
        items = client.get('/api/me/agent-runs?source=enterprise_public', headers=owner).json()['data']
        assert len(items) == 1 and items[0]['enterprise_publication_id'] == pub_id
        assert client.get('/api/me/agent-runs?source=enterprise_public', headers=other).json()['data'] == []
