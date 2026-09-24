"""沙箱 Agent 对长源码与错误的上下文完整性。"""

from __future__ import annotations

from types import SimpleNamespace

from app.agents.deployment_coordinator_agent import DeploymentCoordinatorAgent
from app.agents.source_context import SourceContextError, compact_source_context
from app.agents.syntax_repair_agent import SyntaxRepairAgent
from app.agents.test_case_generator_agent import TestCaseGeneratorAgent as CaseGeneratorAgent


def test_syntax_repair_reconstructs_large_file_from_unique_patch(monkeypatch) -> None:
    source = "<?php\n" + "// filler\n" * 6_000 + "echo broken;\n"
    agent = SyntaxRepairAgent()
    seen: list[str] = []

    def call_json(message, **_kwargs):
        seen.append(message)
        return SimpleNamespace(success=True, data={"edits": [{"old": "echo broken;", "new": "echo 'fixed';"}]})

    monkeypatch.setattr(agent, "call_json", call_json)
    result = agent.repair(
        language="php",
        errors=[{"file": "main.php", "line": 6002, "message": "unexpected identifier"}],
        files={"main.php": source},
    )
    assert result == {"files": {"main.php": source.replace("echo broken;", "echo 'fixed';")}}
    assert "已裁剪" in seen[0]
    assert "edits" in seen[0]


def test_syntax_repair_rejects_partial_file_return_and_nonunique_patch(monkeypatch) -> None:
    source = "<?php\n" + "echo broken;\n" * 6_000
    agent = SyntaxRepairAgent()
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: SimpleNamespace(success=True, data={"content": "<?php\necho 'fixed';"}),
    )
    result = agent.repair(
        language="php",
        errors=[{"file": "main.php", "line": 4000, "message": "unexpected identifier"}],
        files={"main.php": source},
    )
    assert "error" in result and not result.get("files")
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=True, data={"edits": [{"old": "echo broken;", "new": "echo 'fixed';"}]}
        ),
    )
    result = agent.repair(
        language="php",
        errors=[{"file": "main.php", "line": 4000, "message": "unexpected identifier"}],
        files={"main.php": source},
    )
    assert "error" in result and not result.get("files")


def test_deployment_never_claims_complete_with_uncovered_source(monkeypatch) -> None:
    agent = DeploymentCoordinatorAgent()
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: SimpleNamespace(success=True, data={"launch_script": "", "notes": "完整"}),
    )
    result = agent.plan(
        language="python",
        test_mode="blackbox",
        source_summary={"source_chunks": [], "coverage_complete": False},
    )
    assert "error" in result


def test_test_generator_never_calls_model_with_uncovered_source(monkeypatch) -> None:
    agent = CaseGeneratorAgent()
    calls: list[str] = []
    monkeypatch.setattr(agent, "call_json", lambda message, **_kwargs: calls.append(message))
    result = agent.generate(
        language="python",
        test_mode="whitebox",
        source_summary={"source_chunks": [], "coverage_complete": False},
    )
    assert "error" in result
    assert not calls


def test_source_compaction_calls_each_chunk_and_checks_coverage(monkeypatch) -> None:
    import hashlib

    agent = CaseGeneratorAgent()
    texts = ["入口 main.py", "结尾 secret_check.py"]
    chunks = [
        {
            "source_id": f"file-{index}-{hashlib.sha256(value.encode()).hexdigest()[:12]}",
            "text": value, "path": f"{index}.py",
            "sha256": hashlib.sha256(value.encode()).hexdigest(),
        }
        for index, value in enumerate(texts)
    ]
    seen: list[str] = []

    def call_json(message, **_kwargs):
        seen.append(message)
        chunk_id = chunks[len(seen) - 1]["source_id"]
        return SimpleNamespace(success=True, data={
            "covered_source_ids": [chunk_id], "summary": texts[len(seen) - 1],
        })

    monkeypatch.setattr(agent, "call_json", call_json)
    compacted = compact_source_context(
        agent,
        {"coverage_complete": True, "source_chunks": chunks, "source_file_count": 2},
        ctx=None,
    )
    assert compacted["covered_source_ids"] == [item["source_id"] for item in chunks]
    assert len(seen) == 2
    assert "结尾 secret_check.py" in str(compacted)


def test_source_compaction_rejects_unconfirmed_chunk(monkeypatch) -> None:
    import hashlib

    agent = CaseGeneratorAgent()
    text = "tail sentinel"
    monkeypatch.setattr(
        agent, "call_json",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=True, data={"covered_source_ids": [], "summary": text},
        ),
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    chunk = {"source_id": f"tail-{digest[:12]}", "text": text, "sha256": digest}
    try:
        compact_source_context(agent, {"coverage_complete": True, "source_chunks": [chunk]}, ctx=None)
    except SourceContextError as exc:
        assert "覆盖 ID" in str(exc)
    else:
        raise AssertionError("未确认来源不得成功")


def test_test_generator_uses_tail_chunk_summary_in_final_request(monkeypatch) -> None:
    import hashlib

    agent = CaseGeneratorAgent()
    texts = ["main.py contains entrypoint", "tail.py contains important_check"]
    chunks = [
        {"source_id": f"file-{idx}-{hashlib.sha256(value.encode()).hexdigest()[:12]}",
         "text": value, "sha256": hashlib.sha256(value.encode()).hexdigest()}
        for idx, value in enumerate(texts)
    ]
    messages: list[str] = []

    def call_json(message, **_kwargs):
        messages.append(message)
        if len(messages) <= len(chunks):
            idx = len(messages) - 1
            return SimpleNamespace(success=True, data={
                "covered_source_ids": [chunks[idx]["source_id"]], "summary": texts[idx],
            })
        return SimpleNamespace(success=True, data={
            "files": [{"path": "blackbox.py", "content": "assert True\n"}],
        })

    monkeypatch.setattr(agent, "call_json", call_json)
    result = agent.generate(
        language="python", test_mode="blackbox",
        source_summary={"coverage_complete": True, "source_chunks": chunks, "language": "python"},
    )
    assert result["files"][0]["path"] == "blackbox.py"
    assert "important_check" in messages[-1]
    assert all(chunk["source_id"] in messages[-1] for chunk in chunks)


def test_deployment_uses_all_compacted_source_parts(monkeypatch) -> None:
    import hashlib

    agent = DeploymentCoordinatorAgent()
    content = "last/entrypoint.py contains app startup"
    digest = hashlib.sha256(content.encode()).hexdigest()
    chunk = {"source_id": f"file-1-{digest[:12]}", "text": content, "sha256": digest}
    messages: list[str] = []

    def call_json(message, **_kwargs):
        messages.append(message)
        if len(messages) == 1:
            return SimpleNamespace(success=True, data={
                "covered_source_ids": [chunk["source_id"]], "summary": content,
            })
        return SimpleNamespace(success=True, data={"launch_script": "", "notes": "入口存在"})

    monkeypatch.setattr(agent, "call_json", call_json)
    result = agent.plan(
        language="python", test_mode="blackbox",
        source_summary={"coverage_complete": True, "source_chunks": [chunk]},
    )
    assert result["notes"] == "入口存在"
    assert "last/entrypoint.py" in messages[-1]
    assert chunk["source_id"] in messages[-1]
