import pytest
from pydantic import ValidationError

from app.schemas.agent_governance import AgentToolPermissionUpsertIn, PolicyRuleUpsertIn
from app.schemas.agent_mesh import AgentMeshMessageIn
from app.schemas.agent_studio import (
    AgentCreateIn,
    AgentReviseIn,
    CatalogInvokeIn,
    DecisionIn,
    SkillBindingIn,
    SkillCreateIn,
    SkillReviseIn,
    SubmitIn,
    VersionTestIn,
)
from app.schemas.auth import LoginIn
from app.schemas.feedback import FeedbackIn, FeedbackReplyIn
from app.schemas.maintenance import TicketHandleIn, TicketIn
from app.schemas.pentest import (
    PentestAuthorizeIn,
    PentestCreateIn,
    PentestStartToolIn,
    PentestStatusToolIn,
)
from app.schemas.project import ProjectIn, ProjectUpdateIn, RemoteProjectImportIn
from app.schemas.report_template import ReportTemplateIn
from app.schemas.rule import RuleIn


@pytest.mark.parametrize("effect", ["permit", "ALLOW", "", "unknown"])
def test_policy_rule_rejects_unknown_effect(effect: str) -> None:
    with pytest.raises(ValidationError):
        PolicyRuleUpsertIn(rule_code="r", name="r", effect=effect)


@pytest.mark.parametrize("permission", ["permit", "ALLOW", "", "unknown"])
def test_tool_permission_rejects_unknown_decision(permission: str) -> None:
    with pytest.raises(ValidationError):
        AgentToolPermissionUpsertIn(agent_code="a", tool_code="t", permission=permission)


def test_login_password_matches_business_length_boundary() -> None:
    assert LoginIn(username="user", password="x" * 32).password == "x" * 32
    with pytest.raises(ValidationError):
        LoginIn(username="user", password="x" * 33)


def test_report_template_has_explicit_capacity_budget() -> None:
    assert ReportTemplateIn(name="n", type="custom", content="x" * 262_144)
    with pytest.raises(ValidationError):
        ReportTemplateIn(name="n", type="custom", content="x" * 262_145)


@pytest.mark.parametrize("username", ["<svg/onload=1>", "a\x00b", "a\nb"])
def test_registration_username_rejects_markup_and_control_characters(username: str) -> None:
    from app.schemas.auth import RegisterIn

    with pytest.raises(ValidationError):
        RegisterIn(username=username, password="secret1")


def test_login_username_trims_legacy_safe_identifier() -> None:
    assert LoginIn(username="  lijiadong  ", password="secret").username == "lijiadong"


@pytest.mark.parametrize(
    "schema, payload",
    [
        (ProjectIn, {"project_name": "project\x00name"}),
        (ProjectUpdateIn, {"project_name": "project\x00name"}),
        (RemoteProjectImportIn, {"project_name": "project\x00name", "url": "https://example.com/repo.git"}),
    ],
)
def test_project_name_rejects_control_characters_before_html_sanitizing(schema, payload) -> None:
    with pytest.raises(ValidationError):
        schema(**payload)


def test_project_name_rejects_empty_value() -> None:
    with pytest.raises(ValidationError):
        ProjectIn(project_name="   ")


def test_project_name_strips_markup_before_storage() -> None:
    sanitized = ProjectIn(project_name="<script>project</script>").project_name
    assert sanitized == "project"


def test_project_name_rejects_markup_only_value_after_sanitization() -> None:
    with pytest.raises(ValidationError):
        ProjectIn(project_name="<script></script>")


def test_rule_code_is_a_stable_machine_identifier() -> None:
    assert RuleIn(
        rule_code="sec_custom-1",
        rule_name="规则",
        rule_type="security",
        rule_content="检查输入",
    ).rule_code == "sec_custom-1"
    with pytest.raises(ValidationError):
        RuleIn(rule_code="../evil", rule_name="规则", rule_type="security", rule_content="检查输入")


def test_feedback_and_chat_content_reject_control_characters() -> None:
    with pytest.raises(ValidationError):
        FeedbackIn(content="反馈\x00内容")
    from app.api.v1.ai_chat import ChatMessage

    with pytest.raises(ValidationError):
        ChatMessage(content="读取其他用户历史\x00")


