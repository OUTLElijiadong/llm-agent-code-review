"""临时分析执行器的权限、范围和输出真实性回归。"""

import json
from unittest.mock import Mock

import pytest

from app.agents.base import AgentResult, BaseAgent
from app.core.permission_codes import PermissionCode
from app.models.code_file import CodeFile
from app.models.custom_agent import CustomAgent, CustomAgentVersion
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import temporary_agent_runtime as runtime


@pytest.fixture
def actor(db):
    user = User(id=811, username="temporary-runtime", password="unused", role="user", status=1)
    db.add_all([user, Role(id=811, code="user", name="测试用户", status="active"), UserRole(user_id=811, role_id=811)])
    for index, code in enumerate(
        (PermissionCode.AGENT_CHAT, PermissionCode.PROJECT_VIEW, PermissionCode.FILE_VIEW), 811
    ):
        db.add_all(
            [
                Permission(id=index, code=code, name=code, module="agent"),
                RolePermission(role_id=811, permission_id=index),
            ]
        )
    db.add_all(
        [
            Project(id=811, user_id=user.id, project_name="当前项目", status="active"),
            Project(id=812, user_id=999, project_name="另一个账号的项目", status="active"),
            CodeFile(
                id=811,
                project_id=811,
                file_name="sample.py",
                language="python",
                content="eval(value)\n",
                status="active",
                is_binary=0,
            ),
            CodeFile(
                id=812,
                project_id=812,
                file_name="private.py",
                language="python",
                content="OTHER_ACCOUNT_SECRET",
                status="active",
                is_binary=0,
            ),
        ]
    )
    db.commit()
    return user


@pytest.fixture
def model(monkeypatch):
    calls = []

    def answer(instance, message, ctx=None, **kwargs):
        calls.append(
            {"instance": instance, "prompt": instance._system_prompt, "message": message, "ctx": ctx, "kwargs": kwargs}
        )
        return AgentResult(
            success=True,
            data={"summary": "依据输入完成有限分析", "findings": [], "limitations": []},
            model="configured-subagent",
            usage_log_ids=[991],
            http_attempts=1,
        )

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    # 无真实凭据解析；生产函数仍须调用已有配置器，并按当前用户归因。
    import app.services.agent_model_service as config

    configure = Mock(side_effect=lambda db, agent, user_id=None: agent)
    monkeypatch.setattr(config, "configure_subagent", configure)
    return calls, configure


def run(db, actor, payload=None, definition=None):
    return runtime.run_temporary_agent(
        db,
        actor,
        {
            "user_id": actor.id,
            "send_to": "temporary:specialist",
            "trace_id": "trace-temporary",
            "context": {"team_id": 88, "agent_team_task_id": 99},
            "payload": {"instructions": "分析当前材料", **(payload or {})},
        },
        definition or {"purpose": "边界分析", "instructions": "识别边界错误并给出依据"},
        "临时边界分析员",
    )


def test_general_analysis_uses_independent_prompt_and_no_permanent_assets(db, actor, model):
    one = run(db, actor, {"question": "比较两种排序算法"})
    two = run(db, actor, definition={"purpose": "性能分析", "instructions": "关注复杂度"})
    assert one["status"] == two["status"] == "completed"
    calls, config = model
    assert len(calls) == 2 and calls[0]["instance"] is not calls[1]["instance"]
    assert "边界分析" in calls[0]["prompt"] and "关注复杂度" in calls[1]["prompt"]
    assert "关注复杂度" not in calls[0]["prompt"]
    assert calls[0]["ctx"].user_id == actor.id
    assert config.call_count == 2 and all(call.kwargs["user_id"] == actor.id for call in config.call_args_list)
    assert one["execution_mode"] == "analysis_only" and one["formal_review"] is False
    assert one["usage_log_ids"] == [991]
    assert db.query(CustomAgent).count() == db.query(CustomAgentVersion).count() == 0


