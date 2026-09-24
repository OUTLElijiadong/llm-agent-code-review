from app.agents.base import AgentResult
from app.agents.project_agent import ProjectAnalyzerAgent


def _answer(name="项目"):
    return AgentResult(success=True, data={
        "project_name": name,
        "description": "用于处理完整项目文件清单的业务系统",
        "language": "python",
        "language_name": "Python",
    })


def test_project_analysis_includes_files_after_first_thirty(monkeypatch):
    agent = ProjectAnalyzerAgent()
    messages = []

    def fake_call(message):
        messages.append(message)
        return _answer()

    monkeypatch.setattr(agent, "call_json", fake_call)
    files = [f"src/module_{index:03d}.py" for index in range(75)]
    result = agent.execute("大型项目", files)

    assert result.success
    assert "src/module_074.py" in "\n".join(messages)
    assert result.data["coverage"]["total_files"] == 75
    assert result.data["coverage"]["processed_files"] == 75


def test_project_analysis_does_not_succeed_when_a_source_chunk_fails(monkeypatch):
    agent = ProjectAnalyzerAgent()
    monkeypatch.setattr(agent, "_file_chunk_chars", lambda: 1200)
    calls = 0

    def fake_call(_message, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return AgentResult(success=False, error="模型截断", failure_kind="output_truncated")
        return _answer()

    monkeypatch.setattr(agent, "call_json", fake_call)
    result = agent.execute("多分片", [f"src/very_long_filename_{index:03d}.py" for index in range(100)])

    assert not result.success
    assert result.failure_kind == "output_truncated"
    assert calls == 2


def test_project_analysis_final_synthesis_sees_every_chunk(monkeypatch):
    agent = ProjectAnalyzerAgent()
    monkeypatch.setattr(agent, "_file_chunk_chars", lambda: 1200)
    messages = []

    def fake_call(message, **_kwargs):
        messages.append(message)
        return _answer(f"分片{len(messages)}")

    monkeypatch.setattr(agent, "call_json", fake_call)
    files = [f"src/very_long_filename_{index:03d}.py" for index in range(100)]
    result = agent.execute("多分片", files)

    assert result.success
    assert result.data["coverage"]["source_chunks"] > 1
    assert result.data["coverage"]["processed_files"] == len(files)
    assert "分片1" in messages[-1]


def test_project_analysis_rejects_oversized_model_output_without_cutting(monkeypatch):
    agent = ProjectAnalyzerAgent()
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda _message: AgentResult(success=True, data={
            "project_name": "合法名称", "description": "完整结尾" * 101,
            "language": "python", "language_name": "Python",
        }),
    )
    result = agent.execute("项目", ["main.py"])
    assert not result.success
    assert result.failure_kind == "invalid_output_contract"


def test_project_analysis_requires_nonempty_bounded_summary_for_each_chunk(monkeypatch):
    agent = ProjectAnalyzerAgent()
    monkeypatch.setattr(agent, "_file_chunk_chars", lambda: 1200)
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda _message, **_kwargs: AgentResult(success=True, data={
            "project_name": "项目", "description": "", "language": "python", "language_name": "Python",
        }),
    )
    result = agent.execute("多分片", [f"src/very_long_filename_{index:03d}.py" for index in range(100)])
    assert not result.success
    assert result.failure_kind == "invalid_summary"
