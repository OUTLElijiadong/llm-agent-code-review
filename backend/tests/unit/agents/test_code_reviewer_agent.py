"""单元测试:CodeReviewerAgent.execute_review()

mock LLM 调用,验证:
1. execute_review 通过 BaseAgent.call() 调用 LLM
2. 返回 AgentResult.data["issues"] 为 List[Finding]
3. LLM 失败时返回 AgentResult.success=False
4. 行号偏移(line_offset)正确换算
"""
from __future__ import annotations

import json

import pytest

from app.agents.base import AgentContext, AgentResult
from app.agents.review_agent import CodeReviewerAgent, _issue_to_finding
from app.ai.result_parser import Issue
from app.ai.static_analyzer import Finding

# ============ _issue_to_finding ============

class TestIssueToFinding:
    """_issue_to_finding() 转换函数测试"""

    def test_basic_conversion(self):
        """基础字段应正确转换"""
        issue = Issue(
            line_number=5,
            end_line=5,
            issue_type="安全漏洞",
            severity="高",
            title="SQL 注入",
            description="字符串拼接 SQL",
            suggestion="参数化查询",
            owasp="A03:2021-Injection",
            cwe="CWE-89",
            evidence="cursor.execute(f\"...\")",
            confidence=0.9,
        )
        finding = _issue_to_finding(issue, line_offset=0)
        assert isinstance(finding, Finding)
        assert finding.line_number == 5
        assert finding.cwe == "CWE-89"
        assert finding.source == "llm"
        assert finding.confidence == 0.9

    def test_line_offset_applied(self):
        """line_offset 应正确叠加到绝对行号"""
        issue = Issue(
            line_number=3,
            end_line=4,
            issue_type="安全漏洞",
            severity="高",
            title="XSS",
            description="innerHTML 拼接",
            suggestion="转义输出",
            owasp="A03:2021-Injection",
            cwe="CWE-79",
            evidence="el.innerHTML = x",
            confidence=0.85,
        )
        finding = _issue_to_finding(issue, line_offset=200)
        assert finding.line_number == 203
        assert finding.end_line == 204

    def test_zero_line_stays_zero(self):
        """文件级问题(line_number=0)不应被 offset 污染"""
        issue = Issue(
            line_number=0,
            end_line=None,
            issue_type="安全漏洞",
            severity="中",
            title="配置缺失",
            description="缺少安全头",
            suggestion="添加 HSTS",
            owasp="A05:2021-Security Misconfiguration",
            cwe="CWE-693",
            evidence="",
            confidence=0.8,
        )
        finding = _issue_to_finding(issue, line_offset=500)
        assert finding.line_number == 0


# ============ execute_review ============

class TestExecuteReview:
    """execute_review() 方法测试"""

    @pytest.fixture
    def agent(self):
        """构造 CodeReviewerAgent 实例"""
        return CodeReviewerAgent()

    @pytest.fixture
    def ctx(self):
        """构造 AgentContext"""
        return AgentContext(
            user_id=1,
            task_id=1,
            project_id=1,
            file_id=1,
            extra={"trace_id": "test-trace"},
        )

    def test_success_returns_findings(self, agent, ctx, monkeypatch):
        """LLM 成功时应返回 List[Finding]"""
        mock_result = AgentResult(
            success=True,
            data=json.dumps({
                "issues": [
                    {
                        "severity": "高",
                        "issue_type": "安全漏洞",
                        "line_number": 3,
                        "title": "SQL 注入",
                        "description": "字符串拼接 SQL",
                        "suggestion": "参数化查询",
                        "owasp": "A03:2021-Injection",
                        "cwe": "CWE-89",
                        "evidence": "execute(f\"...\")",
                        "confidence": 0.9,
                    }
                ],
                "summary": "发现 1 个问题",
                "score": 70,
            }),
            model="deepseek-test",
            duration_ms=100,
            tokens=500,
        )
        monkeypatch.setattr(agent, "call", lambda *a, **kw: mock_result)

        result = agent.execute_review(
            code="cursor.execute(f\"SELECT * FROM t WHERE id={x}\")\n",
            rules=[],
            language="python",
            file_name="sqli.py",
            line_offset=0,
            ctx=ctx,
        )

        assert result.success is True
        issues = result.data["issues"]
        assert len(issues) == 1
        assert isinstance(issues[0], Finding)
        assert issues[0].cwe == "CWE-89"
        assert issues[0].source == "llm"

    def test_llm_failure_returns_error(self, agent, ctx, monkeypatch):
        """LLM 失败时应返回 success=False"""
        mock_result = AgentResult(success=False, error="LLM 超时")
        monkeypatch.setattr(agent, "call", lambda *a, **kw: mock_result)

        result = agent.execute_review(
            code="x = 1\n",
            rules=[],
            language="python",
            file_name="x.py",
            ctx=ctx,
        )

        assert result.success is False
        assert "LLM 超时" in result.error

    def test_parse_failure_returns_error(self, agent, ctx, monkeypatch):
        """LLM 返回无法解析的 JSON 时应返回 success=False"""
        mock_result = AgentResult(
            success=True,
            data="not a valid review result",
            model="test",
            duration_ms=50,
            tokens=10,
        )
        monkeypatch.setattr(agent, "call", lambda *a, **kw: mock_result)

        result = agent.execute_review(
            code="x = 1\n",
            rules=[],
            language="python",
            file_name="x.py",
            ctx=ctx,
        )

        assert result.success is False

    def test_line_offset_in_findings(self, agent, ctx, monkeypatch):
        """line_offset 应正确反映到 Finding 的绝对行号"""
        mock_result = AgentResult(
            success=True,
            data=json.dumps({
                "issues": [
                    {
                        "severity": "高",
                        "issue_type": "安全漏洞",
                        "line_number": 5,
                        "title": "XSS",
                        "description": "innerHTML",
                        "suggestion": "转义",
                        "owasp": "A03:2021-Injection",
                        "cwe": "CWE-79",
                        "evidence": "innerHTML",
                        "confidence": 0.8,
                    }
                ],
                "summary": "ok",
                "score": 80,
            }),
            model="test",
            duration_ms=10,
            tokens=5,
        )
        monkeypatch.setattr(agent, "call", lambda *a, **kw: mock_result)

        result = agent.execute_review(
            code="x = 1\n",
            rules=[],
            language="javascript",
            file_name="x.js",
            line_offset=100,
            ctx=ctx,
        )

        assert result.success is True
        assert result.data["issues"][0].line_number == 105

    def test_empty_issues_list(self, agent, ctx, monkeypatch):
        """LLM 返回空 issues 时应正确处理"""
        mock_result = AgentResult(
            success=True,
            data=json.dumps({"issues": [], "summary": "无问题", "score": 100}),
            model="test",
            duration_ms=10,
            tokens=5,
        )
        monkeypatch.setattr(agent, "call", lambda *a, **kw: mock_result)

        result = agent.execute_review(
            code="x = 1\n",
            rules=[],
            language="python",
            file_name="clean.py",
            ctx=ctx,
        )

        assert result.success is True
        assert result.data["issues"] == []
        assert result.data["score"] == 100

    def test_agent_metadata(self, agent):
        """Agent 元数据应正确"""
        assert agent.name == "code_reviewer"
        assert agent.category == "reviewer"
        assert "代码审查" in agent.skills


