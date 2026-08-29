"""固定 Agent 工具参数契约的 Schema、严格校验与兼容别名测试。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.agents.tool_contracts import (
    FixedToolArgumentError,
    ImportRemoteProjectArguments,
    fixed_tool_accepts_ctx,
    get_fixed_tool_names,
    get_fixed_tool_schema,
    validate_fixed_tool_arguments,
)

EXPECTED_FIXED_TOOL_NAMES = [
    "list_agents",
    "send_message",
    "create_agent_team",
    "get_agent_team",
    "cancel_agent_team",
    "retry_agent_team",
    "list_projects",
    "get_project_detail",
    "create_project",
    "delete_project",
    "start_review",
    "list_review_tasks",
    "list_review_issues",
    "list_code_files",
    "dashboard_summary",
    "list_rules",
    "list_reports",
    "detect_language",
    "analyze_project",
    "review_code",
    "generate_ai_prompt_for_issue",
    "generate_ai_prompt_for_task",
    "generate_ai_prompt_for_project",
    "audit_security_for_file",
    "audit_security_for_task",
    "audit_security_for_project",
    "run_full_project_validation",
    "create_pentest_engagement",
    "start_pentest_engagement",
    "get_pentest_status",
    "run_project_tests",
    "deploy_project_sandbox",
    "close_sandbox",
    "extend_sandbox",
    "recall_knowledge",
    "save_knowledge_note",
    "change_own_password",
    "trigger_evolution",
    "list_agent_skills",
    "search_published_agents",
    "invoke_published_agent",
    "admin_list_users",
    "admin_list_roles",
    "admin_governance_overview",
    "admin_list_agents",
    "admin_list_approvals",
    "admin_list_agent_release_approvals",
    "admin_system_status",
    "admin_set_user_role",
    "admin_delete_user",
    "admin_delete_users",
    "admin_toggle_agent",
    "admin_decide_agent_release",
]


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected"),
    [
        ("list_agents", {}, {}),
        ("list_projects", {"keyword": "安全", "page": 2}, {"keyword": "安全", "page": 2}),
        ("create_project", {"project_name": "demo"}, {"project_name": "demo"}),
        ("delete_project", {"project_id": 1}, {"project_id": 1}),
        ("start_review", {"project_id": 1}, {"project_id": 1}),
        ("start_review", {"project_id": 1, "file_ids": [2, 3]}, {"project_id": 1, "file_ids": [2, 3]}),
        ("list_review_tasks", {"project_id": None}, {"project_id": None}),
        ("list_review_issues", {"task_id": 4}, {"task_id": 4}),
        ("list_code_files", {"project_id": 1}, {"project_id": 1}),
        ("dashboard_summary", {}, {}),
        ("list_rules", {}, {}),
        ("list_reports", {"project_id": None}, {"project_id": None}),
        ("detect_language", {"project_name": "demo"}, {"project_name": "demo"}),
        (
            "analyze_project",
            {"folder_name": "src", "file_names": ["a.py"]},
            {"folder_name": "src", "file_names": ["a.py"]},
        ),
        (
            "review_code",
            {"code": "print(1)", "rules": "安全", "language": "python"},
            {"code": "print(1)", "rules": "安全", "language": "python"},
        ),
        ("generate_ai_prompt_for_issue", {"issue_id": 1}, {"issue_id": 1}),
        ("generate_ai_prompt_for_task", {"task_id": 1}, {"task_id": 1}),
        ("generate_ai_prompt_for_project", {"project_id": 1}, {"project_id": 1}),
        ("audit_security_for_file", {"file_id": 1}, {"file_id": 1}),
        ("audit_security_for_task", {"task_id": 1}, {"task_id": 1}),
        ("audit_security_for_project", {"project_id": 1}, {"project_id": 1}),
        (
            "run_full_project_validation",
            {"project_id": 1, "language": "python"},
            {"project_id": 1, "language": "python"},
        ),
        (
            "run_project_tests",
            {"project_id": 1, "language": "python"},
            {"project_id": 1, "language": "python"},
        ),
        (
            "deploy_project_sandbox",
            {"project_id": 1, "language": "node"},
            {"project_id": 1, "language": "node"},
        ),
        (
            "close_sandbox",
            {"public_id": "sbx_0123456789abcdef01234567"},
            {"public_id": "sbx_0123456789abcdef01234567"},
        ),
        (
            "extend_sandbox",
            {"public_id": "sbx_0123456789abcdef01234567", "hours": 24},
            {"public_id": "sbx_0123456789abcdef01234567", "hours": 24},
        ),
        (
            "change_own_password",
            {"old_password": "oldpass123", "new_password": "newpass456"},
            {"old_password": "oldpass123", "new_password": "newpass456"},
        ),
        ("trigger_evolution", {}, {}),
        ("list_agent_skills", {}, {}),
        ("search_published_agents", {"query": "安全审查"}, {"query": "安全审查"}),
        (
            "invoke_published_agent",
            {"agent_code": "secure_review", "code": "print(1)"},
            {"agent_code": "secure_review", "code": "print(1)"},
        ),
        ("admin_list_agent_release_approvals", {"approval_id": 7}, {"approval_id": 7}),
        ("admin_delete_users", {"user_ids": [26, 27]}, {"user_ids": [26, 27]}),
        (
            "admin_decide_agent_release",
            {"approval_id": 7, "decision": "approve"},
            {"approval_id": 7, "decision": "approve"},
        ),
    ],
)
def test_all_fixed_tool_contracts_accept_canonical_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    """成员侧固定工具均应接受其真实签名对应的最小规范参数。"""
    assert validate_fixed_tool_arguments(tool_name, arguments) == expected


def test_fixed_tool_registry_has_stable_unique_names() -> None:
    """固定工具注册表应成为唯一名称来源且不含重复项。"""
    names = get_fixed_tool_names()

    assert len(names) == 53  # 50 + 3 个渗透测试工具
    assert len(names) == len(set(names))
    assert names == EXPECTED_FIXED_TOOL_NAMES
    assert names[0] == "list_agents"
    assert names[-1] == "admin_decide_agent_release"


def test_schema_exposes_required_fields_and_forbids_model_owned_context() -> None:
    """Planner Schema 应暴露真实必填字段，并隐藏 ctx/user 等运行时参数。"""
    schema = get_fixed_tool_schema("start_review")

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"project_id"}
    assert "review_type" in schema["properties"]
    assert "ctx" not in schema["properties"]
    assert "user" not in schema["properties"]


def test_validation_rejects_missing_extra_and_strict_type_errors_without_echoing_input() -> None:
    """缺失、extra 与字符串冒充整数均应在执行前被拒绝且错误不回显输入。"""
    secret_marker = "sensitive-input-must-not-echo"

    with pytest.raises(FixedToolArgumentError, match="project_id"):
        validate_fixed_tool_arguments("delete_project", {})
    with pytest.raises(FixedToolArgumentError, match="不允许"):
        validate_fixed_tool_arguments("delete_project", {"project_id": 1, "ctx": secret_marker})
    with pytest.raises(FixedToolArgumentError, match="整数") as exc_info:
        validate_fixed_tool_arguments("delete_project", {"project_id": secret_marker})

    assert secret_marker not in str(exc_info.value)


def test_list_projects_normalizes_explicit_legacy_query_aliases() -> None:
    """历史 project_query/query 参数应显式迁移到 keyword，冲突输入必须拒绝。"""
    assert validate_fixed_tool_arguments(
        "list_projects",
        {"project_query": "旧参数", "page": 2},
    ) == {"keyword": "旧参数", "page": 2}
    assert validate_fixed_tool_arguments(
        "list_projects",
        {"query": "兼容参数"},
    ) == {"keyword": "兼容参数"}

    with pytest.raises(FixedToolArgumentError, match="不能同时"):
        validate_fixed_tool_arguments(
            "list_projects",
            {"keyword": "新参数", "project_query": "旧参数"},
        )


def test_runtime_context_injection_metadata_matches_real_handlers() -> None:
    """仅真实支持 ctx 的包装器应由执行器注入上下文。"""
    assert fixed_tool_accepts_ctx("list_projects") is True
    assert fixed_tool_accepts_ctx("start_review") is True
    assert fixed_tool_accepts_ctx("review_code") is False
    assert fixed_tool_accepts_ctx("list_agents") is True
    assert fixed_tool_accepts_ctx("send_message") is True
    assert fixed_tool_accepts_ctx("create_agent_team") is True
    assert fixed_tool_accepts_ctx("get_agent_team") is True
    assert fixed_tool_accepts_ctx("list_agent_skills") is False


def test_send_message_contract_is_strict_and_structured() -> None:
    payload = {
        "send_to": "session:user:session-b1",
        "message_type": "coordination",
        "subject": "同步排查结论",
        "payload": {"summary": "配置变更是根因"},
        "idempotency_key": "handoff-run-1",
    }
    assert validate_fixed_tool_arguments("send_message", payload)["send_to"] == payload["send_to"]
    with pytest.raises(FixedToolArgumentError):
        validate_fixed_tool_arguments("send_message", {**payload, "text": "自由文本"})


def test_agent_team_contract_is_strict_and_hides_session_ownership() -> None:
    payload = {
        "title": "发布前验证",
        "objective": "并行执行读取、审查和验证",
        "members": [
            {
                "member_key": "reader",
                "display_name": "读取 Agent",
                "address": "agent:project_analyzer",
            },
            {
                "member_key": "verifier",
                "display_name": "验证 Agent",
                "address": "agent:code_reviewer",
                "role": "verifier",
            },
        ],
        "tasks": [
            {
                "task_key": "read",
                "member_key": "reader",
                "title": "读取项目",
                "instructions": "读取项目结构",
            },
            {
                "task_key": "verify",
                "member_key": "verifier",
                "title": "验证结果",
                "instructions": "验证读取结论",
                "depends_on": ["read"],
            },
        ],
    }
    canonical = validate_fixed_tool_arguments("create_agent_team", payload)
    assert canonical["members"][0]["address"] == "agent:project_analyzer"
    schema = get_fixed_tool_schema("create_agent_team")
    assert "surface" not in schema["properties"]
    assert "session_id" not in schema["properties"]
    with pytest.raises(FixedToolArgumentError):
        validate_fixed_tool_arguments("create_agent_team", {**payload, "session_id": "spoofed"})


def test_retry_agent_team_contract_requires_explicit_strategy_mapping() -> None:
    canonical = validate_fixed_tool_arguments(
        "retry_agent_team",
        {
            "team_id": 42,
            "task_keys": ["read"],
            "strategy_changes": {"read": "刷新实时状态后改用路径 B"},
        },
    )
    assert canonical["strategy_changes"] == {"read": "刷新实时状态后改用路径 B"}
    assert "strategy_changes" in get_fixed_tool_schema("retry_agent_team")["properties"]
    with pytest.raises(FixedToolArgumentError):
        validate_fixed_tool_arguments(
            "retry_agent_team",
            {"team_id": 42, "strategy_changes": [], "surface": "admin"},
        )


def test_source_archive_agent_contracts_expose_audit_mode_and_static_full_default() -> None:
    """远程导入必须可显式选择隔离审计，项目安全审计默认不得退化为抽样。"""
    import_schema = ImportRemoteProjectArguments.model_json_schema()
    assert import_schema["properties"]["audit_mode"]["type"] == "boolean"
    assert import_schema["properties"]["audit_mode"]["default"] is False
    assert ImportRemoteProjectArguments.model_validate(
        {
            "url": "https://example.test/source.zip",
            "project_name": "audit-source",
            "audit_mode": True,
        }
    ).model_dump(exclude_unset=True)["audit_mode"] is True
    with pytest.raises(PydanticValidationError):
        ImportRemoteProjectArguments.model_validate(
            {
                "url": "https://example.test/source.zip",
                "project_name": "audit-source",
                "audit_mode": "true",
            }
        )

    audit_schema = get_fixed_tool_schema("audit_security_for_project")
    assert audit_schema["properties"]["scan_mode"]["default"] == "static_full"
    assert set(audit_schema["properties"]["scan_mode"]["enum"]) == {
        "full",
        "static_full",
        "triage",
    }
    assert validate_fixed_tool_arguments(
        "audit_security_for_project",
        {"project_id": 7, "scan_mode": "static_full"},
    ) == {"project_id": 7, "scan_mode": "static_full"}
    with pytest.raises(FixedToolArgumentError, match="scan_mode"):
        validate_fixed_tool_arguments(
            "audit_security_for_project",
            {"project_id": 7, "scan_mode": "sample"},
        )


def test_send_message_context_accepts_supervision_fields():
    """监督式复核协议要求模型回发带 supervision_* 的 context;
    工具契约此前 extra=forbid 缺这些字段,纠正轮次会被直接拒绝。"""
    from app.agents.tool_contracts import SendMessageContextArguments

    ctx = SendMessageContextArguments.model_validate({
        "run_id": "run-1",
        "supervision_objective": "复核越权问题是否修复",
        "supervision_round": 2,
        "supervision_max_rounds": 3,
        "supervision_correlation_id": "msg-orig-1",
    })
    assert ctx.supervision_round == 2
    assert ctx.supervision_correlation_id == "msg-orig-1"
