"""文件名关键词从 Agent 工具传到已授权的代码列表查询。"""
from types import SimpleNamespace

from app.agents.file_agent import CodeFileManagerAgent
from app.services import code_file_service


def test_file_agent_passes_keyword_to_authorized_query(monkeypatch):
    captured = {}

    def list_files(db, **kwargs):
        captured.update(kwargs)
        return {
            "total": 1,
            "items": [SimpleNamespace(
                id=783, file_name="SecurityConfig.java", language="java",
                size_bytes=3725, line_count=73, version_no=1,
            )],
        }

    monkeypatch.setattr(code_file_service, "list_files", list_files)
    agent = CodeFileManagerAgent()
    actor = object()
    agent.inject(object(), user=actor)

    result = agent.list_files(15, keyword="SecurityConfig")

    assert result.success is True
    assert result.data["items"][0]["id"] == 783
    assert captured["user"] is actor
    assert captured["project_id"] == 15
    assert captured["keyword"] == "SecurityConfig"
