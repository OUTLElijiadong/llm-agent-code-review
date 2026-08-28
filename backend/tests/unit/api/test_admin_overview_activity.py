"""管理端 Agent 活跃状态今日调用量:工具网关 + 小菱 AiCallLog 合并统计。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.v1.admin_overview import _agent_activity
from app.models.agent_governance import AgentProfile, ToolCallLog
from app.models.ai_call_log import AiCallLog


def test_agent_activity_today_calls_merge_tool_and_ai_logs(db) -> None:
    db.add(AgentProfile(code="chat_assistant", name="小菱", is_enabled=1))
    db.add(AgentProfile(code="manager", name="管理副驾驶", is_enabled=1))
    # 工具网关调用 1 次
    db.add(ToolCallLog(
        agent_code="chat_assistant", tool_code="start_review", action="reviews.start",
        resource="project:1", status="success", risk_level="low", decision="allow",
        input_summary="", output_summary="",
    ))
    # 小菱 Responses 调用 2 次(AiCallLog)
    db.add(AiCallLog(
        user_id=7,
        agent_label="chat_assistant",
        model_name="deepseek-v4-flash",
        status="success",
        total_tokens=120,
    ))
    db.add(AiCallLog(
        user_id=7,
        agent_label="chat_assistant",
        model_name="deepseek-v4-flash",
        status="success",
        total_tokens=180,
    ))
    db.commit()

    rows = _agent_activity(db)
    by_code = {row["agent_code"]: row for row in rows}
    assert by_code["chat_assistant"]["calls_today"] == 3
    assert by_code["chat_assistant"]["model_calls_today"] == 2
    assert by_code["chat_assistant"]["model_tokens_today"] == 300
    assert by_code["chat_assistant"]["tool_calls_today"] == 1
    assert by_code["manager"]["calls_today"] == 0
    assert by_code["manager"]["model_calls_today"] == 0
    assert by_code["manager"]["model_tokens_today"] == 0
    assert by_code["manager"]["tool_calls_today"] == 0


def test_agent_activity_excludes_old_ai_logs(db) -> None:
    now = datetime.now(timezone.utc)
    db.add(AgentProfile(code="manager", name="管理副驾驶", is_enabled=1))
    old = now - timedelta(days=2)
    db.add(AiCallLog(
        user_id=7, agent_label="manager", model_name="deepseek-v4-flash",
        status="success", create_time=old,
    ))
    db.commit()
    rows = _agent_activity(db)
    by_code = {row["agent_code"]: row for row in rows}
    assert by_code["manager"]["calls_today"] == 0


def test_agent_activity_normalizes_discussion_labels_and_falls_back_to_component_tokens(db) -> None:
    """圆桌画像名与注册 Agent code 不一致时，仍须计入真实消费。"""
    db.add(AgentProfile(code="code_reviewer", name="代码审查员", is_enabled=1))
    db.add(AgentProfile(code="security_sentinel", name="安全哨兵", is_enabled=1))
    db.add(AiCallLog(
        agent_label="general",
        model_name="deepseek-v4-flash/discuss",
        status="success",
        total_tokens=None,
        prompt_tokens=120,
        completion_tokens=30,
    ))
    db.add(AiCallLog(
        agent_label="security",
        model_name="deepseek-v4-flash/discuss",
        status="success",
        total_tokens=10,
        prompt_tokens=40,
        completion_tokens=20,
    ))
    db.commit()

    rows = _agent_activity(db)
    by_code = {row["agent_code"]: row for row in rows}

    assert by_code["code_reviewer"]["model_calls_today"] == 1
    assert by_code["code_reviewer"]["model_tokens_today"] == 150
    assert by_code["security_sentinel"]["model_calls_today"] == 1
    # 组件 Token 大于错误/不完整的 total_tokens 时，按组件和计费。
    assert by_code["security_sentinel"]["model_tokens_today"] == 60


def test_agent_activity_exposes_legacy_unlabeled_model_calls_without_guessing_owner(db) -> None:
    """旧日志没有 agent_label 时应显式标记未归因,不猜测某个 Agent。"""
    db.add(AgentProfile(code="code_reviewer", name="代码审查员", is_enabled=1))
    db.add(AiCallLog(
        agent_label=None,
        model_name="deepseek-v4-pro",
        status="success",
        total_tokens=None,
        prompt_tokens=12,
        completion_tokens=8,
    ))
    db.commit()

    rows = _agent_activity(db)
    by_code = {row["agent_code"]: row for row in rows}

    assert by_code["code_reviewer"]["model_calls_today"] == 0
    assert by_code["unattributed_model"]["model_calls_today"] == 1
    assert by_code["unattributed_model"]["model_tokens_today"] == 20


def test_agent_activity_attributes_multi_agent_logs_to_known_review_agents(db) -> None:
    """multi-agent 日志应与 Agent 中心的历史归因口径一致。"""
    db.add(AgentProfile(code="code_reviewer", name="代码审查员", is_enabled=1))
    db.add(AgentProfile(code="security_sentinel", name="安全哨兵", is_enabled=1))
    db.add(AiCallLog(
        agent_label=None,
        model_name="deepseek-v4-flash/multi-agent",
        status="success",
        total_tokens=80,
    ))
    db.commit()

    rows = _agent_activity(db)
    by_code = {row["agent_code"]: row for row in rows}

    assert by_code["code_reviewer"]["model_calls_today"] == 1
    assert by_code["security_sentinel"]["model_calls_today"] == 1
    assert "unattributed_model" not in by_code
