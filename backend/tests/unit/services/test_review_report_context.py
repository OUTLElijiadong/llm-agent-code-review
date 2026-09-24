"""沙箱多 Agent 报告的证据与输出完整性门禁。"""

import json
from types import SimpleNamespace

import pytest

from app.agents.base import AgentResult
from app.agents.test_review_reporter_agent import TestReviewReporterAgent as ReporterAgent


def _agent(monkeypatch):
    agent = ReporterAgent()
    agent._api_key = "test-key"
    monkeypatch.setattr(agent, "_knowledge_refs", lambda *args, **kwargs: "")
    return agent


def _environment():
    return SimpleNamespace(
        public_id="env_real", purpose="test", test_mode="combined",
        language="python", status="succeeded", project_id=1, owner_id=1,
    )


def test_redaction_keeps_non_secret_tail_and_all_rows() -> None:
    """脱敏职责不应顺手裁掉第 51 条发现或日志尾部。"""
    source = {"password": "secret", "rows": list(range(60)), "log": "x" * 5000 + "尾部异常证据"}

    redacted = ReporterAgent._redact(source)

    assert redacted["password"] == "***"
    assert len(redacted["rows"]) == 60
    assert redacted["log"].endswith("尾部异常证据")


def test_redaction_masks_free_text_credentials_without_cutting_log_tail() -> None:
    """放开全文证据后仍必须清除日志正文中的凭据。"""
    source = {"log": "请求 Authorization: Bearer abcdefghijklmnop\n" + "x" * 5000 + "末尾错误"}

    redacted = ReporterAgent._redact(source)

    assert "abcdefghijklmnop" not in redacted["log"]
    assert redacted["log"].endswith("末尾错误")


def test_report_role_receives_full_evidence_and_prior_role_tail(monkeypatch) -> None:
    """后续模型不能只看见被前 4K/16K 字符裁剪的证据。"""
    agent = _agent(monkeypatch)
    calls = []

    def role_call(system, user, ctx=None, max_tokens=None):
        calls.append((system, user))
        if "白盒测试审查" in system:
            return AgentResult(success=True, data="白盒" + "x" * 17_000 + "WHITE_TAIL", finish_reason="stop")
        if "报告 Agent" in system:
            return AgentResult(success=True, data="## 总体结论\n完整", finish_reason="stop")
        if "编排 Agent" in system:
            return AgentResult(success=True, data={"extra_roles": []}, finish_reason="stop")
        return AgentResult(success=True, data="正常", finish_reason="stop")

    monkeypatch.setattr(agent, "_role_call", role_call)
    result = agent.review(
        None,
        environment=_environment(),
        conclusion={
            "passed": False,
            "evidence": {"worker_result": {"logs": {"text": "x" * 5000 + "LATE_FAILURE_SIGNAL"}}},
        },
    )

    assert result.success is True
    report_inputs = [user for system, user in calls if "报告 Agent" in system]
    assert len(report_inputs) == 1
    assert "LATE_FAILURE_SIGNAL" in report_inputs[0]
    assert "WHITE_TAIL" in report_inputs[0]


def test_length_limited_report_is_not_accepted_as_shortened_success(monkeypatch) -> None:
    """七段报告 length 后不能靠前 2K 字/15 问题重试假装覆盖完整。"""
    agent = _agent(monkeypatch)
    report_calls = []

    def role_call(system, user, ctx=None, max_tokens=None):
        if "报告 Agent" in system:
            report_calls.append(user)
            if len(report_calls) == 1:
                return AgentResult(
                    success=False, error="length", failure_kind="output_truncated",
                    finish_reason="length",
                )
            return AgentResult(success=True, data="## 总体结论\n摘要", finish_reason="stop")
        if "编排 Agent" in system:
            return AgentResult(success=True, data={"extra_roles": []}, finish_reason="stop")
        return AgentResult(success=True, data="正常", finish_reason="stop")

    monkeypatch.setattr(agent, "_role_call", role_call)
    result = agent.review(None, environment=_environment(), conclusion={"passed": True})

    assert result.success is False
    assert result.failure_kind == "output_truncated"
    assert len(report_calls) == 1


