"""单元测试 (v2.0): AiPromptAgent 模板渲染 + 脱敏"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agents.ai_prompt_agent import (
    AiPromptAgent,
    _format_lines,
    _redact,
)
from app.agents.base import AgentResult


def test_format_lines_handles_none_and_range():
    assert _format_lines(None, None) == "文件级"
    assert _format_lines(0, 0) == "文件级"
    assert _format_lines(12, 12) == "L12"
    assert _format_lines(12, 18) == "L12-L18"


def test_redact_replaces_common_secrets():
    src = 'api_key = "sk-abcdef12345"'
    out = _redact(src)
    assert "sk-abcdef12345" not in out
    assert "<REDACTED>" in out


def test_redact_keeps_innocent_strings():
    src = 'msg = "hello world"'
    assert _redact(src) == src


def test_supported_tools_contains_expected_targets():
    agent = AiPromptAgent()
    tools = {t["value"] for t in agent.list_supported_tools()}
    assert {"generic", "cursor", "copilot", "chatgpt", "claude_code"}.issubset(tools)


def test_render_template_contains_required_fields():
    """模板必须包含文件/行号/问题/修复要求/上下文"""
    agent = AiPromptAgent()

    class _Issue:
        severity = "严重"
        issue_type = "安全漏洞"
        description = "SQL 拼接未参数化"
        suggestion = "改用 ORM 参数绑定"

    rendered = agent._render_template(
        target_tool="cursor",
        issue=_Issue(),
        file_path="backend/app/crud.py",
        language="python",
        snippet="  45| query = f\"SELECT * FROM u WHERE id={uid}\"",
        start=45,
        end=45,
    )

    assert "文件:" in rendered
    assert "行号:" in rendered
    assert "L45" in rendered
    assert "修复要求:" in rendered
    assert "Cursor" in rendered  # tool footer 应出现
    assert "SQL 拼接未参数化" in rendered


class _AggIssue:
    """聚合提示词测试用的轻量问题对象(file_id=None 时不触碰 DB)"""
    def __init__(self, iid, severity, issue_type, line, title, file_name):
        self.id = iid
        self.file_id = None
        self.file_name = file_name
        self.line_number = line
        self.end_line = None
        self.issue_type = issue_type
        self.severity = severity
        self.title = title
        self.description = f"{title} 的描述"
        self.suggestion = f"{title} 的建议"


def test_build_aggregate_groups_orders_and_marks_kind():
    """一键修复全部: 标记 kind、按严重度排序、按文件分组、负数 id、含全部问题"""
    agent = AiPromptAgent()
    issues = [
        _AggIssue(1, "中", "性能问题", 40, "N+1 查询", "dao/user.py"),
        _AggIssue(2, "严重", "潜在Bug", 7, "空指针", "svc/order.py"),
        _AggIssue(3, "高", "安全漏洞", 12, "SQL 注入", "dao/user.py"),
    ]
    agg = agent._build_aggregate(issues, "cursor", scope_label="订单模块", agg_id=-99)

    assert agg["kind"] == "aggregate"
    assert agg["issue_id"] == -99           # 负数,避免与真实 issue_id 冲突
    assert agg["severity"] == "严重"         # 取最高严重度
    assert "3 个问题" in agg["title"]
    text = agg["prompt_text"]
    # 三个问题都在
    for kw in ("N+1 查询", "空指针", "SQL 注入"):
        assert kw in text
    # 按文件分组
    assert "### 文件: svc/order.py" in text
    assert "### 文件: dao/user.py" in text
    # 严重度排序: 严重的「空指针」应排在「N+1 查询」之前
    assert text.index("空指针") < text.index("N+1 查询")
    assert "修复要求:" in text


def test_build_aggregate_keeps_every_snippet_when_many_issues(monkeypatch):
    """问题数 > 15 时仍保留每条代码证据。"""
    agent = AiPromptAgent()
    issues = [
        _AggIssue(i, "低", "代码规范", i, f"问题{i}", "a.py")
        for i in range(1, 20)
    ]
    monkeypatch.setattr(
        agent,
        "_extract_context",
        lambda _file_id, line, **_kw: (f"代码证据_{line}", line, line, None),
    )
    agg = agent._build_aggregate(issues, "generic", scope_label="任务A", agg_id=-1)
    assert "19 个问题" in agg["title"]
    assert agg["prompt_text"].count("上下文:") == 19
    assert "代码证据_19" in agg["prompt_text"]


def test_llm_polish_missing_source_falls_back_to_full_draft(monkeypatch):
    agent = AiPromptAgent()
    draft = "文件: a.py\n行号: L7\n严重度: 高\n问题类型: 潜在Bug\n上下文代码:\n```python\n完整源码末尾\n```\n"
    monkeypatch.setattr(
        agent,
        "call",
        lambda _message: AgentResult(success=True, data="文件: a.py\n行号: L7\n严重度: 高\n问题类型: 潜在Bug"),
    )
    polished, _result = agent._polish_with_llm(draft, "generic")
    assert polished == draft


def test_llm_polish_missing_problem_description_falls_back(monkeypatch):
    agent = AiPromptAgent()
    draft = "文件: a.py\n行号: L7\n严重度: 高\n问题类型: 潜在Bug\n问题描述:\n遗漏边界条件\n\n修复要求:\n补测试"
    monkeypatch.setattr(
        agent,
        "call",
        lambda _message: AgentResult(
            success=True, data="文件: a.py\n行号: L7\n严重度: 高\n问题类型: 潜在Bug\n修复要求:\n补测试",
        ),
    )
    polished, _result = agent._polish_with_llm(draft, "generic")
    assert polished == draft


def test_llm_polish_over_context_keeps_complete_template(monkeypatch):
    agent = AiPromptAgent()
    draft = "文件: a.py\n问题描述:\n" + "证据" * 3000 + "\n尾部关键事实"
    monkeypatch.setattr(
        agent,
        "call",
        lambda _message: AgentResult(
            success=False,
            error="input_exceeds_context",
            failure_kind="input_exceeds_context",
        ),
    )

    polished, result = agent._polish_with_llm(draft, "generic")

    assert result.failure_kind == "input_exceeds_context"
    assert polished == draft
    assert polished.endswith("尾部关键事实")


def test_task_prompt_generation_does_not_silently_limit_to_fifty(monkeypatch):
    agent = AiPromptAgent()
    issues = [
        SimpleNamespace(id=index, task_id=9, severity="低", line_number=index)
        for index in range(1, 62)
    ]
    query = MagicMock()
    query.order_by.return_value.all.return_value = issues
    db = MagicMock()
    db.get.return_value = SimpleNamespace(project_id=3, task_name="完整任务")
    db.query.return_value.filter.return_value = query
    agent._db = db
    monkeypatch.setattr(agent, "_authz_project", lambda *_args: None)
    monkeypatch.setattr(agent, "_build_for_issue", lambda issue, *_args: {"issue_id": issue.id, "tokens": {}})
    monkeypatch.setattr(agent, "_build_aggregate", lambda group, *_args, **_kwargs: {"issue_id": -9})

    result = agent.execute_for_task(9, use_llm=False)

    assert result.success
    assert len(result.data["prompts"]) == 61
    assert result.data["prompts"][-1]["issue_id"] == 61
    query.order_by.return_value.limit.assert_not_called()


class _FakeDb:
    """为上下文抽取测试提供最小 get() 接口。"""

    def __init__(self, file):
        """保存测试构造的文件对象。"""
        self.file = file

    def get(self, _model, _file_id):
        """返回测试文件。

        Args:
            _model: SQLAlchemy 模型类型，本测试不使用。
            _file_id: 文件 ID，本测试不使用。

        Returns:
            测试构造的文件对象。
        """
        return self.file


def test_extract_context_omits_binary_base64_payload():
    """二进制 Base64 内容不得进入提示词上下文。"""
    agent = AiPromptAgent()
    agent._db = _FakeDb(SimpleNamespace(
        content="[BINARY:BASE64:]aGVsbG8=",
        language="binary",
    ))

    snippet, start, end, _file = agent._extract_context(1, 1)

    assert snippet == ""
    assert start == 0
    assert end == 0


def test_extract_context_keeps_oversized_text_line():
    """超长文本行进入完整提示词；模型无法润色时应回退模板而非丢代码。"""
    agent = AiPromptAgent()
    full_line = "x" * 5000 + "END_OF_SOURCE_LINE"
    agent._db = _FakeDb(SimpleNamespace(
        content=full_line,
        language="javascript",
    ))

    snippet, start, end, _file = agent._extract_context(1, 1)

    assert snippet.endswith("END_OF_SOURCE_LINE")
    assert full_line in snippet
    assert start == 1
    assert end == 1
