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
