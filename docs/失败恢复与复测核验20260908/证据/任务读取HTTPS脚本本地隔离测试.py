"""Independent local acceptance-script contracts; all HTTP is intercepted by ASGI."""
import importlib.util
import io
import json
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path('/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台')
RUNNER_PATH = ROOT / 'backend/scripts/verify_permission_acceptance_https.py'
SCRIPT_PATH = ROOT / 'docs/失败恢复与复测核验20260908/证据/verify_task_access_https.py'
SPEC = importlib.util.spec_from_file_location('verify_permission_acceptance_https', RUNNER_PATH)
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)

from app.main import app
from app.api.v1 import auth
from app.core.database import Base, get_db
from app.core.config import settings
from app.core.rate_limit import LoginFailureLimiter
from app.core.security import hash_password
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.models.review_task import ReviewTask
from app.models.ai_call_log import AiCallLog


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'app_env', 'production')
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    manifest = {'marker': '20260908', 'role_code': 'qa_permission_20260908', 'accounts': {}}
    role = Role(name='本地隔离验收', code=manifest['role_code'], status='active')
    db.add(role); db.flush()
    for code in HELPER.EXPECTED_PERMISSIONS:
        permission = Permission(code=code,name=code,module=code.split(':')[0],type='api')
        db.add(permission); db.flush(); db.add(RolePermission(role_id=role.id,permission_id=permission.id))
    for index,account in enumerate(HELPER.ACCOUNTS,107):
        user=User(id=index,username=f'qa_20260908_{account}',password=hash_password('isolated-valid-password'),role='user',status=1)
        db.add(user); db.flush()
        manifest['accounts'][account]={'id':user.id,'username':user.username,'password':'isolated-valid-password'}
        if account != 'no_permission':db.add(UserRole(user_id=user.id,role_id=role.id))
    db.commit()
    previous=dict(app.dependency_overrides)
    app.dependency_overrides.clear(); app.dependency_overrides[get_db]=lambda:db
    monkeypatch.setattr(auth,'login_failure_limiter',LoginFailureLimiter(redis_url='',limit=5,window_seconds=60))
    client=TestClient(app)
    requests=[]; mutate=[None]
    def open_local(request,timeout):
        path=request.full_url.removeprefix('https://lijiadong.cn')
        assert path.startswith('/api/review/tasks/') or path.startswith('/api/rbac/users/') or path=='/api/auth/login'
        assert request.get_method() == 'GET' or (request.get_method()=='POST' and path=='/api/auth/login')
        requests.append((request.get_method(),path))
        response=client.request(request.get_method(),path,headers=dict(request.header_items()),content=request.data)
        data=response.json()
        if response.status_code==404 and mutate[0]=='leaky404':data['detail']={'project_id':161,'private':'must-not-pass'}
        if response.status_code==401 and mutate[0]=='wrong401':data['code']=40102
        if response.status_code==403 and mutate[0]=='wrong403':data['code']=40301
        body=io.BytesIO(json.dumps(data).encode()); body.code=response.status_code; body.headers=response.headers
        return body
    original=HELPER.Runner
    class LocalRunner(original):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.interval=0; self.client=SimpleNamespace(open=open_local)
    monkeypatch.setattr(HELPER,'Runner',LocalRunner)
    monkeypatch.setitem(sys.modules,'verify_permission_acceptance_https',HELPER)
    def execute():
        credentials=tmp_path/'credentials.json'; credentials.write_text(json.dumps(manifest)); os.chmod(credentials,0o600)
        output=tmp_path/'evidence.json'
        monkeypatch.setattr(sys,'argv',[str(SCRIPT_PATH),'--credentials',str(credentials),'--output',str(output)])
        try:
            runpy.run_path(str(SCRIPT_PATH),run_name='__main__')
        except RuntimeError:
            if output.exists():
                saved=json.loads(output.read_text())
                print(json.dumps({'failure_type':saved.get('failure_type'),'last_request':saved.get('requests',[])[-1:]},ensure_ascii=False))
            raise
        return json.loads(output.read_text())
    try:
        yield SimpleNamespace(execute=execute,requests=requests,manifest=manifest,mutate=mutate,db=db,output=tmp_path/'evidence.json')
    finally:
        client.close(); app.dependency_overrides.clear(); app.dependency_overrides.update(previous); db.close(); engine.dispose()


def test_real_login_runner_and_thirty_negative_gets_match_contract(isolated):
    evidence=isolated.execute()
    assert evidence['status']=='passed'
    assert len(isolated.requests)==42
    assert len([row for row in evidence['requests'] if row['path'].startswith('/api/review/tasks/')])==30
    assert isolated.db.query(ReviewTask).count()==isolated.db.query(AiCallLog).count()==0
    assert 'isolated-valid-password' not in json.dumps(evidence)
    assert 'access_token' not in json.dumps(evidence)


@pytest.mark.parametrize('field',['role_code','username','id'])
def test_manifest_identity_is_rejected_before_login(isolated,field):
    if field=='role_code':isolated.manifest['role_code']='foreign_role'
    elif field=='username':isolated.manifest['accounts']['owner_a']['username']='foreign-account'
    else:isolated.manifest['accounts']['owner_a']['id']=True
    with pytest.raises((ValueError,RuntimeError,AssertionError)):
        isolated.execute()
    assert isolated.requests==[]


@pytest.mark.parametrize('mutate',['leaky404','wrong401','wrong403'])
def test_error_envelope_rejects_privacy_leak_or_wrong_exact_auth_code(isolated,mutate):
    isolated.mutate[0]=mutate
    with pytest.raises((ValueError,RuntimeError,AssertionError)):
        isolated.execute()
    saved=json.loads(isolated.output.read_text())
    row=saved['requests'][-1]
    assert row['actual']=={'leaky404':404,'wrong401':401,'wrong403':403}[mutate]
    assert row['status']=='failed'
    assert 'must-not-pass' not in isolated.output.read_text()
