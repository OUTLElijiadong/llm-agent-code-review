import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path('/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台')
sys.path.insert(0, str(ROOT / 'backend'))
SPEC = importlib.util.spec_from_file_location('qa_finalize', '/tmp/prism_finalize_qa_20260907_v2.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def manifest():
    return {'marker': m.MARKER, 'role_code': m.ROLE_CODE, 'role_id': 80, 'accounts': {name: {'id': uid, 'username': f'qa_{m.MARKER}_{name}', 'password': 'LOCAL_ONLY_Secret_Sentinel'} for name, uid in m.ACCOUNTS.items()}}


def snap(status=1, version=5):
    data = manifest()
    return {'context': {'database': 'code_review', 'readonly': 1}, 'users': [{'id': uid, 'username': data['accounts'][name]['username'], 'role': 'user', 'status': status, 'token_version': version} for name, uid in m.ACCOUNTS.items()], 'role': [{'id': 80, 'code': m.ROLE_CODE, 'status': 'active', 'is_builtin': 0}], 'assignments': [{'user_id': uid, 'role_id': 80, 'code': m.ROLE_CODE} for name, uid in m.ACCOUNTS.items() if name != 'no_permission'], 'projects': [{'id': pid, 'user_id': uid, 'marker_match': 1, 'status': 'active', 'row_sha256': 'b' * 64} for pid, uid in [(162, 103), (163, 105)]], 'counts': {'ai_call_log': 0, 'review_task': 0}}


def test_plan_never_reads_credentials_or_uses_network(monkeypatch, capsys):
    monkeypatch.setattr(m, 'read_manifest', lambda: pytest.fail('must not read production'))
    monkeypatch.setattr(m, 'run', lambda *_a, **_k: pytest.fail('must not run command'))
    assert m.main([]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'prepared_not_executed'


@pytest.mark.parametrize('kind', ['id', 'name', 'marker', 'role', 'extra_account', 'bool_role_id'])
def test_manifest_gate(kind):
    data = manifest()
    if kind == 'id': data['accounts']['owner_a']['id'] = 1
    if kind == 'name': data['accounts']['member_a']['username'] = 'real_user'
    if kind == 'marker': data['marker'] = 'different'
    if kind == 'role': data['role_code'] = 'admin'
    if kind == 'extra_account': data['accounts']['extra'] = data['accounts']['owner_a']
    if kind == 'bool_role_id': data['role_id'] = True
    with pytest.raises(m.FinalizationError): m.validate_manifest(data)


@pytest.mark.parametrize('kind', ['enabled', 'user_role', 'missing_user', 'extra_role', 'no_permission_role', 'model', 'review', 'project_owner', 'project_deleted'])
def test_database_gate(kind):
    data = snap()
    if kind == 'enabled': data['users'][0]['status'] = 0
    if kind == 'user_role': data['users'][0]['role'] = 'admin'
    if kind == 'missing_user': data['users'].pop()
    if kind == 'extra_role': data['role'].append(data['role'][0])
    if kind == 'no_permission_role': data['assignments'].append({'user_id': 106, 'role_id': 80, 'code': m.ROLE_CODE})
    if kind == 'model': data['counts']['ai_call_log'] = 1
    if kind == 'review': data['counts']['review_task'] = 1
    if kind == 'project_owner': data['projects'][0]['user_id'] = 1
    if kind == 'project_deleted': data['projects'][0]['status'] = 'deleted'
    with pytest.raises(m.FinalizationError): m.validate_snapshot(data, manifest(), status=1)


def test_sql_is_readonly_and_minimal(monkeypatch):
    value = snap()
    def command(args, *, input):
        sql = input.decode()
        assert 'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY' in sql
        assert 'ROLLBACK;' in sql
        assert all(word not in sql.upper().split() for word in ['INSERT', 'UPDATE', 'DELETE', 'ALTER', 'DROP'])
        assert 'MYSQL_ROOT_PASSWORD' in args[-1] and 'Secret' not in sql
        assert '--default-character-set=utf8mb4' in args[-1]
        return '\n'.join(k + '\t' + json.dumps(v) for k,v in value.items()).encode()
    monkeypatch.setattr(m, 'run', command)
    assert m.snapshot(80) == value


def test_http_forbids_other_endpoints():
    with pytest.raises(m.FinalizationError, match='http_operation_not_allowed'):
        m.http(None, 'POST', '/api/reviews', payload={})


def test_main_sanitizes_unexpected_errors(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(m, 'read_manifest', lambda: (_ for _ in ()).throw(ValueError('SECRET_VALUE')))
    assert m.main(['--execute']) == 1
    assert 'SECRET_VALUE' not in capsys.readouterr().out


def test_no_replay_existing_result(monkeypatch, tmp_path, capsys):
    result = tmp_path / 'result.json'
    result.write_text('existing')
    monkeypatch.setattr(m, 'RESULT', result)
    monkeypatch.setattr(m, 'read_manifest', manifest)
    monkeypatch.setattr(m, 'finalize', lambda *_a: pytest.fail('no replay'))
    assert m.main(['--execute']) == 1
    assert result.read_text() == 'existing'
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'


def test_normal_auth_existing_disable_and_old_token_rejection_real_sqlite(monkeypatch, tmp_path, capsys):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.core.database import Base, get_db
    from app.core.security import hash_password
    from app.main import app
    from app.models.user import User
    from app.models.rbac import Role, UserRole
    from app.models.project import Project
    from app.models.ai_call_log import AiCallLog
    from app.models.review_task import ReviewTask
    from app.api.v1 import auth as auth_api
    from app.core.rate_limit import LoginFailureLimiter
    data = manifest()
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        db.add(Role(id=80, name='local QA', code=m.ROLE_CODE, status='active', is_builtin=0))
        hashed = hash_password('LOCAL_ONLY_Secret_Sentinel')
        db.add(User(id=1, username='unrelated-real-fixture', role='admin', password=hashed, status=1, token_version=40))
        for name, uid in m.ACCOUNTS.items():
            db.add(User(id=uid, username=data['accounts'][name]['username'], role='user', password=hashed, status=1, token_version=5))
            if name != 'no_permission': db.add(UserRole(user_id=uid, role_id=80))
        for pid, uid in [(162, 103), (163, 105)]:
            db.add(Project(id=pid, user_id=uid, project_name=f'QA权限验收-20260907-{ "a" if pid==162 else "b"}', status='active'))
        db.commit()
    path = tmp_path / 'credentials.json'
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    pspec = importlib.util.spec_from_file_location('actual_prepare', ROOT / 'backend/scripts/prepare_permission_acceptance.py')
    prepare = importlib.util.module_from_spec(pspec)
    pspec.loader.exec_module(prepare)
    monkeypatch.setattr(prepare, 'SessionLocal', Session)
    monkeypatch.setattr(auth_api, 'login_failure_limiter', LoginFailureLimiter(redis_url='', limit=5, window_seconds=60))
    old = dict(app.dependency_overrides)
    def fixture_db():
        with Session() as db: yield db
    app.dependency_overrides[get_db] = fixture_db
    client = TestClient(app)
    tokens = []
    def open_local(request, timeout):
        assert request.full_url.startswith(m.BASE + '/api/auth/')
        res = client.request(request.method, request.full_url.removeprefix(m.BASE), headers=dict(request.header_items()), content=request.data)
        if request.method == 'POST' and res.status_code == 200: tokens.append(res.json()['data']['access_token'])
        body = io.BytesIO(res.content)
        body.code = res.status_code
        return body
    def local_snapshot(_role_id):
        value = snap()
        with Session() as db:
            users = db.query(User).filter(User.id.in_(m.ACCOUNTS.values())).order_by(User.id).all()
            value['users'] = [{'id': u.id, 'username': u.username, 'role': u.role, 'status': u.status, 'token_version': u.token_version} for u in users]
            assert db.query(Project).count() == 2
            value['counts'] = {'ai_call_log': db.query(AiCallLog).count(), 'review_task': db.query(ReviewTask).count()}
        return value
    calls = []
    def actual_disable(_manifest):
        calls.append('disable')
        monkeypatch.setattr(sys, 'argv', ['prepare', '--marker', m.MARKER, '--credentials', str(path), '--disable'])
        prepare.main()
    monkeypatch.setattr(m, 'snapshot', local_snapshot)
    monkeypatch.setattr(m, 'disable', actual_disable)
    report = {'accounts': []}
    try:
        m.finalize(data, SimpleNamespace(open=open_local), report, lambda: None)
        assert report['status'] == 'passed'
        assert calls == ['disable']
        assert len(report['accounts']) == 4
        assert all(r['token_version_before'] == 6 and r['token_version_after'] == 7 and r['old_token_me_http'] == 403 and r['old_token_me_business_code'] == 40301 for r in report['accounts'])
        text = json.dumps(report) + capsys.readouterr().out
        assert data['accounts']['owner_a']['password'] not in text
        assert all(token not in text for token in tokens)
        with Session() as db:
            untouched = db.get(User, 1)
            assert untouched.status == 1 and untouched.token_version == 40
            assert db.query(Project).count() == 2
            assert db.query(AiCallLog).count() == db.query(ReviewTask).count() == 0
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)
        engine.dispose()


def test_v1_latin1_chinese_marker_mismatch_reproduced_and_v2_declares_utf8(monkeypatch):
    oldspec = importlib.util.spec_from_file_location('qa_finalize_v1', '/tmp/prism_finalize_qa_20260907.py')
    old = importlib.util.module_from_spec(oldspec)
    oldspec.loader.exec_module(old)
    calls = []
    def mysql_contract(args, *, input):
        charset = 'utf8' if '--default-character-set=utf8mb4' in args[-1] else 'latin1'
        statement = input.decode(charset)
        value = snap()
        # This matches the separately observed production CLI charset contract.
        for row, suffix in zip(value['projects'], ('a', 'b')):
            row['marker_match'] = f"QA权限验收-20260907-{suffix}" in statement
        calls.append(charset)
        return '\n'.join(k + '\t' + json.dumps(v) for k,v in value.items()).encode()
    monkeypatch.setattr(old, 'run', mysql_contract)
    monkeypatch.setattr(m, 'run', mysql_contract)
    with pytest.raises(old.FinalizationError, match='qa_project_identity_changed'):
        old.validate_snapshot(old.snapshot(80), manifest(), status=1)
    m.validate_snapshot(m.snapshot(80), manifest(), status=1)
    assert calls == ['latin1', 'utf8']
