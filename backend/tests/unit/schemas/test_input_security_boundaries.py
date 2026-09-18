import pytest
from pydantic import ValidationError

from app.schemas.agent_governance import AgentToolPermissionUpsertIn, PolicyRuleUpsertIn
from app.schemas.auth import LoginIn
from app.schemas.report_template import ReportTemplateIn


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
