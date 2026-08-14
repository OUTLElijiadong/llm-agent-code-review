"""小菱执行策略自动固化与失败纠偏测试。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.agent_capability import SandboxEnvironment
from app.models.agent_governance import AgentMemory
from app.services import agent_responses_service, strategy_learning_service
from app.services.deepseek_responses_runtime import COMPLETED, RuntimeResult


def _sandbox(*, owner_id: int, public_id: str, status: str = "succeeded") -> SandboxEnvironment:
    return SandboxEnvironment(
        public_id=public_id,
        project_id=17,
        owner_id=owner_id,
        worker_id=3,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="combined",
        status=status,
        runtime="runsc",
        image_ref="python@sha256:fixed",
        image_digest="sha256:fixed",
        source_sha256="a" * 64,
        resource_policy_json="{}",
        agent_config_json=json.dumps(
            {
                "worker_code": "worker-a",
                "source_revision_id": 88,
                "authorization": "Bearer should-never-be-persisted",
            }
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def test_strategy_fingerprint_redacts_secrets_and_dynamic_ids() -> None:
    first = strategy_learning_service.strategy_fingerprint(
        agent_code="test_verifier",
        tool_name="run_full_project_validation",
        arguments={
            "project_id": 17,
            "source_revision_id": 88,
            "language": "python",
            "test_mode": "combined",
            "api_key": "sk-secret-one",
            "path": "/Users/li/private/source.zip",
        },
        failure_kind="no_healthy_worker",
    )
    second = strategy_learning_service.strategy_fingerprint(
        agent_code="test_verifier",
        tool_name="run_full_project_validation",
        arguments={
            "project_id": 999,
            "source_revision_id": 1234,
            "language": "python",
            "test_mode": "combined",
            "api_key": "sk-secret-two",
            "path": "/tmp/another/source.zip",
        },
        failure_kind="no_healthy_worker",
    )

    assert first == second


def test_worker_health_error_has_specific_failure_kind() -> None:
    assert strategy_learning_service.classify_failure(
        "指定 worker 不健康、不支持当前任务或已达并发上限"
    ) == "no_healthy_worker"


def test_failed_tool_is_idempotent_and_injects_change_strategy(db) -> None:
    kwargs = {
        "owner_user_id": 7,
        "project_id": 17,
        "agent_code": "test_verifier",
        "tool_name": "run_full_project_validation",
        "arguments": {"project_id": 17, "language": "python", "worker_code": "missing"},
        "outcome": "failure",
        "failure_kind": "no_healthy_worker",
        "summary": "没有可用的隔离 worker，任务未运行",
        "evidence_ref": "tool:run-1:call-1",
    }
    strategy_learning_service.record_tool_outcome(db, **kwargs)
    strategy_learning_service.record_tool_outcome(db, **kwargs)
    db.commit()

    row = db.query(AgentMemory).filter(AgentMemory.memory_type == "execution_strategy").one()
    assert row.failure_count == 1
    assert row.success_count == 0
    assert row.share_scope == "user"
    assert "missing" in row.content
    assert "原样重试" in row.content

    prompt = strategy_learning_service.build_strategy_context(
        db,
        owner_user_id=7,
        surface="user",
        messages=[{"role": "user", "content": "继续验证项目 17"}],
    )
    assert "不得原样重试" in prompt
    assert "no_healthy_worker" in prompt
    assert "Bearer" not in prompt

    other_user_prompt = strategy_learning_service.build_strategy_context(
        db,
        owner_user_id=8,
        surface="user",
        messages=[{"role": "user", "content": "继续验证项目 17"}],
    )
    assert other_user_prompt == ""


def test_read_only_success_is_not_solidified(db) -> None:
    row = strategy_learning_service.record_tool_outcome(
        db,
        owner_user_id=7,
        project_id=17,
        agent_code="chat_assistant",
        tool_name="get_project_detail",
        arguments={"project_id": 17},
        outcome="success",
        summary="项目详情查询成功",
        evidence_ref="tool:read-only",
    )

    assert row is None
    assert db.query(AgentMemory).count() == 0


def test_read_only_dynamic_capability_success_is_not_solidified(db) -> None:
    row = strategy_learning_service.record_tool_outcome(
        db,
        owner_user_id=7,
        project_id=17,
        agent_code="chat_assistant",
        tool_name="user_execute_capability",
        arguments={"capability": "sandboxes.get", "params": {"public_id": "sbx_1"}},
        outcome="success",
        summary="状态查询成功",
        evidence_ref="tool:capability-read-only",
    )

    assert row is None


def test_base_instructions_do_not_duplicate_execution_memory_in_notebook() -> None:
    actor = type("Actor", (), {"username": "tester", "role": "user"})()
    instructions = agent_responses_service._instructions("user", actor)

    assert "自动按当前账户固化" in instructions
    assert "不得为这类执行结果重复调用 save_knowledge_note" in instructions


def test_sandbox_only_solidifies_verified_terminal_outcome(db) -> None:
    env = _sandbox(owner_id=9, public_id="sbx_verified")
    db.add(env)
    db.flush()

    assert strategy_learning_service.observe_sandbox_outcome(db, env, None) is None
    assert db.query(AgentMemory).count() == 0

    row = strategy_learning_service.observe_sandbox_outcome(
        db,
        env,
        {
            "passed": True,
            "summary": "组合测试通过",
            "evidence": {"worker_result": {"exit_code": 0}},
        },
    )
    strategy_learning_service.observe_sandbox_outcome(
        db,
        env,
        {
            "passed": True,
            "summary": "组合测试通过",
            "evidence": {"worker_result": {"exit_code": 0}},
        },
    )
    db.commit()

    assert row is not None
    assert row.success_count == 1
    assert row.failure_count == 0
    assert row.outcome == "success"
    assert row.confidence >= 0.7
    assert "已验证成功" in row.content


def test_reused_strategy_points_to_latest_evidence(db) -> None:
    common = {
        "owner_user_id": 9,
        "project_id": 17,
        "agent_code": "test_verifier",
        "tool_name": "run_full_project_validation",
        "arguments": {"project_id": 17, "language": "python", "worker_code": "worker-a"},
        "outcome": "success",
        "summary": "组合测试通过",
        "verified_async": True,
    }
    row = strategy_learning_service.record_tool_outcome(
        db, **common, evidence_ref="sandbox:sbx_old",
    )
    strategy_learning_service.record_tool_outcome(
        db, **common, evidence_ref="sandbox:sbx_new",
    )
    db.commit()

    assert row is not None
    assert row.source_ref == "sandbox:sbx_new"
    assert "sandbox:sbx_old" in row.evidence_json
    assert "sandbox:sbx_new" in row.evidence_json


def test_failed_sandbox_report_status_cannot_be_learned_as_success(db) -> None:
    env = _sandbox(owner_id=10, public_id="sbx_failed", status="failed")
    db.add(env)
    db.flush()

    row = strategy_learning_service.observe_sandbox_outcome(
        db,
        env,
        {"passed": False, "summary": "应用无法启动", "evidence": {"worker_result": {"exit_code": 1}}},
    )
    db.commit()

    assert row is not None
    assert row.outcome == "failure"
    assert row.failure_count == 1
    assert row.success_count == 0
    assert "改变方案" in row.content


@pytest.mark.asyncio
async def test_new_response_run_receives_same_account_strategy_context(db, monkeypatch) -> None:
    strategy_learning_service.record_tool_outcome(
        db,
        owner_user_id=21,
        project_id=17,
        agent_code="test_verifier",
        tool_name="run_full_project_validation",
        arguments={"project_id": 17, "language": "python", "worker_code": "missing"},
        outcome="failure",
        failure_kind="no_healthy_worker",
        summary="没有可用 worker",
        evidence_ref="tool:run-old:call-old",
    )
    db.commit()
    captured: dict[str, str] = {}

    class FakeExecutor:
        async def tool_schemas(self):
            return []

    class FakeRuntime:
        async def start(self, _messages, *, instructions, tools, run_id):
            captured["instructions"] = instructions
            assert tools == []
            return RuntimeResult(run_id=run_id, status=COMPLETED)

    async def fake_runtime(_self, _run_id, _event_sink):
        return FakeExecutor(), FakeRuntime()

    monkeypatch.setattr(agent_responses_service.AgentResponsesService, "_runtime", fake_runtime)
    service = agent_responses_service.AgentResponsesService(
        db,
        type("Actor", (), {"id": 21, "username": "tester", "role": "user"})(),
        surface="user",
        session_key="new-session",
    )

    await service.start(
        [{"role": "user", "content": "继续验证项目 17"}],
        run_id="run-new-session",
    )

    assert "小菱已验证策略记忆" in captured["instructions"]
    assert "不得原样重试" in captured["instructions"]
