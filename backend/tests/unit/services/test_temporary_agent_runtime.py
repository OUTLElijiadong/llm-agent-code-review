"""临时分析执行器的权限、范围和输出真实性回归。"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.agents.base import AgentResult, BaseAgent
from app.core.permission_codes import PermissionCode
from app.models.agent_team import AgentTeam, AgentTeamMember, AgentTeamTask
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


def run(db, actor, payload=None, definition=None, context=None):
    return runtime.run_temporary_agent(
        db,
        actor,
        {
            "user_id": actor.id,
            "send_to": "temporary:specialist",
            "trace_id": "trace-temporary",
            "context": {"team_id": 88, "agent_team_task_id": 99, **(context or {})},
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


def test_two_accessible_projects_cannot_be_combined_under_one_file_scope(db, actor, model):
    db.add_all([
        Project(id=813, user_id=actor.id, project_name="另一个自有项目", status="active"),
        CodeFile(id=814, project_id=813, file_name="other.py", language="python",
                 content="print('other')", status="active", is_binary=0),
    ])
    db.commit()
    result = run(db, actor, {"file_ids": [811, 814]})
    assert result["status"] == "blocked" and not model[0]


def test_more_than_twenty_completed_dependencies_are_all_included(db, actor, model):
    dependencies = {
        f"prior_{index}": {"status": "completed", "result": {"summary": f"依赖结论 {index}"}}
        for index in range(25)
    }
    dependencies["prior_24"]["result"]["detail"] = "d" * 13_000 + "LAST_DEPENDENCY_EVIDENCE"
    result = run(db, actor, {"dependency_context": dependencies})
    assert result["status"] == "completed"
    sources = json.loads(model[0][0]["message"])["sources"]
    assert len([source for source in sources if source["type"] == "dependency_result"]) == 25
    assert "LAST_DEPENDENCY_EVIDENCE" in sources[-1]["text"]
    assert result["coverage"]["dependency_count"] == result["coverage"]["included_dependency_count"] == 25
    assert result["coverage"]["complete"] is True and result["coverage"]["truncated"] is False


def test_unicode_dependency_uses_actual_model_window_budget(db, actor, model, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 100_000)
    map_calls = []
    final_calls = []

    def answer(instance, message, **kwargs):
        if kwargs.get("system_prompt"):
            part = json.loads(message)
            map_calls.append(part)
            return AgentResult(success=True, data={
                "part_id": part["part_id"], "part_sha256": part["part_sha256"],
                "summary": f"已覆盖 {part['part_id']}",
            }, usage_log_ids=[1000 + len(map_calls)], http_attempts=1)
        final_calls.append(message)
        return AgentResult(success=True, data={"summary": "完成", "findings": [], "limitations": []},
                           usage_log_ids=[2000], http_attempts=1)

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor, {"dependency_context": {
        "earlier": {"status": "completed", "result": {"summary": "中文证据" * 7500}}
    }})
    assert result["status"] == "completed"
    assert map_calls
    assert len(final_calls) == 1
    source = json.loads(final_calls[0])["sources"][1]
    assert source["id"] == "dependency:earlier" and source["compressed"] is True
    assert source["covered_parts"] == len(map_calls)
    assert result["coverage"]["complete"] is True


@pytest.mark.parametrize("upstream_coverage", [{"complete": False}, {"truncated": True}])
def test_partial_dependency_does_not_claim_complete_coverage(db, actor, model, upstream_coverage):
    result = run(db, actor, {"dependency_context": {
        "earlier": {"status": "completed", "result": {
            "status": "completed", "coverage": upstream_coverage,
        }}
    }})
    assert result["status"] == "blocked"
    assert not model[0]


def test_selected_large_files_are_compacted_with_each_part_and_full_hash(db, actor, model, monkeypatch):
    import hashlib

    first = "first\n" + "x" * 70_000 + "FIRST_TAIL_RISK"
    second = "second\n" + "y" * 70_000 + "SECOND_TAIL_RISK"
    db.query(CodeFile).filter_by(id=811).update({"content": first})
    db.add(CodeFile(id=813, project_id=811, file_name="second.py", language="python",
                    content=second, status="active", is_binary=0))
    db.commit()
    map_calls = []
    final_calls = []

    def answer(instance, message, **kwargs):
        if kwargs.get("system_prompt"):
            part = json.loads(message)
            map_calls.append(part)
            return AgentResult(success=True, data={
                "part_id": part["part_id"], "part_sha256": part["part_sha256"],
                "summary": f"已读取 {part['part_id']}；末尾 {part['text'][-32:]}",
            }, usage_log_ids=[1000 + len(map_calls)], http_attempts=1)
        final_calls.append(json.loads(message))
        return AgentResult(success=True, data={"summary": "完成所选文件分析", "findings": [], "limitations": []},
                           usage_log_ids=[2000], http_attempts=1)

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor, {"file_ids": [811, 813]})
    assert result["status"] == "completed" and len(final_calls) == 1
    assert result["coverage"]["included_file_ids"] == [811, 813]
    assert result["coverage"]["source_chars_included"] == len(first) + len(second)
    assert result["coverage"]["complete"] is True and result["coverage"]["truncated"] is False
    assert result["coverage"]["compressed"] is True
    assert result["coverage"]["source_parts"] == result["coverage"]["covered_source_parts"] == len(map_calls)
    assert len(map_calls) == 18
    assert any("FIRST_TAIL_RISK" in part["text"] for part in map_calls)
    assert any("SECOND_TAIL_RISK" in part["text"] for part in map_calls)
    files = [source for source in final_calls[0]["sources"] if source["type"] == "source_file"]
    assert {source["id"]: source["sha256"] for source in files} == {
        "file:811": hashlib.sha256(first.encode()).hexdigest(),
        "file:813": hashlib.sha256(second.encode()).hexdigest(),
    }
    assert all(len(source["summary_parts"]) == 9 for source in files)
    assert result["usage_log_ids"] == [*(1000 + index for index in range(1, 19)), 2000]
    assert result["http_attempts"] == 19


@pytest.mark.parametrize("failure", ["missing_part", "length"])
def test_compaction_failure_never_reaches_final_model_or_completed(db, actor, model, monkeypatch, failure):
    db.query(CodeFile).filter_by(id=811).update({"content": "x" * 70_000})
    db.commit()
    calls = []

    def answer(instance, message, **kwargs):
        calls.append(message)
        assert kwargs.get("system_prompt")
        part = json.loads(message)
        if failure == "length":
            return AgentResult(success=False, failure_kind="output_truncated",
                               usage_log_ids=[1901], http_attempts=1)
        return AgentResult(success=True, data={"part_id": "wrong", "part_sha256": part["part_sha256"],
                                               "summary": "不完整覆盖"}, usage_log_ids=[1901], http_attempts=1)

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    monkeypatch.setattr(runtime, "enrich_recorded_usage", lambda *args, **kwargs: None)
    result = run(db, actor, {"file_id": 811})
    assert result["status"] == "failed" and result["retryable"] is False
    assert result["usage_log_ids"] == [1901] and result["http_attempts"] == 1
    assert len(calls) == 1


def test_source_change_after_first_compaction_call_blocks_remaining_calls(db, actor, model, monkeypatch):
    db.query(CodeFile).filter_by(id=811).update({"content": "x" * 70_000})
    db.commit()
    calls = []

    def answer(instance, message, **kwargs):
        calls.append(message)
        part = json.loads(message)
        db.query(CodeFile).filter_by(id=811).update({"content": "changed"}, synchronize_session=False)
        db.commit()
        return AgentResult(success=True, data={
            "part_id": part["part_id"], "part_sha256": part["part_sha256"], "summary": "首片已读",
        }, usage_log_ids=[1901], http_attempts=1)

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor, {"file_id": 811})
    assert result["status"] == "blocked" and result["retryable"] is False
    assert result["usage_log_ids"] == [1901] and result["http_attempts"] == 1
    assert len(calls) == 1


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


def test_project_without_selection_over_five_blocks_instead_of_claiming_partial_success(db, actor, model):
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
    assert result["status"] == "blocked" and result["retryable"] is False
    assert "显式选择" in result["summary"] and not model[0]


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


@pytest.mark.parametrize("change", ["content", "version_no", "file_name", "language"])
def test_source_changed_during_model_call_discards_old_analysis(db, actor, model, monkeypatch, change):
    def answer(*args, **kwargs):
        replacement = {
            "content": "safe_call(value)\n",
            "version_no": 2,
            "file_name": "renamed.py",
            "language": "javascript",
        }[change]
        db.query(CodeFile).filter_by(id=811).update({change: replacement}, synchronize_session=False)
        db.commit()
        return AgentResult(
            success=True,
            data={"summary": "STALE_MODEL_CONCLUSION", "findings": [], "limitations": []},
            usage_log_ids=[901], http_attempts=1,
        )

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor, {"file_id": 811})
    assert result["status"] == "blocked" and result["retryable"] is False
    assert result["usage_log_ids"] == [901] and result["http_attempts"] == 1
    assert "STALE_MODEL_CONCLUSION" not in json.dumps(result)
    assert not result.get("findings") and not result.get("coverage")


def test_source_changed_after_truncation_stops_paid_retry(db, actor, model, monkeypatch):
    calls = []

    def answer(*args, **kwargs):
        calls.append(1)
        db.query(CodeFile).filter_by(id=811).update(
            {"content": "safe_call(value)\n"}, synchronize_session=False
        )
        db.commit()
        return AgentResult(success=False, failure_kind="output_truncated", usage_log_ids=[901], http_attempts=1)

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor, {"file_id": 811})
    assert result["status"] == "blocked" and result["retryable"] is False
    assert result["usage_log_ids"] == [901] and result["http_attempts"] == 1
    assert len(calls) == 1


def _active_team_lease(db, actor):
    db.add(AgentTeam(
        id=88, user_id=actor.id, surface="user", session_key="temporary-lease",
        title="租约复核", objective="只分析当前源码", status="running", trace_id="temporary-lease",
    ))
    db.add(AgentTeamMember(
        id=89, team_id=88, member_key="specialist", display_name="临时分析员",
        address="temporary:specialist", kind="temporary", role="worker",
    ))
    db.add(AgentTeamTask(
        id=99, team_id=88, member_id=89, task_key="analyze", title="分析",
        instructions="只分析当前源码", status="running", lease_token="old-lease",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    ))
    db.commit()


@pytest.mark.parametrize("change", ["cancelled", "replaced_lease"])
def test_lost_team_lease_after_truncation_stops_paid_retry(db, actor, model, monkeypatch, change):
    _active_team_lease(db, actor)
    calls = []

    def answer(*args, **kwargs):
        calls.append(1)
        if change == "cancelled":
            db.query(AgentTeam).filter_by(id=88).update({"status": "cancelled"}, synchronize_session=False)
        else:
            db.query(AgentTeamTask).filter_by(id=99).update(
                {"lease_token": "new-lease"}, synchronize_session=False
            )
        db.commit()
        return AgentResult(success=False, failure_kind="output_truncated", usage_log_ids=[901], http_attempts=1)

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor, {"file_id": 811}, context={"member_id": 89, "lease_token": "old-lease"})
    assert result["status"] == "blocked" and result["retryable"] is False
    assert result["usage_log_ids"] == [901] and result["http_attempts"] == 1
    assert len(calls) == 1


def test_cancelled_team_discards_successful_model_result(db, actor, model, monkeypatch):
    _active_team_lease(db, actor)

    def answer(*args, **kwargs):
        db.query(AgentTeam).filter_by(id=88).update({"status": "cancelled"}, synchronize_session=False)
        db.commit()
        return AgentResult(
            success=True,
            data={"summary": "STALE_MODEL_CONCLUSION", "findings": [], "limitations": []},
            usage_log_ids=[901], http_attempts=1,
        )

    monkeypatch.setattr(BaseAgent, "call_json", answer)
    result = run(db, actor, {"file_id": 811}, context={"member_id": 89, "lease_token": "old-lease"})
    assert result["status"] == "blocked" and result["retryable"] is False
    assert result["usage_log_ids"] == [901] and result["http_attempts"] == 1
    assert "STALE_MODEL_CONCLUSION" not in json.dumps(result)


def test_input_over_compaction_capacity_fails_before_any_model_call(db, actor, model):
    result = run(db, actor, {"question": "大" * (runtime.SOURCE_PART_CHARS * 34)})
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "context_capacity_exceeded"
    assert result["retryable"] is False
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