class TestLegacyExecuteOutputRecovery:
    """团队内置代码审查的输出截断恢复必须保持源码覆盖。"""

    @pytest.fixture
    def agent(self):
        return CodeReviewerAgent()

    def test_truncated_full_review_retries_disjoint_scopes_with_full_file_context(
        self, agent, monkeypatch,
    ):
        code = "first()\nsecond()\nthird()\nfourth()"
        calls = []

        def fake_call_json(message, **_kwargs):
            calls.append(message)
            if len(calls) == 1:
                return AgentResult(
                    success=False,
                    error="模型输出被截断",
                    failure_kind="output_truncated",
                    finish_reason="length",
                )
            assert code in message
            if "原文件行范围：101-102" in message:
                issue_line = 1
            elif "原文件行范围：103-104" in message:
                issue_line = 4
            else:
                pytest.fail("恢复请求没有声明精确且连续的原文件行范围")
            return AgentResult(success=True, data={
                "summary": "片段审查完成",
                "score": 1,
                "issues": [{
                    "line_number": issue_line,
                    "issue_type": "潜在Bug",
                    "severity": "高",
                    "title": f"问题行 {issue_line}",
                    "description": "此处存在可复现的边界处理缺陷。",
                    "suggestion": "补充边界检查并覆盖对应分支。",
                }],
            })

        monkeypatch.setattr(agent, "_recovery_window_lines", 2, raising=False)
        monkeypatch.setattr(agent, "call_json", fake_call_json)

        result = agent.execute(code, "检查缺陷", "python", "sample.py", line_offset=100)

        assert result.success is True
        assert len(calls) == 3
        assert result.data["coverage"]["total_lines"] == 4
        assert result.data["coverage"]["reviewed_lines"] == 4
        assert [item["line_number"] for item in result.data["issues"]] == [101, 104]
        assert result.data["score"] == 84

    def test_irreducible_truncated_line_fails_closed_without_partial_success(
        self, agent, monkeypatch,
    ):
        calls = []

        def fake_call_json(_message, **_kwargs):
            calls.append(1)
            return AgentResult(
                success=False,
                error="模型输出被截断",
                failure_kind="output_truncated",
                finish_reason="length",
            )

        monkeypatch.setattr(agent, "_recovery_window_lines", 1, raising=False)
        monkeypatch.setattr(agent, "call_json", fake_call_json)

        result = agent.execute("x = compute()", "检查缺陷", "python", "sample.py")

        assert result.success is False
        assert result.failure_kind == "coverage_incomplete"
        assert "覆盖不完整" in result.error
        assert len(calls) == 2  # 原请求和一次单行焦点请求，不整项原样重试


