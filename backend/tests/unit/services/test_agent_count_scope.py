"""真实注册表和隔离调用账本复现5/19/17，校验各数量的可见范围。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.event_bus import AgentEventBus
from app.agents.events import AgentEvent, AgentEventType
from app.agents.orchestrator import Orchestrator
from app.agents.registry import AgentRegistry
from app.agents.skills.registry import SkillRegistry
from app.api.v1.agents import get_metagpt_info, preview_metagpt_environment
from app.models.ai_call_log import AiCallLog
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import agent_service
from app.services.declarative_agent_runtime import PublishedAgentCatalog


@pytest.fixture
def count_scope(db, monkeypatch):
    registry = AgentRegistry()
    bus = AgentEventBus()
    monkeypatch.setattr(AgentRegistry, '_instance', registry)
    monkeypatch.setattr(SkillRegistry, '_instance', SkillRegistry())
    monkeypatch.setattr(AgentEventBus, '_instance', bus)
    Orchestrator()
    assert len(registry.list_runtime()) == 17
    custom = [{'code': f'qa_custom_{i}', 'name': f'合成已发布代理{i}', 'source': 'custom',
               'category': 'custom_review'} for i in range(2)]
    monkeypatch.setattr(PublishedAgentCatalog, 'runtime_metadata', lambda _db: [dict(row) for row in custom])
    user = User(username='qa_count_scope', password='isolated', role='user', status=1)
    other = User(username='qa_count_foreign', password='isolated', role='user', status=1)
    db.add_all([user, other])
    db.commit()
    codes = [r['code'] for r in registry.list_runtime() if r['code'] not in agent_service.USER_HIDDEN_BUILTIN][:5]
    return {'db': db, 'user': user, 'other': other, 'codes': codes, 'bus': bus, 'registry': registry}


def seed_calls(env, codes):
    for code in codes:
        env['db'].add(AiCallLog(user_id=env['user'].id, agent_label=code, model_name='local-fixture',
                               status='success', create_time=datetime.now(timezone.utc)))
    env['db'].commit()


def test_office_list_summary_and_situation_share_visible_five_of_nineteen(count_scope):
    env = count_scope
    seed_calls(env, env['codes'] + ['qa_custom_0'])
    runtime = agent_service.get_runtime_agents(env['db'], env['user'].id)
    summary = agent_service.get_runtime_summary(env['db'], env['user'].id)
    situation = agent_service.get_situation(env['db'], env['user'].id)
    assert len(agent_service.get_runtime_catalog(env['db'])) == 19
    assert len(runtime) == 5
    assert summary['total'] == sum(row['count'] for row in summary['by_category']) == len(runtime)
    assert situation['online'] == len(runtime)


def test_empty_member_has_zero_visible_agents_without_fake_online(count_scope):
    env = count_scope
    assert agent_service.get_runtime_agents(env['db'], env['user'].id) == []
    assert agent_service.get_runtime_summary(env['db'], env['user'].id)['total'] == 0
    assert agent_service.get_situation(env['db'], env['user'].id)['online'] == 0


@pytest.mark.parametrize('label', ['api_connection_test', 'embedding'])
def test_explicit_non_runtime_calls_do_not_fabricate_review_agent_history(count_scope, label):
    env = count_scope
    env['db'].add(AiCallLog(user_id=env['user'].id, agent_label=label, model_name='deepseek-chat',
                           status='success', create_time=datetime.now(timezone.utc)))
    env['db'].commit()
    assert agent_service.get_runtime_agents(env['db'], env['user'].id) == []
    assert agent_service.get_runtime_summary(env['db'], env['user'].id)['total'] == 0
    situation = agent_service.get_situation(env['db'], env['user'].id)
    assert situation['online'] == 0
    assert situation['hotspots'] == []
    assert situation['today_calls'] == 1


def test_unlabelled_legacy_call_keeps_existing_model_name_fallback(count_scope):
    env = count_scope
    env['db'].add(AiCallLog(user_id=env['user'].id, agent_label=None, model_name='deepseek-chat',
                           status='success', create_time=datetime.now(timezone.utc)))
    env['db'].commit()
    runtime = agent_service.get_runtime_agents(env['db'], env['user'].id)
    assert [(row['code'], row['call_count']) for row in runtime] == [('code_reviewer', 1)]


def test_custom_invoke_permission_is_applied_to_all_three_counts(count_scope):
    env = count_scope
    db = env['db']
    seed_calls(env, env['codes'] + ['qa_custom_0'])
    role = Role(code='qa_custom_invoke', name='验收角色', status='active')
    permission = Permission(code='custom_agent:invoke', name='调用', module='agent', type='api')
    db.add_all([role, permission])
    db.flush()
    db.add_all([RolePermission(role_id=role.id, permission_id=permission.id),
                UserRole(role_id=role.id, user_id=env['user'].id)])
    db.commit()
    assert len(agent_service.get_runtime_agents(db, env['user'].id)) == 6
    assert agent_service.get_runtime_summary(db, env['user'].id)['total'] == 6
    assert agent_service.get_situation(db, env['user'].id)['online'] == 6


def test_foreign_or_system_events_do_not_mark_member_agent_as_working(count_scope):
    env = count_scope
    code = env['codes'][0]
    seed_calls(env, [code])
    for owner in [env['other'].id, None]:
        env['bus'].publish(AgentEvent(type=AgentEventType.PROGRESS, agent=code,
                                      trace_id=f'foreign-{owner}', user_id=owner))
    assert agent_service.get_runtime_agents(env['db'], env['user'].id)[0]['status'] == 'idle'
    assert agent_service.get_situation(env['db'], env['user'].id)['working'] == 0
    env['bus'].publish(AgentEvent(type=AgentEventType.PROGRESS, agent=code, trace_id='own', user_id=env['user'].id))
    assert agent_service.get_situation(env['db'], env['user'].id)['working'] == 1


def test_recent_event_expiry_does_not_rewrite_historical_log(count_scope):
    env = count_scope
    code = env['codes'][0]
    seed_calls(env, [code])
    env['bus'].publish(AgentEvent(type=AgentEventType.PROGRESS, agent=code, trace_id='expired',
                                  user_id=env['user'].id,
                                  timestamp=(datetime.now(timezone.utc)-timedelta(seconds=120)).isoformat()))
    assert agent_service.get_situation(env['db'], env['user'].id)['working'] == 0
    assert env['db'].query(AiCallLog).one().status == 'success'


def test_admin_catalog_nineteen_and_metagpt_builtin_seventeen_are_distinct(count_scope):
    env = count_scope
    assert len(agent_service.get_runtime_agents(env['db'], None)) == 19
    assert agent_service.get_runtime_summary(env['db'], None)['total'] == 19
    assert agent_service.get_situation(env['db'], None)['online'] == 19
    info = get_metagpt_info(env['user']).data
    preview = preview_metagpt_environment('review', env['user']).data
    assert len(info['adaptable_agents']) == preview['registered_agent_count'] == 17
    assert info['catalog_scope'] == preview['catalog_scope'] == 'builtin_registry'
    assert len(preview['roles']) == 2
