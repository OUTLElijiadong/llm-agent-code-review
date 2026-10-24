"""监督器对注册表只读能力与写能力的区分（生产误判 pentest.list/rules 为高风险的回归）。"""

import pytest

from app.services import agent_supervisor_service as supervisor
from app.services.agent_mesh_service import render_terminal_team_fallback


@pytest.mark.parametrize("capability", ["pentest.list", "pentest.rules"])
def test_registered_read_get_capability_is_not_escalated(capability):
    review = supervisor.review_team_task(
        address="agent:site_operator", task_key="read", title="读取", instructions="只读",
        task_input={"operation": "execute_capability", "scope": "user", "capability": capability, "params": {}},
    )
    assert review.needs_confirmation is False
    assert review.risk_level == supervisor.LOW


def test_registered_write_capability_with_high_risk_marker_still_escalates():
    review = supervisor.review_team_task(
        address="agent:site_operator", task_key="cancel", title="取消", instructions="取消委托",
        task_input={"operation": "execute_capability", "scope": "user", "capability": "pentest.cancel",
                    "params": {"public_id": "x"}},
    )
    assert review.needs_confirmation is True


def test_page_click_still_requires_confirmation():
    review = supervisor.review_team_task(
        address="agent:site_operator", task_key="click", title="点击", instructions="点击",
        task_input={"operation": "operate_current_page", "action": "click",
                    "args": {"control_handle": "ctrl_" + "a" * 32}},
    )
    assert review.needs_confirmation is True


def test_terminal_fallback_renders_status_summary_and_failed_nodes():
    text = render_terminal_team_fallback({
        "team_id": 414, "status": "failed",
        "final_result": {"summary": "部分完成"},
        "failed_task_samples": [{"task_key": "ops_status", "status": "failed",
                                 "error_previews": ["存在阻断性生产故障"]}],
        "omitted_failed_task_count": 2,
    })
    assert text.startswith("团队 414 执行失败。")
    assert "部分完成" in text
    assert "ops_status（failed）：存在阻断性生产故障" in text
    assert "另有 2 个" in text


def test_policy_review_allows_registered_read_capability_despite_sensitive_name():
    """生产回归：user.pentest.list 经策略引擎复核时不再因名称含 pentest 被升级确认。"""
    review = supervisor.review_action("user.pentest.list", "/pentests", declared_risk=supervisor.LOW)
    assert review.decision == supervisor.ALLOW and review.needs_confirmation is False
    write = supervisor.review_action("user.pentest.list_typo", "/pentests", declared_risk=supervisor.LOW)
    assert write.needs_confirmation is True
