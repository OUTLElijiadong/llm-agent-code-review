"""项目管理 Agent 不得把用户的长字段静默裁短后写入数据库。"""
from types import SimpleNamespace

from app.agents.project_manager_agent import ProjectManagerAgent, ProjectOps


def test_create_project_preserves_valid_full_fields(monkeypatch):
    from app.agents import project_manager_agent as module

    captured = []

    def fake_create(_db, *, user, payload):
        captured.append(payload)
        return SimpleNamespace(id=1, project_name=payload.project_name,
                               language=payload.language, status="active")

    monkeypatch.setattr(module.project_service, "create_project", fake_create)
    agent = ProjectManagerAgent()
    agent._ops = ProjectOps(db=SimpleNamespace())
    agent._user = SimpleNamespace(id=7)
    name = "项目" * 40
    description = "完整说明" * 90
    result = agent.create_project(name, description)
    assert result.success is True
    assert captured[0].project_name == name
    assert captured[0].description == description


def test_create_project_rejects_over_schema_limit_without_writing(monkeypatch):
    from app.agents import project_manager_agent as module

    called = []
    monkeypatch.setattr(module.project_service, "create_project", lambda *_args, **_kwargs: called.append(True))
    agent = ProjectManagerAgent()
    agent._ops = ProjectOps(db=SimpleNamespace())
    agent._user = SimpleNamespace(id=7)
    result = agent.create_project("名称" * 51, "说明")
    assert result.success is False
    assert called == []
