from app.ai.multi_agent import format_agent_section, get_agent_profiles
from app.ai.prompt_builder import build_prompt


def test_build_prompt_includes_agent_section():
    """Prompt 应包含当前审查代理画像,支撑多 Agent 分工"""
    profile = get_agent_profiles("security")[0]
    _, user_prompt = build_prompt(
        language="python",
        file_name="auth.py",
        code="print('hello')",
        rules=[],
        line_offset=0,
        agent_section=format_agent_section(profile),
    )

    assert "## 本轮审查代理" in user_prompt
    assert "安全审查代理" in user_prompt
    assert "重点问题类型" in user_prompt


def test_build_prompt_requires_relative_line_number():
    """Prompt 应要求模型返回分片内相对行号,避免后端重复偏移"""
    _, user_prompt = build_prompt(
        language="python",
        file_name="main.py",
        code="x = 1",
        rules=[],
        line_offset=200,
    )

    assert "返回当前代码块内的相对行号" in user_prompt