def test_file_only_infers_real_project_and_reads_source(db, actor, model):
    result = run(db, actor, {"file_id": 811})
    assert result["status"] == "completed"
    assert result["project_id"] == 811
    assert result["coverage"]["included_file_ids"] == [811]
    assert "eval(value)" in model[0][0]["message"]
    # 输出合同直接枚举合法来源，避免真实模型把行号拼进 source ID 后反复失败。
    schema = json.loads(model[0][0]["prompt"].split("Schema：\n", 1)[1])
    refs = schema["$defs"]["_Finding"]["properties"]["evidence_refs"]["items"]
    assert set(refs["enum"]) == {"task_input", "project:811", "file:811"}


def test_multiple_files_infer_same_project_and_report_count(db, actor, model):
    db.add(
        CodeFile(
            id=813,
            project_id=811,
            file_name="second.py",
            language="python",
            content="print('second')",
            status="active",
            is_binary=0,
        )
    )
    db.commit()
    result = run(db, actor, {"file_ids": [811, 813]})
    assert result["status"] == "completed" and result["project_id"] == 811
    assert result["coverage"]["included_file_count"] == result["coverage"]["total_files"] == 2
    assert result["coverage"]["included_file_ids"] == [811, 813]
    assert "eval(value)" in model[0][0]["message"] and "print('second')" in model[0][0]["message"]


@pytest.mark.parametrize(
    "payload",
    [
        {"file_ids": [811, 812]},
        {"file_ids": [811, 811]},
        {"file_ids": [True]},
        {"file_ids": [811, 812, 813, 814, 815, 816]},
        {"file_ids": []},
        {"file_ids": [None]},
        {"file_ids": "811"},
        {"file_id": 811, "file_ids": [811]},
    ],
)
def test_multiple_files_invalid_scope_blocks_before_model(db, actor, model, payload):
    result = run(db, actor, payload)
    assert result["status"] == "blocked" and not model[0]
    assert "OTHER_ACCOUNT_SECRET" not in json.dumps(result)


def test_binary_file_blocks_without_mutating_content(db, actor, model):
    db.query(CodeFile).filter_by(id=811).update({"is_binary": 1, "content": "BINARY_CONTENT"})
    db.commit()
    assert run(db, actor, {"file_id": 811})["status"] == "blocked"
    assert db.get(CodeFile, 811).content == "BINARY_CONTENT" and not db.dirty and not model[0]


def test_completed_dependencies_and_valid_source_finding_are_preserved(db, actor, model, monkeypatch):
    def answer(instance, message, **kwargs):
        sources = json.loads(message)["sources"]
        assert any(source["id"] == "dependency:earlier" and "有界建议" in source["text"] for source in sources)
        return AgentResult(
            success=True,
            data={
                "summary": "发现危险调用",
                "findings": [
                    {
                        "title": "动态执行",
                        "description": "eval 来自源码",
                        "severity": "高",
                        "kind": "inference",
                        "evidence_refs": ["file:811", "dependency:earlier"],
                        "file_id": 811,
                        "line_number": 1,
                    }
                ],
                "limitations": ["未执行代码"],
            },
        )

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(
        db,
        actor,
        {"file_id": 811, "dependency_context": {"earlier": {"status": "completed", "result": {"summary": "有界建议"}}}},
    )
    assert result["status"] == "completed" and result["findings"][0]["project_id"] == 811
    assert "未执行代码" in result["limitations"]
    assert "eval(value)" not in json.dumps(result)


def test_accounting_failure_never_retries_a_model_request(db, actor, model, monkeypatch):
    from app.services.ai_usage_context import UsageAccountingError

    def answer(*args, **kwargs):
        raise UsageAccountingError("unit")

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor)
    assert result["status"] == "failed" and result["retryable"] is False


@pytest.mark.parametrize(
    "payload", [{"project_id": 812}, {"file_id": 812}, {"project_id": 811, "file_id": 812}, {"file_id": True}]
)
def test_unavailable_or_inconsistent_scope_blocks_before_model(db, actor, model, payload):
    result = run(db, actor, payload)
    assert result["status"] == "blocked"
    assert not model[0]
    assert "OTHER_ACCOUNT_SECRET" not in json.dumps(result)


