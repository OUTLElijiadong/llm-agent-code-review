import pytest
from pydantic import ValidationError

from app.schemas.agent_governance import AgentToolPermissionUpsertIn, PolicyRuleUpsertIn
from app.schemas.auth import LoginIn
from app.schemas.feedback import FeedbackIn
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
