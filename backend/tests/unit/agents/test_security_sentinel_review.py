"""单元测试:SecuritySentinelAgent.scan_file_for_review()

mock LLM 调用,验证:
1. scan_file_for_review 返回 AgentResult.data["issues"] 为 List[Finding]
2. LLM 失败时返回 success=False
3. findings 的 source 字段为 "llm"
4. 安全评分计算正确
"""
from __future__ import annotations

import pytest

from app.agents.base import AgentContext, AgentResult
from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.ai.static_analyzer import Finding

# ============ scan_file_for_review ============

class TestScanFileForReview:
    """scan_file_for_review() 方法测试"""

    @pytest.fixture
    def agent(self):
        """构造 SecuritySentinelAgent 实例"""
        return SecuritySentinelAgent()

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
            data={
                "findings": [
                    {
                        "title": "SQL 注入",
                        "category": "注入",
                        "owasp": "A03:2021-Injection",
                        "cwe": "CWE-89",
                        "severity": "严重",
                        "line_start": 3,
                        "line_end": 3,
                        "evidence": "cursor.execute(f\"...\")",
                        "exploit_scenario": "字符串拼接构造 SQL",
                        "fix_suggestion": "参数化查询",
                        "references": ["https://owasp.org/x"],
                        "confidence": 0.9,
                    }
                ],
                "summary": "发现 1 个严重漏洞",
            },
            model="deepseek-test",
            duration_ms=100,
            tokens=500,
        )
        monkeypatch.setattr(agent, "call_json", lambda *a, **kw: mock_result)

        result = agent.scan_file_for_review(
            code='cursor.execute(f"SELECT * FROM t WHERE id={x}")\n',
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
        assert issues[0].severity == "严重"

    def test_llm_failure_returns_error(self, agent, ctx, monkeypatch):
        """LLM 失败时应返回 success=False"""
        mock_result = AgentResult(success=False, error="LLM 超时")
        monkeypatch.setattr(agent, "call_json", lambda *a, **kw: mock_result)

        result = agent.scan_file_for_review(
            code="x = 1\n",
            language="python",
            file_name="x.py",
            ctx=ctx,
        )

        assert result.success is False
        assert "LLM 超时" in result.error

    def test_non_dict_data_returns_error(self, agent, ctx, monkeypatch):
        """LLM 返回非 dict 时应返回 success=False"""
        mock_result = AgentResult(
            success=True,
            data="not a dict",
            model="test",
            duration_ms=10,
            tokens=5,
        )
        monkeypatch.setattr(agent, "call_json", lambda *a, **kw: mock_result)

        result = agent.scan_file_for_review(
            code="x = 1\n",
            language="python",
            file_name="x.py",
            ctx=ctx,
        )

        assert result.success is False

    def test_empty_findings_list(self, agent, ctx, monkeypatch):
        """LLM 返回空 findings 时应正确处理"""
        mock_result = AgentResult(
            success=True,
            data={"findings": [], "summary": "无安全问题"},
            model="test",
            duration_ms=10,
            tokens=5,
        )
        monkeypatch.setattr(agent, "call_json", lambda *a, **kw: mock_result)

        result = agent.scan_file_for_review(
            code="x = 1\n",
            language="python",
            file_name="clean.py",
            ctx=ctx,
        )

        assert result.success is True
        assert result.data["issues"] == []
        # 无 findings 时评分应为 100
        assert result.data["score"] == 100

    def test_multiple_findings_score_deduction(self, agent, ctx, monkeypatch):
        """多个 findings 应正确扣减安全评分"""
        mock_result = AgentResult(
            success=True,
            data={
                "findings": [
                    {
                        "title": "SQL 注入",
                        "category": "注入",
                        "owasp": "A03:2021-Injection",
                        "cwe": "CWE-89",
                        "severity": "严重",
                        "line_start": 1,
                        "line_end": 1,
                        "evidence": "execute",
                        "exploit_scenario": "SQLi",
                        "fix_suggestion": "参数化",
                        "confidence": 0.9,
                    },
                    {
                        "title": "XSS",
                        "category": "注入",
                        "owasp": "A03:2021-Injection",
                        "cwe": "CWE-79",
                        "severity": "高",
                        "line_start": 2,
                        "line_end": 2,
                        "evidence": "innerHTML",
                        "exploit_scenario": "XSS",
                        "fix_suggestion": "转义",
                        "confidence": 0.8,
                    },
                ],
                "summary": "发现 2 个漏洞",
            },
            model="test",
            duration_ms=20,
            tokens=10,
        )
        monkeypatch.setattr(agent, "call_json", lambda *a, **kw: mock_result)

        result = agent.scan_file_for_review(
            code="x = 1\n",
            language="python",
            file_name="multi.py",
            ctx=ctx,
        )

        assert result.success is True
        assert len(result.data["issues"]) == 2
        # 严重 -15, 高 -8 → 100 - 23 = 77
        assert result.data["score"] == 77

    def test_finding_source_always_llm(self, agent, ctx, monkeypatch):
        """scan_file_for_review 返回的 Finding source 应始终为 llm"""
        mock_result = AgentResult(
            success=True,
            data={
                "findings": [
                    {
                        "title": "测试",
                        "severity": "中",
                        "line_start": 1,
                        "line_end": 1,
                        "evidence": "x",
                        "exploit_scenario": "y",
                        "fix_suggestion": "z",
                        "confidence": 0.5,
                    }
                ],
            },
            model="test",
            duration_ms=5,
            tokens=1,
        )
        monkeypatch.setattr(agent, "call_json", lambda *a, **kw: mock_result)

        result = agent.scan_file_for_review(
            code="x = 1\n",
            language="python",
            file_name="x.py",
            ctx=ctx,
        )

        assert result.success is True
        assert all(f.source == "llm" for f in result.data["issues"])

    def test_agent_metadata(self, agent):
        """Agent 元数据应正确"""
        assert agent.name == "security_sentinel"
        assert agent.category == "security"
