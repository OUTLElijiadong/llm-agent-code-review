"""独立审阅：非目录的明确模型来源不能回退为代码审查员。"""
import pytest

from app.agents.orchestrator import Orchestrator
from app.agents.registry import AgentRegistry
from app.agents.skills.registry import SkillRegistry
from app.models.ai_call_log import AiCallLog
from app.services import agent_service


@pytest.mark.parametrize('label', ['api_connection_test', 'embedding'])
def test_explicit_non_catalog_source_does_not_create_fake_agent_history(db, admin_user, monkeypatch, label):
    monkeypatch.setattr(AgentRegistry, '_instance', AgentRegistry())
    monkeypatch.setattr(SkillRegistry, '_instance', SkillRegistry())
    Orchestrator()
    db.add(AiCallLog(user_id=admin_user.id, agent_label=label, model_name='provider-model', status='success'))
    db.commit()
    assert agent_service.get_runtime_agents(db, admin_user.id) == []
    assert agent_service.get_runtime_summary(db, admin_user.id)['total'] == 0
    assert agent_service.get_situation(db, admin_user.id)['online'] == 0
