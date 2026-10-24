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


def test_supervisor_agent_review_of_bare_read_capability_is_not_escalated():
    """生产团队 426：agent:supervisor 复核裸能力码 pentest.list 时不应要求确认。"""
    assert supervisor.review_action("pentest.list", "/pentests").decision == supervisor.ALLOW
    assert supervisor.review_action("pentest.create", "/pentests").needs_confirmation is True


def test_terminal_fallback_uses_member_business_facts_instead_of_internal_protocol_text():
    """生产 444/415：兜底不能只剩一句“已完成”或复述 claim_checks，要带上成员返回的真实数据。"""
    text = render_terminal_team_fallback({
        "team_id": 415, "status": "completed",
        "final_result": {"summary": "汇总已完成：已核对 1 个子任务；逐项结论见 claim_checks"},
        "member_facts": [{
            "task_key": "list_projects", "title": "列举当前账号名下全部项目", "total": 14,
            "items": [{"id": "174", "project_name": "本科答辩演示", "file_count": "1"},
                      {"id": "172", "project_name": "Local-File-Inclusion-Demo", "file_count": "3"}],
        }],
    })
    assert text.startswith("团队 415 已完成。")
    assert "列举当前账号名下全部项目：共 14 条。" in text
    assert "project_name=本科答辩演示" in text and "file_count=3" in text
    assert "其余 12 条见团队详情" in text
    assert "claim_checks" not in text


def test_terminal_fallback_reports_an_empty_result_explicitly():
    text = render_terminal_team_fallback({
        "team_id": 444, "status": "completed", "final_result": {"summary": "汇总已完成"},
        "member_facts": [{"task_key": "list_projects", "title": "枚举当前账号可见项目", "total": 0, "items": []}],
    })
    assert "枚举当前账号可见项目：结果为空（共 0 条）。" in text


def test_member_business_facts_are_bounded_and_skip_summarizer_nodes():
    from app.services.agent_mesh_service import _member_business_facts

    big = {"status": "completed", "evidence": [{"data": {
        "total": 99, "items": [{"id": n, "name": f"项目{n}", "a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6, "g": 7}
                               for n in range(30)],
    }}]}
    tasks = [
        {"task_key": "list", "title": "列表", "member_key": "reader", "status": "completed", "result": big},
        {"task_key": "sum", "title": "汇总", "member_key": "sum", "status": "completed", "result": big},
        {"task_key": "bad", "title": "失败节点", "member_key": "reader", "status": "failed", "result": big},
    ]
    facts = _member_business_facts(tasks, {"reader": "worker", "sum": "summarizer"})
    assert [item["task_key"] for item in facts] == ["list"]
    assert facts[0]["total"] == 99 and len(facts[0]["items"]) == 8
    assert all(len(row) <= 6 for row in facts[0]["items"])
