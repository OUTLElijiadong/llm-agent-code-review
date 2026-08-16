"""小菱 RAG 知识笔记本工具(recall_knowledge / save_knowledge_note)与服务器运维教学引导回归。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services import agent_responses_service as service_module
from app.services.agent_responses_service import PrismToolExecutor, _instructions, _operations_tool_schema
from app.services.deepseek_responses_runtime import ToolCall


class EmptyMcp:
    async def discover(self) -> list[dict[str, Any]]:
        return []

    def has_tool(self, _: str) -> bool:
        return False


@pytest.fixture(autouse=True)
def lightweight_orchestrator(monkeypatch):
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())


def _executor(db, user, run_id: str) -> PrismToolExecutor:
    return PrismToolExecutor(
        db,
        user,
        surface="admin" if user.role in {"admin", "super_admin"} else "user",
        run_id=run_id,
        mcp_provider=EmptyMcp(),
    )


async def test_recall_knowledge_tool_schema_is_exposed(db, super_admin_user) -> None:
    executor = _executor(db, super_admin_user, "run-recall-schema")
    names = {schema["name"] for schema in await executor.tool_schemas() if isinstance(schema, dict)}
    assert "recall_knowledge" in names
    assert "save_knowledge_note" in names


def test_recall_knowledge_calls_unified_retrieve(db, super_admin_user, monkeypatch) -> None:
    executor = _executor(db, super_admin_user, "run-recall")
    captured: dict[str, Any] = {}

    def fake_retrieve(_db, **kwargs):
        captured.update(kwargs)
        return [{"content": "教学片段", "score": 0.9, "doc_id": 1, "title": "服务器运维手册", "source_type": "manual"}]

    monkeypatch.setattr(service_module.agent_knowledge_service, "unified_retrieve", fake_retrieve)
    result = executor._recall_knowledge(
        ToolCall("call-recall", "recall_knowledge", {"query": "如何开放端口", "top_k": 3}, "{}")
    )
    assert result.status == "success"
    assert result.output["count"] == 1
    assert result.output["hits"][0]["title"] == "服务器运维手册"
    assert captured["agent_code"] == "manager"
    assert captured["user_id"] == int(super_admin_user.id)


async def test_save_knowledge_note_requires_approval_and_persists(db, super_admin_user, monkeypatch) -> None:
    executor = _executor(db, super_admin_user, "run-save-note")
    call = ToolCall(
        "call-note",
        "save_knowledge_note",
        {"title": "端口开放经验", "content": "开放 8080 端口用 firewall_action add+port", "confidence": 0.9},
        "{}",
    )
    # 唯一超级管理员:save_knowledge_note 非高危,免审批直接执行(2026-08 需求)
    captured: dict[str, Any] = {}

    def fake_add(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=1, title=str(kwargs["title"]), status="active")

    monkeypatch.setattr(service_module.agent_knowledge_service, "add_document", fake_add)
    result = await executor.execute(call)
    assert result.status == "success"
    assert result.output["doc_id"] == 1
    assert captured["agent_code"] == "manager"
    assert captured["risk_level"] == "medium"


def test_operations_schema_includes_firewall_description() -> None:
    parameters = _operations_tool_schema()["parameters"]
    variants = {item["properties"]["action"]["const"]: item for item in parameters["oneOf"]}
    firewall = variants["firewall_action"]
    assert "开放或关闭防火墙" in firewall["description"]
    assert "8080" in firewall["description"] or "端口号" in firewall["description"]


def test_admin_instructions_teach_server_ops_and_notebook() -> None:
    text = _instructions("admin")
    assert "admin_execute_operation" in text
    assert "firewall_action" in text
    assert "开放" in text
    assert "recall_knowledge" in text
    assert "save_knowledge_note" in text


def test_user_instructions_teach_notebook() -> None:
    text = _instructions("user")
    assert "recall_knowledge" in text
    assert "save_knowledge_note" in text
    # 用户面不得泄露服务器运维动作引导
    assert "firewall_action" not in text


def test_admin_instructions_inject_identity_and_superadmin_boundary(db, super_admin_user) -> None:
    text = _instructions("admin", super_admin_user, is_super_admin=True)
    assert super_admin_user.username in text
    assert "超级管理员" in text
    assert "先向用户复述当前身份并请用户确认" in text


def test_ordinary_admin_instructions_state_server_ops_forbidden(db, admin_user) -> None:
    text = _instructions("admin", admin_user, is_super_admin=False)
    assert "仅超级管理员 admin 可执行" in text
    assert "没有服务器运维权限" in text


def test_user_instructions_inject_identity(db) -> None:
    from app.models.user import User
    member = User(username="member01", password="x", role="user", status=1)
    db.add(member)
    db.commit()
    text = _instructions("user", member, is_super_admin=False)
    assert member.username in text
    assert "普通用户" in text


def test_recall_knowledge_filters_ops_tutorial_for_non_super_admin(db, admin_user, monkeypatch) -> None:
    executor = _executor(db, admin_user, "run-recall-filter")
    hits = [
        {"content": "端口", "title": "运维教程(仅超级管理员)", "score": 0.9},
        {"content": "普通知识", "title": "系统角色权限说明书", "score": 0.8},
    ]
    monkeypatch.setattr(service_module.agent_knowledge_service, "unified_retrieve", lambda *_a, **_k: hits)
    result = executor._recall_knowledge(
        ToolCall("call-filter", "recall_knowledge", {"query": "端口", "top_k": 5}, "{}")
    )
    assert result.status == "success"
    titles = [hit["title"] for hit in result.output["hits"]]
    assert "运维教程(仅超级管理员)" not in titles
    assert "系统角色权限说明书" in titles


def test_recall_knowledge_keeps_ops_tutorial_for_super_admin(db, super_admin_user, monkeypatch) -> None:
    executor = _executor(db, super_admin_user, "run-recall-super")
    hits = [{"content": "端口", "title": "运维教程(仅超级管理员)", "score": 0.9}]
    monkeypatch.setattr(service_module.agent_knowledge_service, "unified_retrieve", lambda *_a, **_k: hits)
    result = executor._recall_knowledge(
        ToolCall("call-super", "recall_knowledge", {"query": "开放端口", "top_k": 5}, "{}")
    )
    assert result.status == "success"
    assert result.output["hits"][0]["title"] == "运维教程(仅超级管理员)"
