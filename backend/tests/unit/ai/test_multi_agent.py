from app.ai.multi_agent import (
    build_agent_summary,
    get_agent_profiles,
    get_discussion_agent_profiles,
    get_model_label,
)


def test_get_agent_profiles_returns_standard_for_unknown_type():
    """未知审查类型应回退到通用质量代理"""
    profiles = get_agent_profiles("unknown")

    assert len(profiles) == 1
    assert profiles[0].code == "general"


def test_get_agent_profiles_full_uses_multiple_agents():
    """全面审查应启用多个专项代理"""
    profiles = get_agent_profiles("full")
    codes = {profile.code for profile in profiles}

    assert {"security", "reliability", "performance", "maintainability"} <= codes


def test_get_discussion_agent_profiles_returns_every_review_specialist():
    """圆桌讨论应固定邀请全部五个代码审查子 Agent。

    Returns:
        None: 断言参会集合完整且顺序稳定。
    """
    profiles = get_discussion_agent_profiles()

    assert [profile.code for profile in profiles] == [
        "general",
        "security",
        "reliability",
        "performance",
        "maintainability",
    ]


def test_build_agent_summary_marks_multi_agent_mode():
    """多个代理组合应在摘要中明确体现多 Agent 概念"""
    profiles = get_agent_profiles("full")

    assert build_agent_summary(profiles).startswith("多 Agent 协同")


def test_get_model_label_keeps_standard_model_name():
    """标准审查应保持原模型名,避免任务列表出现多余噪声"""
    profiles = get_agent_profiles("standard")

    assert get_model_label("deepseek-chat", profiles) == "deepseek-chat"


def test_get_model_label_marks_multi_agent():
    """多代理审查应在模型标签中体现 multi-agent"""
    profiles = get_agent_profiles("security")

    assert get_model_label("deepseek-chat", profiles) == "deepseek-chat/multi-agent"
