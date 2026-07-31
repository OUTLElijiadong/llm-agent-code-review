"""固定 Agent 工具参数契约的 Schema、严格校验与兼容别名测试。"""
from __future__ import annotations

from typing import Any

import pytest

from app.agents.tool_contracts import (
    FixedToolArgumentError,
    fixed_tool_accepts_ctx,
    get_fixed_tool_names,
    get_fixed_tool_schema,
    validate_fixed_tool_arguments,
)


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

    assert len(names) == 36
    assert len(names) == len(set(names))
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
    assert fixed_tool_accepts_ctx("list_agents") is False
    assert fixed_tool_accepts_ctx("list_agent_skills") is False
