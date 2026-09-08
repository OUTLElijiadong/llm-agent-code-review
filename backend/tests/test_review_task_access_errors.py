"""真实 JWT/SQLite 验证审查读取的统一防枚举错误与合法成员回归。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.review import router
from app.core.database import Base, get_db
from app.core.error_handlers import register_handlers
from app.core.exceptions import ServiceUnavailableError
from app.core.security import create_access_token
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import project_member_service


@pytest.fixture
def review_http():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    users = {name: User(id=index, username=f'review_access_{name}', password='isolated', role='user', status=1)
             for index, name in enumerate(('owner', 'member', 'outsider', 'no_permission', 'disabled'), 1)}
    users['disabled'].status = 0
    role = Role(id=1, name='审查读取', code='review_read_test', status='active', is_builtin=0)
    db.add_all([*users.values(), role])
    for index, code in enumerate(('review:view', 'issue:view'), 1):
        db.add(Permission(id=index, code=code, name=code, module=code.split(':')[0], type='api'))
        db.add(RolePermission(role_id=role.id, permission_id=index))
    for name in ('owner', 'member', 'outsider', 'disabled'):
        db.add(UserRole(user_id=users[name].id, role_id=role.id))
    for index, status in enumerate(('active', 'deleted', 'quarantined'), 11):
        db.add(Project(id=index, user_id=users['owner'].id, project_name=f'private-project-{index}', status=status))
        db.add(ReviewTask(id=index, user_id=users['owner'].id, project_id=index,
                          task_name=f'private-task-{index}', status='success'))
    db.add(ProjectMember(project_id=11, user_id=users['member'].id, role_in_project='reviewer'))
    db.add(ReviewTask(id=14, user_id=users['owner'].id, project_id=999,
                      task_name='private-missing-parent', status='success'))
    db.add(ReviewTask(id=15, user_id=users['owner'].id, project_id=11,
                      task_name='private-deleted-task', status='deleted'))
    db.commit()
    application = FastAPI()
    register_handlers(application)
    application.include_router(router, prefix='/api/review')
    application.dependency_overrides[get_db] = lambda: db
    client = TestClient(application, raise_server_exceptions=False)
    writes = []

    def track_write(_conn, _cursor, statement, *_args):
        if statement.lstrip().split(' ', 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
            writes.append(statement.split(' ', 1)[0])

    event.listen(engine, 'before_cursor_execute', track_write)
    yield client, users, writes
    client.close()
    db.close()
    engine.dispose()


def headers(user):
    return {'Authorization': 'Bearer ' + create_access_token(user.id, user.role, user.token_version or 0)}


@pytest.mark.parametrize('suffix', ['', '/issues'])
def test_missing_invisible_deleted_and_parent_unavailable_have_identical_review_error(review_http, suffix):
    client, users, writes = review_http
    bodies = []
    cases = [('owner', 162), ('outsider', 11), ('owner', 12), ('owner', 13), ('owner', 14), ('owner', 15)]
    for actor, task_id in cases:
        response = client.get(f'/api/review/tasks/{task_id}{suffix}', headers=headers(users[actor]))
        assert response.status_code == 404
        body = response.json()
        assert body['code'] == 40400
        assert body.get('detail') is None
        assert 'private-' not in response.text and 'project_id' not in response.text
        body.pop('request_id')
        bodies.append(body)
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]['message'] == '审查任务不存在或当前账号无权访问'
    assert bodies[0]['retryable'] is False
    assert bodies[0]['next_action'] == '请返回审查记录列表重新选择；如需访问，请联系项目负责人确认权限'
    assert writes == []


@pytest.mark.parametrize('actor', ['owner', 'member'])
@pytest.mark.parametrize('suffix', ['', '/issues'])
def test_visible_owner_and_reviewer_keep_success(review_http, actor, suffix):
    client, users, writes = review_http
    response = client.get(f'/api/review/tasks/11{suffix}', headers=headers(users[actor]))
    assert response.status_code == 200
    assert response.json()['code'] == 0
    assert writes == []


@pytest.mark.parametrize('suffix', ['', '/issues'])
@pytest.mark.parametrize(('actor', 'status', 'code'), [
    (None, 401, 40100), ('no_permission', 403, 40303), ('disabled', 403, 40301),
])
def test_authentication_and_permission_errors_are_not_masked_as_not_found(review_http, suffix, actor, status, code):
    client, users, writes = review_http
    response = client.get(f'/api/review/tasks/162{suffix}', headers=headers(users[actor]) if actor else {})
    assert response.status_code == status
    assert response.json()['code'] == code
    assert writes == []


@pytest.mark.parametrize('suffix', ['', '/issues'])
def test_project_read_infrastructure_failure_is_not_masked_as_not_found(review_http, monkeypatch, suffix):
    client, users, writes = review_http

    def unavailable(*_args, **_kwargs):
        raise ServiceUnavailableError('隔离测试：连接暂不可用')

    monkeypatch.setattr(project_member_service, 'require_project_access', unavailable)
    response = client.get(f'/api/review/tasks/11{suffix}', headers=headers(users['owner']))
    assert response.status_code == 503
    assert response.json()['message'] == '隔离测试：连接暂不可用'
    assert response.json()['retryable'] is True
    assert writes == []