def test_current_permission_revocation_blocks_source_read(db, actor, model):
    db.query(RolePermission).filter_by(permission_id=813).delete()
    db.commit()
    result = run(db, actor, {"file_id": 811})
    assert result["status"] == "blocked"
    assert not model[0]


def test_project_analysis_reports_bounded_file_and_character_coverage(db, actor, model):
    db.query(CodeFile).filter_by(id=811).update({"content": "x" * 13000})
    for index in range(6):
        db.add(
            CodeFile(
                id=820 + index,
                project_id=811,
                file_name=f"large{index}.py",
                language="python",
                content="x" * 13000,
                is_binary=0,
            )
        )
    db.commit()
    result = run(db, actor, {"project_id": 811})
    assert result["status"] == "completed"
    coverage = result["coverage"]
    assert coverage["total_files"] == 7 and len(coverage["included_file_ids"]) <= 5
    assert coverage["source_chars_included"] <= 60000
    assert coverage["truncated"] is True and coverage["complete"] is False
    assert result["limitations"]
    assert len(model[0][0]["message"]) <= 60000


def test_incomplete_dependency_never_claims_completed(db, actor, model):
    result = run(
        db, actor, {"dependency_context": {"failed_scan": {"status": "failed", "result": {"summary": "失败"}}}}
    )
    assert result["status"] == "blocked"
    assert not model[0]


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"summary": "伪成功", "findings": [], "limitations": [], "task_id": 666},
        {
            "summary": "伪证据",
            "findings": [
                {
                    "title": "不存在证据",
                    "description": "伪造",
                    "severity": "高",
                    "kind": "fact",
                    "evidence_refs": ["file:999"],
                }
            ],
            "limitations": [],
        },
    ],
)
def test_invalid_model_contract_or_invented_evidence_fails(db, actor, model, monkeypatch, data):
    monkeypatch.setattr(BaseAgent, "call_json", lambda *args, **kwargs: AgentResult(success=True, data=data))
    assert run(db, actor)["status"] == "failed"


def test_model_failure_remains_failure(db, actor, model, monkeypatch):
    monkeypatch.setattr(
        BaseAgent,
        "call_json",
        lambda *args, **kwargs: AgentResult(
            success=False, error="timeout", failure_kind="timeout", usage_log_ids=[992]
        ),
    )
    result = run(db, actor)
    assert result["status"] == "failed" and result["usage_log_ids"] == [992]


def test_output_truncation_retries_once_with_compact_prompt_and_aggregated_usage(db, actor, model, monkeypatch):
    calls = []

    def answer(instance, message, **kwargs):
        calls.append((message, kwargs))
        if len(calls) == 1:
            return AgentResult(
                success=False,
                failure_kind="output_truncated",
                usage_log_ids=[901],
                http_attempts=1,
                model="configured-subagent",
            )
        return AgentResult(
            success=True,
            data={"summary": "有限结论", "findings": [], "limitations": []},
            usage_log_ids=[902],
            http_attempts=1,
            model="configured-subagent",
        )

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor, {"file_id": 811})
    assert result["status"] == "completed"
    assert result["usage_log_ids"] == [901, 902] and result["http_attempts"] == 2
    assert len(calls) == 2
    assert calls[0][1]["max_tokens"] == runtime.INITIAL_OUTPUT_TOKENS
    assert calls[1][1]["max_tokens"] == runtime.RETRY_OUTPUT_TOKENS
    assert "最多 6 条" in calls[1][0] and "eval(value)" in calls[1][0]


def test_repeated_output_truncation_stops_without_outer_identical_retry(db, actor, model, monkeypatch):
    calls = []

    def answer(*args, **kwargs):
        calls.append(kwargs)
        return AgentResult(
            success=False,
            failure_kind="output_truncated",
            usage_log_ids=[900 + len(calls)],
            http_attempts=1,
        )

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor, {"file_id": 811})
    assert result["status"] == "failed" and result["retryable"] is False
    assert result["errors"][0]["failure_kind"] == "output_truncated"
    assert result["usage_log_ids"] == [901, 902] and result["http_attempts"] == 2
    assert len(calls) == 2


