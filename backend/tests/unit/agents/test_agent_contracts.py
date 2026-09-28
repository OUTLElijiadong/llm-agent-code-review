"""Agent 职责、专属 Skill、提示词和协作边界契约测试。"""

from __future__ import annotations

from app.agents.contracts import (
    CONTRACTS,
    PROTECTED_AGENT_CODES,
    collaboration_allowed,
    compose_system_prompt,
    validate_contract_catalog,
)

EXPECTED_RUNTIME = {
    "orchestrator",
    "chat_assistant",
    "language_detector",
    "project_analyzer",
    "code_reviewer",
    "project_manager",
    "review_orchestrator",
    "code_file_manager",
    "dashboard",
    "rule_manager",
    "reporter",
    "ai_prompt",
    "security_sentinel",
    "evolution",
}
EXPECTED_SERVICES = {
    "manager",
    "operations",
    "approval",
    "policy",
    "scheduler",
    "memory_manager",
    "knowledge_distiller",
    "monitor",
    "reflection",
    "alert",
    "test_verifier",
    "sandbox_deployer",
    "quality_evaluator",
    "cost_controller",
    "model_evaluator",
    "report_verifier",
    "data_integrity",
    "incident_responder",
    "supervisor",
}


def test_contract_catalog_covers_all_agents_and_has_unique_skill_owners() -> None:
    """33 个现有 Agent/服务画像必须全部覆盖且每个 Skill 只有一个所有者。"""
    assert set(CONTRACTS) == EXPECTED_RUNTIME | EXPECTED_SERVICES
    assert PROTECTED_AGENT_CODES == {"chat_assistant", "manager"}
    skill_codes = [skill.code for item in CONTRACTS.values() for skill in item.skills]
    assert len(skill_codes) == len(set(skill_codes))
    validate_contract_catalog()


def test_each_contract_is_complete_and_renders_full_prompt() -> None:
    """每份契约必须包含角色、边界、Skill、输出与协作接口。"""
    for code, contract in CONTRACTS.items():
        assert contract.mission
        assert contract.responsibilities
        assert contract.allowed_operations
        assert contract.forbidden_operations
        assert contract.skills
        assert contract.output_fields
        prompt = contract.system_prompt()
        for section in ("核心使命", "职责范围", "允许执行", "禁止越界", "专属 Skill", "协作协议", "输出要求"):
            assert section in prompt, (code, section)
        for field in (
            "schema_version",
            "metadata.trace_id",
            "sent_from",
            "send_to",
            "message_type",
            "correlation_id",
            "payload",
            "artifacts",
            "errors",
        ):
            assert field in prompt, (code, field)
        assert "source_agent" not in prompt
        assert "target_agent" not in prompt
        assert "跨 Agent 协作消息必须" in prompt
        assert "用户或系统直接调用沿用" in prompt
        assert "只根据当前输入和已验证工具结果断言事实" in prompt
        assert "没有证据就说明未知或阻断" in prompt


def test_protected_interaction_agents_are_not_prompt_injected() -> None:
    """聊天和管理 Agent 只登记边界，不改写其既有系统提示词。"""
    baseline = "existing prompt"
    assert compose_system_prompt("chat_assistant", baseline) == baseline
    assert compose_system_prompt("manager", baseline) == baseline
    assert compose_system_prompt("code_reviewer", baseline) != baseline


def test_manager_contract_is_only_a_legacy_policy_alias() -> None:
    """manager 只能兼容权限策略，不能成为对话根或子 Agent 目标。"""
    manager = CONTRACTS["manager"]
    assert "不代表第二个主 Agent" in manager.mission
    assert manager.delegates_to == ()
    assert {skill.code for skill in manager.skills} == {"manager.admin_capabilities"}
    assert "策略编码" in manager.skills[0].purpose
    assert "不是执行主体" in manager.skills[0].usage_rule
    assert collaboration_allowed("manager", "operations") is False
    assert collaboration_allowed("operations", "manager") is False
    assert collaboration_allowed("chat_assistant", "operations") is True


def test_xiaoling_is_the_only_interaction_root_and_orchestrator_is_internal() -> None:
    """身份契约把小菱设为唯一主控，并把 orchestrator 限定为内部引擎。"""
    assert CONTRACTS["chat_assistant"].name == "小菱唯一主控"
    assert "唯一对用户负责" in CONTRACTS["chat_assistant"].mission
    assert CONTRACTS["orchestrator"].name == "内部调度引擎"
    assert "不是独立对话身份" in CONTRACTS["orchestrator"].mission


def test_collaboration_requires_reciprocal_allowlist() -> None:
    """已治理 Agent 间只允许双向声明的委派，未知端点不能成为旁路。"""
    assert collaboration_allowed("orchestrator", "code_reviewer") is True
    assert collaboration_allowed("code_reviewer", "reporter") is True
    assert collaboration_allowed("code_reviewer", "project_manager") is False
    assert collaboration_allowed("code_reviewer", "unknown_agent") is False
    assert collaboration_allowed("unknown_agent", "code_reviewer") is False
    assert collaboration_allowed("legacy_a", "legacy_b") is True


def test_llm_contracts_preserve_native_output_formats() -> None:
    """契约不能用通用外层结构破坏现有严格 JSON 或纯文本解析器。"""
    assert CONTRACTS["language_detector"].output_fields == (
        "language",
        "language_name",
        "confidence",
        "reason",
    )
    assert CONTRACTS["project_analyzer"].output_fields == (
        "project_name",
        "description",
        "language",
        "language_name",
    )
    assert CONTRACTS["code_reviewer"].output_fields == ("issues",)
    assert CONTRACTS["ai_prompt"].output_fields == ("修复提示词纯文本",)
    for code in ("language_detector", "project_analyzer", "code_reviewer", "security_sentinel", "ai_prompt"):
        assert "不得为了契约新增外层结构" in CONTRACTS[code].system_prompt()
