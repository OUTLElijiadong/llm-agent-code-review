"""文件名关键词从 Agent 工具传到已授权的代码列表查询。"""
import hashlib
from types import SimpleNamespace

from app.agents import file_agent
from app.agents.file_agent import CodeFileManagerAgent
from app.agents.source_context import SourceContextError
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


def test_file_agent_get_file_does_not_silently_cut_source(monkeypatch):
    content = "line = 'source'\n" * 300
    actor = object()
    monkeypatch.setattr(
        code_file_service, "get_file",
        lambda db, user, file_id: SimpleNamespace(
            id=file_id, file_name="module.py", language="python",
            content=content, line_count=300, is_binary=0,
        ),
    )
    agent = CodeFileManagerAgent()
    agent.inject(object(), user=actor)

    result = agent.get_file(783)

    assert result.success is True
    assert result.data["content"] == content
    assert result.data["content_mode"] == "full"


def test_file_agent_compacts_every_long_source_chunk(monkeypatch):
    content = "abc\n" * 7_000
    monkeypatch.setattr(
        code_file_service, "get_file",
        lambda db, user, file_id: SimpleNamespace(
            id=file_id, file_name="long.py", language="python",
            content=content, line_count=7_000, is_binary=0,
        ),
    )
    observed = {}

    def compact(agent, source, *, ctx):
        observed["source"] = source
        return {"covered_source_ids": [c["source_id"] for c in source["source_chunks"]]}

    monkeypatch.setattr(file_agent, "compact_source_context", compact)
    agent = CodeFileManagerAgent()
    agent.inject(object(), user=object())

    result = agent.get_file(784)

    assert result.success is True
    assert result.data["content_mode"] == "compacted"
    assert result.data["content"] is None
    assert result.data["source_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    chunks = observed["source"]["source_chunks"]
    assert len(chunks) > 1
    assert "".join(chunk["text"] for chunk in chunks) == content
    assert result.data["content_context"]["covered_source_ids"] == [
        chunk["source_id"] for chunk in chunks
    ]


def test_file_agent_reports_oversized_source_as_failure(monkeypatch):
    content = "x" * 400_000
    monkeypatch.setattr(
        code_file_service, "get_file",
        lambda db, user, file_id: SimpleNamespace(
            id=file_id, file_name="oversized.py", language="python",
            content=content, line_count=1, is_binary=0,
        ),
    )
    agent = CodeFileManagerAgent()
    agent.inject(object(), user=object())

    result = agent.get_file(785)

    assert result.success is False
    assert result.failure_kind == "source_coverage_incomplete"
    assert "超过单轮压缩上限" in result.error


def test_file_agent_does_not_claim_success_when_compaction_fails(monkeypatch):
    monkeypatch.setattr(
        code_file_service, "get_file",
        lambda db, user, file_id: SimpleNamespace(
            id=file_id, file_name="long.py", language="python",
            content="code\n" * 3_000, line_count=3_000, is_binary=0,
        ),
    )

    def fail_compaction(agent, source, *, ctx):
        raise SourceContextError("来源分片 2 缺失")

    monkeypatch.setattr(file_agent, "compact_source_context", fail_compaction)
    agent = CodeFileManagerAgent()
    agent.inject(object(), user=object())

    result = agent.get_file(786)

    assert result.success is False
    assert result.failure_kind == "source_coverage_incomplete"
    assert "来源分片 2 缺失" in result.error