def test_output_truncation_rechecks_account_before_second_request(db, actor, model, monkeypatch):
    calls = []

    def answer(*args, **kwargs):
        calls.append(1)
        db.query(RolePermission).filter_by(permission_id=811).delete()
        db.commit()
        return AgentResult(success=False, failure_kind="output_truncated", usage_log_ids=[901], http_attempts=1)

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor, {"file_id": 811})
    assert result["status"] == "blocked" and result["retryable"] is False
    assert result["usage_log_ids"] == [901] and result["http_attempts"] == 1
    assert len(calls) == 1
    assert "eval(value)" not in json.dumps(result)


def test_oversized_user_input_is_explicitly_blocked(db, actor, model):
    result = run(db, actor, {"question": "大" * 70000})
    assert result["status"] == "blocked"
    assert not model[0]


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "agent_permission",
        "project_permission",
        "file_permission",
        "project_owner",
        "file_project",
        "file_deleted",
    ],
)
def test_access_revoked_during_model_call_discards_analysis(db, actor, model, monkeypatch, change):
    def answer(*args, **kwargs):
        if change == "disabled":
            db.query(User).filter_by(id=811).update({"status": 0}, synchronize_session=False)
        elif change.endswith("permission"):
            permission_id = {"agent_permission": 811, "project_permission": 812, "file_permission": 813}[change]
            db.query(RolePermission).filter_by(permission_id=permission_id).delete()
        elif change == "project_owner":
            db.query(Project).filter_by(id=811).update({"user_id": 999}, synchronize_session=False)
        elif change == "file_project":
            db.query(CodeFile).filter_by(id=811).update({"project_id": 812}, synchronize_session=False)
        else:
            db.query(CodeFile).filter_by(id=811).update({"status": "deleted"}, synchronize_session=False)
        db.commit()
        return AgentResult(
            success=True, data={"summary": "PRIVATE_MODEL_CONCLUSION", "findings": [], "limitations": []}
        )

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor, {"file_id": 811})
    assert result["status"] == "blocked"
    assert "PRIVATE_MODEL_CONCLUSION" not in json.dumps(result)
    assert not result.get("findings") and not result.get("coverage")


def test_file_finding_requires_its_source_reference(db, actor, model, monkeypatch):
    monkeypatch.setattr(
        BaseAgent,
        "call_json",
        lambda *args, **kwargs: AgentResult(
            success=True,
            data={
                "summary": "位置没有对应证据",
                "findings": [
                    {
                        "title": "危险调用",
                        "description": "源码结论",
                        "severity": "高",
                        "kind": "fact",
                        "evidence_refs": ["task_input"],
                        "file_id": 811,
                        "line_number": 1,
                    }
                ],
                "limitations": [],
            },
        ),
    )
    assert run(db, actor, {"file_id": 811})["status"] == "failed"


def test_general_findings_are_counted_once_in_dependency_summary(db, actor, model, monkeypatch):
    from app.services.agent_team_summary import summarize_dependencies

    monkeypatch.setattr(
        BaseAgent,
        "call_json",
        lambda *args, **kwargs: AgentResult(
            success=True,
            data={
                "summary": "两项需求缺口",
                "findings": [
                    {
                        "title": title,
                        "description": "缺少可验证定义",
                        "severity": "中",
                        "kind": "fact",
                        "evidence_refs": ["task_input"],
                    }
                    for title in ("时延未定义", "冲突处理未定义")
                ],
                "limitations": [],
            },
        ),
    )
    result = run(db, actor)
    summary = summarize_dependencies({"requirements": {"status": "completed", "result": result}})
    assert len(result["findings"]) == summary["unique_finding_count"] == len(summary["findings"]) == 2