def test_required_role_context_failure_prevents_partial_ai_report(monkeypatch) -> None:
    """前置审查角色未看完证据时应让沙箱走确定性报告兜底。"""
    agent = _agent(monkeypatch)
    calls = []

    def role_call(system, user, ctx=None, max_tokens=None):
        calls.append(system)
        if "白盒测试审查" in system:
            return AgentResult(
                success=False,
                error="输入超过模型上下文容量",
                failure_kind="input_exceeds_context",
            )
        return AgentResult(success=True, data="正常", finish_reason="stop")

    monkeypatch.setattr(agent, "_role_call", role_call)
    result = agent.review(None, environment=_environment(), conclusion={"passed": True})

    assert result.success is False
    assert result.failure_kind == "input_exceeds_context"
    assert len(calls) == 1


def test_recon_facts_receive_the_same_secret_redaction(monkeypatch) -> None:
    """重复注入的 recon_facts 不得绕过已脱敏 conclusion。"""
    agent = _agent(monkeypatch)
    requests = []

    def role_call(system, user, ctx=None, max_tokens=None):
        requests.append(user)
        if "编排 Agent" in system:
            return AgentResult(success=True, data={"extra_roles": []}, finish_reason="stop")
        return AgentResult(success=True, data="## 总体结论\n正常", finish_reason="stop")

    monkeypatch.setattr(agent, "_role_call", role_call)
    result = agent.review(
        None,
        environment=_environment(),
        conclusion={
            "auto_test_chain": [{"facts": {"api_key": "supersecret", "exit_code": 1}}],
        },
    )

    assert result.success is True
    assert all("supersecret" not in request for request in requests)
    assert any('"api_key": "***"' in request for request in requests)


def test_large_evidence_is_summarized_by_complete_source_chunks(monkeypatch) -> None:
    """超长沙箱证据应逐片压缩，末尾源片仍要进入最终报告。"""
    agent = _agent(monkeypatch)
    calls = []

    def role_call(system, user, ctx=None, max_tokens=None):
        calls.append((system, user))
        if "证据压缩 Agent" in system:
            source = user.split("原始片段:\n", 1)[1]
            quote = source[-40:]
            return AgentResult(
                success=True,
                data=json.dumps({
                    "summary": f"该段证据末尾：{quote}",
                    "anchors": [quote],
                    "coverage_complete": True,
                }, ensure_ascii=False),
                finish_reason="stop",
            )
        if "编排 Agent" in system:
            return AgentResult(success=True, data={"extra_roles": []}, finish_reason="stop")
        return AgentResult(success=True, data="## 总体结论\n已检查", finish_reason="stop")

    monkeypatch.setattr(agent, "_role_call", role_call)
    result = agent.review(
        None,
        environment=_environment(),
        conclusion={"evidence": {"worker_result": {"logs": {"text": "x" * 70_000 + "LATE_SOURCE"}}}},
    )

    assert result.success is True
    compressed = [user for system, user in calls if "证据压缩 Agent" in system]
    assert len(compressed) >= 3
    report_input = [user for system, user in calls if "报告 Agent" in system][0]
    assert "LATE_SOURCE" in report_input
    assert "source=evidence:" in report_input
    assert "sha256=" in report_input


@pytest.mark.parametrize("summary", [
    {"summary": "摘要", "anchors": ["not-in-source"], "coverage_complete": True},
    {"summary": "摘要", "anchors": ["x"], "coverage_complete": False},
])
def test_evidence_compaction_rejects_unverified_or_incomplete_chunks(monkeypatch, summary) -> None:
    """压缩模型的无源引文或缺失声明应阻止报告成功。"""
    agent = _agent(monkeypatch)
    monkeypatch.setattr(
        agent,
        "_role_call",
        lambda *args, **kwargs: AgentResult(
            success=True, data=json.dumps(summary), finish_reason="stop",
        ),
    )

    result = agent._compact_evidence("x" * 61_000, None)

    assert result.success is False
    assert result.failure_kind in {"invalid_schema", "coverage_incomplete"}


def test_evidence_over_batch_budget_fails_before_any_model_call(monkeypatch) -> None:
    """批次数耗尽应明确失败，不发送前缀并谎称已检查。"""
    agent = _agent(monkeypatch)

    def forbidden_model(*args, **kwargs):
        raise AssertionError("不应调用模型")

    monkeypatch.setattr(
        agent,
        "_role_call",
        forbidden_model,
    )

    result = agent._compact_evidence("x" * (24_000 * 32 + 1), None)

    assert result.success is False
    assert result.failure_kind == "input_exceeds_context"