class TestExecuteReviewOutputRecovery:
    def test_real_execute_review_path_recovers_with_full_source_and_absolute_lines(
        self, monkeypatch,
    ):
        agent = CodeReviewerAgent()
        ctx = AgentContext(user_id=1, task_id=2, project_id=3, file_id=4)
        code = "first()\nsecond()\nthird()\nfourth()"
        calls = []

        monkeypatch.setattr(agent, "_recovery_window_lines", 2, raising=False)
        monkeypatch.setattr(agent, "call", lambda *_a, **_k: AgentResult(
            success=False, error="length", failure_kind="output_truncated",
            finish_reason="length", tokens={"prompt": 2, "completion": 3, "total": 5},
            usage_log_ids=[10], http_attempts=1, duration_ms=7,
        ))

        def fake_call_json(message, **kwargs):
            calls.append((message, kwargs))
            assert code in message
            assert kwargs["recover_truncation"] is True
            if "原文件行范围：101-102" in message:
                line = 1
            elif "原文件行范围：103-104" in message:
                line = 4
            else:
                pytest.fail("恢复请求未携带主路径的精确源码范围")
            return AgentResult(success=True, data={
                "summary": "该范围已审查完成",
                "issues": [{
                    "line_number": line,
                    "end_line": line,
                    "issue_type": "潜在Bug",
                    "severity": "高",
                    "title": "范围内问题",
                    "description": "此处对无效状态的处理不完整。",
                    "suggestion": "增加对应状态校验并补充回归用例。",
                }],
            }, tokens={"prompt": 11, "completion": 13, "total": 24},
                usage_log_ids=[len(calls) + 10], http_attempts=1, duration_ms=5)

        monkeypatch.setattr(agent, "call_json", fake_call_json)
        result = agent.execute_review(
            code=code,
            rules=[],
            language="python",
            file_name="sample.py",
            line_offset=100,
            ctx=ctx,
        )

        assert result.success is True
        assert [item.line_number for item in result.data["issues"]] == [101, 104]
        assert [item.end_line for item in result.data["issues"]] == [101, 104]
        assert result.data["coverage"]["reviewed_lines"] == 4
        assert result.usage_log_ids == [10, 11, 12]
        assert result.failed_usage_log_ids == [10]
        assert result.tokens == {"prompt": 24, "completion": 29, "total": 53}
        assert result.http_attempts == 3
        assert result.duration_ms == 17

    def test_recovery_rejects_end_line_outside_focus(self, monkeypatch):
        agent = CodeReviewerAgent()
        monkeypatch.setattr(agent, "_recovery_window_lines", 2, raising=False)
        monkeypatch.setattr(agent, "call", lambda *_a, **_k: AgentResult(
            success=False, error="length", failure_kind="output_truncated",
            finish_reason="length",
        ))
        monkeypatch.setattr(agent, "call_json", lambda *_a, **_k: AgentResult(success=True, data={
            "summary": "范围审查完成",
            "issues": [{
                "line_number": 1,
                "end_line": 3,
                "issue_type": "潜在Bug",
                "severity": "中",
                "title": "结束行越界",
                "description": "结束位置超过当前源码焦点范围。",
                "suggestion": "限制问题位置到当前已覆盖源码范围。",
            }],
        }))

        result = agent.execute_review(
            code="one()\ntwo()\nthree()\nfour()",
            rules=[], language="python", file_name="sample.py",
        )

        assert result.success is False
        assert result.failure_kind == "coverage_incomplete"
        assert "行号" in result.error or "范围" in result.error

    def test_recovery_failure_preserves_all_attempt_usage(self, monkeypatch):
        agent = CodeReviewerAgent()
        code = "one()\ntwo()\nthree()\nfour()"
        monkeypatch.setattr(agent, "_recovery_window_lines", 2, raising=False)
        monkeypatch.setattr(agent, "call", lambda *_a, **_k: AgentResult(
            success=False, error="length", failure_kind="output_truncated",
            finish_reason="length", tokens={"prompt": 1, "completion": 2, "total": 3},
            usage_log_ids=[10], http_attempts=1, duration_ms=7,
        ))
        calls = []

        def fake_call_json(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                return AgentResult(success=True, data={"summary": "已审查", "issues": []},
                    tokens={"prompt": 2, "completion": 3, "total": 5},
                    usage_log_ids=[11], http_attempts=1, duration_ms=5)
            return AgentResult(success=False, error="上游断连", failure_kind="transport_error",
                tokens={"prompt": 4, "completion": 5, "total": 9},
                usage_log_ids=[12], http_attempts=1, duration_ms=11)

        monkeypatch.setattr(agent, "call_json", fake_call_json)
        result = agent.execute_review(code=code, rules=[], language="python", file_name="a.py")

        assert result.success is False
        assert result.failure_kind == "coverage_incomplete"
        assert result.usage_log_ids == [10, 11, 12]
        assert result.failed_usage_log_ids == [10, 12]
        assert result.tokens == {"prompt": 7, "completion": 10, "total": 17}
        assert result.http_attempts == 3
        assert result.duration_ms == 23