@pytest.mark.parametrize("valid", [True, False, "invalid_json"])
def test_http_attempt_has_exact_owned_ledger_and_contract_status(db, actor, monkeypatch, valid):
    from types import SimpleNamespace

    import httpx

    from app.agents import base
    from app.models.ai_call_log import AiCallLog
    from app.services import agent_model_service
    from app.services.ai_usage_context import usage_context

    payload = {"summary": "真实内核模拟传输验收", "findings": [], "limitations": []} if valid else {"ok": True}
    requests = []
    original_client = httpx.Client

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "reported-unit-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "not valid JSON" if valid == "invalid_json" else json.dumps(payload)},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 9, "total_tokens": 19},
            },
        )

    def configure(db, agent, user_id=None):
        assert user_id == actor.id
        agent._base_url, agent._api_key, agent._model = "https://unit.example/v1", "unit", "requested-unit-model"
        agent._max_retries = 0
        agent._emit = lambda *args, **kwargs: None
        return agent

    monkeypatch.setattr(agent_model_service, "configure_subagent", configure)
    monkeypatch.setattr(
        base.httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setattr(
        base,
        "pin_public_http_url",
        lambda url: SimpleNamespace(request_url=url, host_header="unit.example", request_extensions={}),
    )
    with usage_context(actor.id, {"agent_team_id": 88, "agent_team_task_id": 99}, db=db):
        result = run(db, actor)
    db.rollback()
    row = db.query(AiCallLog).one()
    assert result["status"] == ("completed" if valid is True else "failed")
    assert len(requests) == 1 and result["usage_log_ids"] == [row.id]
    assert row.user_id == actor.id and row.agent_team_id == 88 and row.agent_team_task_id == 99
    assert row.total_tokens == 19 and row.model_name == "reported-unit-model"
    assert row.status == ("success" if valid is True else "failed")
    assert row.agent_label.startswith("temporary_") and row.task_id is None


def test_http_truncation_retry_records_both_calls_and_raises_budget_once(db, actor, monkeypatch):
    from types import SimpleNamespace

    import httpx

    from app.agents import base
    from app.models.ai_call_log import AiCallLog
    from app.services import agent_model_service
    from app.services.ai_usage_context import usage_context

    requests = []
    original_client = httpx.Client

    def handler(request):
        requests.append(json.loads(request.content))
        stopped = len(requests) == 2
        return httpx.Response(
            200,
            json={
                "model": "reported-unit-model",
                "choices": [
                    {
                        "finish_reason": "stop" if stopped else "length",
                        "message": {
                            "content": json.dumps(
                                {"summary": "精简复核完成", "findings": [], "limitations": []}
                            ) if stopped else '{"summary":',
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 9, "total_tokens": 19},
            },
        )

    def configure(db, agent, user_id=None):
        assert user_id == actor.id
        agent._base_url, agent._api_key, agent._model = "https://unit.example/v1", "unit", "requested-unit-model"
        agent._max_retries = 0
        agent._emit = lambda *args, **kwargs: None
        return agent

    monkeypatch.setattr(agent_model_service, "configure_subagent", configure)
    monkeypatch.setattr(
        base.httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setattr(
        base,
        "pin_public_http_url",
        lambda url: SimpleNamespace(request_url=url, host_header="unit.example", request_extensions={}),
    )
    with usage_context(actor.id, {"agent_team_id": 88, "agent_team_task_id": 99}, db=db):
        result = run(db, actor, {"file_id": 811})
    db.rollback()
    rows = db.query(AiCallLog).order_by(AiCallLog.id).all()
    assert result["status"] == "completed" and result["http_attempts"] == 2
    assert len(requests) == len(rows) == 2
    assert [request["max_tokens"] for request in requests] == [
        runtime.INITIAL_OUTPUT_TOKENS,
        runtime.RETRY_OUTPUT_TOKENS,
    ]
    assert "最多 6 条" in requests[1]["messages"][1]["content"]
    assert result["usage_log_ids"] == [row.id for row in rows]
    assert [row.status for row in rows] == ["failed", "success"]
    assert all(row.user_id == actor.id and row.agent_team_id == 88 for row in rows)