@pytest.mark.parametrize(
    "schema",
    [
        FeedbackIn,
        FeedbackReplyIn,
        TicketIn,
        TicketHandleIn,
        AgentCreateIn,
        AgentReviseIn,
        SkillCreateIn,
        SkillReviseIn,
        SkillBindingIn,
        VersionTestIn,
        SubmitIn,
        DecisionIn,
        CatalogInvokeIn,
        PentestCreateIn,
        PentestAuthorizeIn,
        PentestStartToolIn,
        PentestStatusToolIn,
    ],
)
def test_internal_business_request_models_forbid_unknown_fields(schema) -> None:
    assert schema.model_config.get("extra") == "forbid"


def test_support_inputs_reject_controls_and_unknown_fields_but_allow_markup_text() -> None:
    with pytest.raises(ValidationError):
        TicketIn(title="故障\x00", description="页面无法打开")
    with pytest.raises(ValidationError):
        TicketIn(title="故障", description="页面无法打开", user_id=99)
    with pytest.raises(ValidationError):
        FeedbackIn(content="反馈", status="closed")

    # 业务文本不做破坏性过滤；参数化入库与前端转义承担注入边界。
    payload = FeedbackIn(content="<script>alert('x')</script> OR 1=1")
    assert payload.content == "<script>alert('x')</script> OR 1=1"


def test_agent_studio_nested_json_has_control_depth_and_byte_budgets() -> None:
    valid = AgentCreateIn(
        code="quality_guard",
        name="质量守门员",
        description="安全审查",
        prompt="请仔细审查全部代码并返回有明确证据的结构化问题。",
        review_focus="输入安全",
        model_config_json={"temperature": 0.1},
    )
    assert valid.model_config_json == {"temperature": 0.1}

    with pytest.raises(ValidationError):
        AgentCreateIn(
            code="quality_guard",
            name="质量守门员",
            prompt="请仔细审查全部代码并返回有明确证据的结构化问题。",
            review_focus="输入安全",
            model_config_json={"note": "x" * 33_000},
        )
    with pytest.raises(ValidationError):
        SkillCreateIn(
            code="secure_transform",
            name="安全转换",
            skill_type="llm_transform",
            definition={"prompt": "bad\x00value"},
        )
    nested: dict = {}
    for _ in range(13):
        nested = {"child": nested}
    with pytest.raises(ValidationError):
        SkillCreateIn(
            code="deep_transform",
            name="深度转换",
            skill_type="llm_transform",
            definition=nested,
        )


def test_catalog_source_preserves_whitespace_and_rejects_nested_control_characters() -> None:
    source = "  def f():\n\treturn 1\n"
    assert CatalogInvokeIn(code=source).code == source
    with pytest.raises(ValidationError):
        CatalogInvokeIn(code="print(1)", rules=[{"name": "bad\x00rule"}])


def test_pentest_request_rejects_controls_and_undeclared_authorization_fields() -> None:
    with pytest.raises(ValidationError):
        PentestCreateIn(project_id=1, target_type="web", notes="scope\x00escape")
    with pytest.raises(ValidationError):
        PentestAuthorizeIn(ack_rules=True, window_hours=4, authorized_by=1)


def test_agent_responses_rejects_unknown_fields_and_controls_but_preserves_code() -> None:
    from app.api.v1.agent_responses import AgentResponsesRequest

    source = "  def f():\n\treturn 1\n"
    request = AgentResponsesRequest(
        session_id="session-input-01",
        messages=[{"role": "user", "content": source}],
    )
    assert request.messages[0].content == source
    with pytest.raises(ValidationError):
        AgentResponsesRequest(
            session_id="session-input-01",
            messages=[{"role": "user", "content": "bad\x00message"}],
        )
    with pytest.raises(ValidationError):
        AgentResponsesRequest(
            session_id="session-input-01",
            messages=[{"role": "user", "content": "ok"}],
            owner_id=99,
        )


def test_agent_mesh_dynamic_json_rejects_control_and_depth_overflow() -> None:
    base = {
        "idempotency_key": "mesh-boundary-1",
        "send_to": "agent:code_reviewer",
        "message_type": "task.request",
        "subject": "执行审查",
        "payload": {"code": "print(1)"},
    }
    assert AgentMeshMessageIn.model_validate(base).payload["code"] == "print(1)"
    with pytest.raises(ValidationError):
        AgentMeshMessageIn.model_validate({**base, "payload": {"code": "bad\x00code"}})
    nested: dict = {}
    for _ in range(18):
        nested = {"child": nested}
    with pytest.raises(ValidationError):
        AgentMeshMessageIn.model_validate({**base, "payload": nested})
