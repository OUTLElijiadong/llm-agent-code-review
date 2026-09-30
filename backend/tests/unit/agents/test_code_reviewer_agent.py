"""单元测试:CodeReviewerAgent.execute_review()

mock LLM 调用,验证:
1. execute_review 通过 BaseAgent.call() 调用 LLM
2. 返回 AgentResult.data["issues"] 为 List[Finding]
3. LLM 失败时返回 AgentResult.success=False
4. 行号偏移(line_offset)正确换算
"""
from __future__ import annotations

import json
from types import SimpleNamespace

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

    def test_truncated_full_review_retries_disjoint_source_slices_with_relation_context(
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
            assert code not in message
            assert "当前代码单元的符号上下文" in message
            if "原文件行范围：101-102" in message:
                issue_line = 1
            elif "原文件行范围：103-104" in message:
                issue_line = 2
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
    def test_real_execute_review_path_recovers_with_bounded_source_and_absolute_lines(
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
            assert code not in message
            assert "当前代码单元的符号上下文" in message
            assert kwargs["recover_truncation"] is True
            if "原文件行范围：101-102" in message:
                line = 1
            elif "原文件行范围：103-104" in message:
                line = 2
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

    def test_input_window_rejection_retries_only_with_bounded_compressed_context(self, monkeypatch):
        agent = CodeReviewerAgent()
        code = "first()\nsecond()\n"
        bounded_context = "[来源 sha256=fixture] 已核验认证调用关系。"
        original_context = "原始上下文必须不再进入重试。" * 2_000
        monkeypatch.setattr(agent, "_recovery_window_lines", 1, raising=False)
        monkeypatch.setattr(agent, "call", lambda *_args, **_kwargs: AgentResult(
            success=False,
            error="input exceeds model context",
            failure_kind="input_exceeds_context",
            http_attempts=0,
        ))
        retry_messages = []

        def fake_call_json(message, **_kwargs):
            retry_messages.append(message)
            assert bounded_context in message
            assert original_context not in message
            return AgentResult(success=True, data={
                "summary": "当前源码范围已完成检查，没有发现可靠问题。",
                "score": 100,
                "issues": [],
            })

        monkeypatch.setattr(agent, "call_json", fake_call_json)
        result = agent.execute_review(
            code=code,
            rules=[],
            language="python",
            file_name="sample.py",
            prepared_prompts=("固定审查系统提示", "预算后的初次用户提示"),
            bounded_sections={"agent": "general", "experience": "", "context": bounded_context},
            context_section=original_context,
        )

        assert result.success is True
        assert len(retry_messages) == 2
        assert result.data["coverage"]["recovered_from_output_truncation"] is False
        assert result.data["coverage"]["reviewed_lines"] == 2

    def test_team_legacy_review_uses_bounded_chunks_and_splits_empty_response(
        self, monkeypatch,
    ):
        agent = CodeReviewerAgent()
        code = "".join(
            f"function handler_{i}() {{\n"
            + f"  const value = request_{i}.query?.input_{i} || 'default'; // validate untrusted request {i:03d} "
            + ("preserve request-to-render flow and inspect output escaping " * 3)
            + f"context {i:03d}\n"
            + f"  return render_template(value, component_{i}, options_{i}, config_{i});\n"
            + "}\n"
            for i in range(1, 118)
        ) + "// end of source file\n"
        assert len(code) > 39_000
        calls = []

        def fake_call_json(message, **kwargs):
            calls.append((message, kwargs))
            assert len(message) < 12_000
            assert code not in message
            assert kwargs["recover_truncation"] is True
            assert kwargs["max_tokens"] == 8_192
            if len(calls) == 1:
                return AgentResult(
                    success=False,
                    error="finish_reason=length",
                    failure_kind="output_truncated",
                    finish_reason="length",
                    http_attempts=1,
                )
            if len(calls) == 2:
                return AgentResult(
                    success=False,
                    error="empty stop response",
                    failure_kind="invalid_response",
                    finish_reason="stop",
                    http_attempts=1,
                )
            return AgentResult(
                success=True,
                data={"summary": "范围已审查", "score": 100, "issues": []},
                finish_reason="stop",
                http_attempts=1,
            )

        ctx = AgentContext(user_id=7, task_id=9, project_id=14, file_id=597)
        monkeypatch.setattr(agent, "call_json", fake_call_json)
        result = agent.execute(code, "检查质量", "javascript", "admin.js", ctx=ctx)

        assert result.success is True
        assert result.data["coverage"]["total_lines"] == 469
        assert result.data["coverage"]["reviewed_lines"] == 469
        assert result.data["coverage"]["context_mode"] == "lexical_symbol_index"
        assert result.data["coverage"]["coverage_scope"] == "file"
        assert result.data["coverage"]["recovered_from_output_truncation"] is True
        assert result.data["coverage"]["index_counts"]["calls"] == 0
        assert result.data["coverage"]["symbol_context_available"] is True
        assert result.data["coverage"]["ranges"] == [[1, len(code.splitlines())]]
        assert result.data["coverage"]["source_sha256"]
        assert result.tokens == {"prompt": 0, "completion": 0, "total": 0}
        assert len(calls) > 3
        assert calls[0][0] != calls[1][0]
        first_scope = calls[0][0].split("原文件行范围：", 1)[1].split("\n", 1)[0]
        second_scope = calls[1][0].split("原文件行范围：", 1)[1].split("\n", 1)[0]
        assert first_scope != second_scope

    def test_initial_empty_response_recovers_by_splitting_instead_of_replaying_source(
        self, monkeypatch,
    ):
        agent = CodeReviewerAgent()
        calls = []
        code = "one()\ntwo()\nthree()\nfour()"

        def fake_call_json(message, **kwargs):
            calls.append(message)
            if len(calls) == 1:
                return AgentResult(
                    success=False, error="empty stop response",
                    failure_kind="invalid_response", finish_reason="stop", http_attempts=1,
                )
            assert code not in message
            return AgentResult(success=True, data={"summary": "完成", "score": 100, "issues": []})

        monkeypatch.setattr(agent, "_recovery_window_lines", 2, raising=False)
        monkeypatch.setattr(agent, "call_json", fake_call_json)
        result = agent.execute(code, "检查", "python", "sample.py")

        assert result.success is True
        assert len(calls) == 3
        assert len(set(calls)) == 3

    def test_execute_review_recovery_preserves_rule_profile_experience_and_outer_context(
        self, monkeypatch,
    ):
        agent = CodeReviewerAgent()
        code = "first()\nsecond()\nthird()\nfourth()"
        rule = SimpleNamespace(
            rule_type="security",
            severity="高",
            language="javascript",
            rule_name="输出转义",
            rule_code="SEC-XSS-01",
            rule_content="将不可信内容渲染到 DOM 前必须完成上下文转义。",
        )
        calls = []
        monkeypatch.setattr(agent, "_recovery_window_lines", 2, raising=False)
        monkeypatch.setattr(agent, "call", lambda *_a, **_k: AgentResult(
            success=False, error="length", failure_kind="output_truncated",
            finish_reason="length", http_attempts=1,
        ))

        def fake_call_json(message, **kwargs):
            calls.append(message)
            assert kwargs["max_tokens"] == 8_192
            return AgentResult(success=True, data={"summary": "完成", "score": 100, "issues": []})

        monkeypatch.setattr(agent, "call_json", fake_call_json)
        result = agent.execute_review(
            code=code,
            rules=[rule],
            language="javascript",
            file_name="widget.js",
            agent_section="安全审查画像：关注 DOM sink 与数据流。",
            experience_section="历史经验：模板插值需要按 HTML 上下文转义。",
            context_section=(
                "全文件上下文：renderView 调用 sanitizeView。\n"
                "symbol_index_truncated: true"
            ),
        )

        assert result.success is True
        assert len(calls) == 2
        for prompt in calls:
            assert "SEC-XSS-01" in prompt
            assert "将不可信内容渲染到 DOM 前必须完成上下文转义。" in prompt
            assert "安全审查画像：关注 DOM sink 与数据流。" in prompt
            assert "历史经验：模板插值需要按 HTML 上下文转义。" in prompt
            assert "全文件上下文：renderView 调用 sanitizeView。" in prompt
        coverage = result.data["coverage"]
        assert coverage["coverage_scope"] == "input_chunk"
        assert coverage["upstream_context_preserved"] is True
        assert coverage["upstream_context_index_truncated"] is True
        assert coverage["context_index_truncated"] is True

    @pytest.mark.parametrize(
        "ranges",
        [
            [(0, 1, "index"), (3, 5, "index")],  # gap and out of bounds
            [(0, 2, "index"), (1, 3, "index")],  # overlap
            [(0, 4, "index")],  # endpoint out of bounds
        ],
    )
    def test_invalid_recovery_ranges_fail_before_model_calls(self, monkeypatch, ranges):
        agent = CodeReviewerAgent()
        calls = []
        monkeypatch.setattr(agent, "_recovery_ranges", lambda *_a, **_k: ranges)

        def fake_call_json(*_args, **_kwargs):
            calls.append("initial")
            return AgentResult(
                success=False,
                error="truncated",
                failure_kind="output_truncated",
                finish_reason="length",
                http_attempts=1,
            )

        monkeypatch.setattr(agent, "call_json", fake_call_json)

        result = agent.execute(
            "one()\ntwo()\nthree()", "检查", "python", "sample.py",
        )

        assert result.success is False
        assert result.failure_kind == "coverage_incomplete"
        assert result.data["coverage"]["calls"] == 1
        assert result.data["coverage"]["recovered_from_output_truncation"] is True
        assert calls == ["initial"]

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
        assert result.failure_kind == "transport_error"
        assert result.usage_log_ids == [10, 11, 12]
        assert result.failed_usage_log_ids == [10, 12]
        assert result.tokens == {"prompt": 7, "completion": 10, "total": 17}
        assert result.http_attempts == 3
        assert result.duration_ms == 23
        assert result.data["coverage"]["recovered_from_output_truncation"] is True

        from app.services.agent_mesh_dispatcher import _as_mesh_result

        mesh_result = _as_mesh_result(result, action="只读代码质量审查")
        assert mesh_result["status"] == "failed"
        assert mesh_result["retryable"] is True
        assert mesh_result["errors"][0]["code"] == "transport_error"

    def test_oversized_input_truncation_preserves_terminal_finish_reason(self, monkeypatch):
        agent = CodeReviewerAgent()
        monkeypatch.setattr(agent, "_recovery_window_chars", 6, raising=False)
        monkeypatch.setattr(agent, "_recovery_window_lines", 2, raising=False)
        monkeypatch.setattr(agent, "call_json", lambda *_a, **_k: AgentResult(
            success=False,
            error="输出达到上限",
            failure_kind="output_truncated",
            finish_reason="length",
            http_attempts=1,
        ))

        result = agent.execute("a=1\nb=2\nc=3\nd=4", "检查", "python", "large.py")

        assert result.success is False
        assert result.failure_kind == "coverage_incomplete"
        assert result.finish_reason == "length"
        assert result.data["coverage"]["recovered_from_output_truncation"] is True


def test_team_review_batches_oversized_rules_without_losing_source_or_rules(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 100_000)
    agent = CodeReviewerAgent()
    source = "value = 1\n"
    rules = [
        {"rule_code": f"R-{index:04d}", "rule_content": f"RULE-{index:04d}-" + ("preserve-all-rule-text " * 18)}
        for index in range(420)
    ]
    rule_text = json.dumps(rules, ensure_ascii=False, separators=(",", ":"))
    requests = []
    recovered = []

    def guarded_call_json(message, **kwargs):
        system_prompt = kwargs.get("system_prompt", agent._system_prompt)
        _, exceeded = agent._project_input(
            message,
            system_prompt=system_prompt,
            output_tokens=kwargs.get("max_tokens"),
        )
        assert not exceeded, "every team recovery request must pass the real BaseAgent byte guard"
        request_rules = json.loads(message.rsplit("审查规则：\n", 1)[1])
        requests.append(request_rules)
        recovered.append(kwargs.get("recover_truncation"))
        return AgentResult(
            success=True,
            data={"summary": "当前源码范围已按规则完成检查。", "score": 100, "issues": []},
            http_attempts=1,
        )

    monkeypatch.setattr(agent, "call_json", guarded_call_json)
    result = agent.execute(source, rule_text, "python", "sample.py")

    observed = [item["rule_code"] for batch in requests for item in batch]
    assert result.success
    assert len(requests) > 1
    assert all(recovered)
    assert observed == [item["rule_code"] for item in rules]
    assert result.data["coverage"]["rule_batches_completed"] == len(requests)
    assert result.data["coverage"]["reviewed_lines"] == 1
    assert result.data["coverage"]["source_sha256"]


def test_team_review_refuses_single_rule_that_cannot_be_batched_losslessly(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 100_000)
    agent = CodeReviewerAgent()
    called = []
    monkeypatch.setattr(agent, "call_json", lambda *_args, **_kwargs: called.append(True))
    rules = json.dumps([{"rule_code": "oversized", "rule_content": "x" * 150_000}])

    result = agent.execute("value = 1\n", rules, "python", "sample.py")

    assert result.success is False
    assert result.failure_kind == "input_exceeds_context"
    assert "无损分批" in result.error
    assert called == []


def test_team_review_splits_rule_batch_when_context_guard_rejects_short_source(monkeypatch):
    """The actual request envelope may exceed budget even when rule-byte batching passed."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 100_000)
    monkeypatch.setattr(settings, "deepseek_max_output_tokens", 65_536)
    agent = CodeReviewerAgent(agent_section="agent-context " * 5_800)
    source = "value = 1\n"
    rules = [
        {"rule_code": f"R-{index:02d}", "rule_content": f"rule-{index:02d} " + ("evidence " * 150)}
        for index in range(10)
    ]
    rule_text = json.dumps(rules, ensure_ascii=False, separators=(",", ":"))
    rejected_before_send = []
    accepted_rule_batches = []

    def guarded_call_json(message, **kwargs):
        system_prompt = kwargs.get("system_prompt", agent._system_prompt)
        _, exceeded = agent._project_input(
            message,
            system_prompt=system_prompt,
            output_tokens=kwargs.get("max_tokens"),
        )
        if exceeded:
            rejected_before_send.append(message)
            return AgentResult(
                success=False,
                error="输入超过模型上下文容量",
                failure_kind="input_exceeds_context",
                http_attempts=0,
            )
        accepted_rule_batches.append(json.loads(message.rsplit("审查规则：\n", 1)[1]))
        return AgentResult(
            success=True,
            data={"summary": "完整审查", "score": 100, "issues": []},
            http_attempts=1,
        )

    monkeypatch.setattr(agent, "call_json", guarded_call_json)
    result = agent.execute(source, rule_text, "python", "sample.py")

    assert result.success is True
    assert rejected_before_send, "the reproduction must hit the real BaseAgent preflight guard"
    assert all(accepted_rule_batches)
    assert [rule for batch in accepted_rule_batches for rule in batch] == rules
    assert result.data["coverage"]["stage"] == "complete"
    assert result.data["coverage"]["rule_batches_completed"] == len(accepted_rule_batches)


def test_team_review_fails_closed_when_system_context_alone_exceeds_budget(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 100_000)
    agent = CodeReviewerAgent(agent_section="agent-context " * 9_000)
    rejected_before_send = []
    sent = []

    def guarded_call_json(message, **kwargs):
        system_prompt = kwargs.get("system_prompt", agent._system_prompt)
        _, exceeded = agent._project_input(
            message,
            system_prompt=system_prompt,
            output_tokens=kwargs.get("max_tokens"),
        )
        if exceeded:
            rejected_before_send.append(message)
            return AgentResult(
                success=False,
                error="输入超过模型上下文容量",
                failure_kind="input_exceeds_context",
                http_attempts=0,
            )
        sent.append(message)
        return AgentResult(success=True, data={"summary": "完整审查", "score": 100, "issues": []}, http_attempts=1)

    monkeypatch.setattr(agent, "call_json", guarded_call_json)

    result = agent.execute("value = 1\n", '[{"rule_code":"R1","rule_content":"check"}]', "python", "sample.py")

    assert result.success is False
    assert result.failure_kind == "input_exceeds_context"
    assert "覆盖不完整" in result.error
    assert result.data["coverage"]["stage"] == "failed"
    assert result.data["coverage"].get("ranges", []) == []
    assert result.data["coverage"]["input_rejected_before_http"] is True
    assert rejected_before_send
    assert sent == []


def test_team_review_failure_keeps_truncation_flag_from_failed_rule_batch(monkeypatch):
    agent = CodeReviewerAgent()
    monkeypatch.setattr(agent, "_recovery_window_chars", 6, raising=False)
    monkeypatch.setattr(agent, "_recovery_window_lines", 1, raising=False)
    monkeypatch.setattr(agent, "call_json", lambda *_args, **_kwargs: AgentResult(
        success=False,
        error="finish_reason=length",
        failure_kind="output_truncated",
        finish_reason="length",
        http_attempts=1,
    ))

    result = agent.execute("a()\nb()", "检查", "python", "sample.py")

    assert result.success is False
    assert result.data["coverage"]["stage"] == "failed"
    assert result.data["coverage"]["recovered_from_output_truncation"] is True
