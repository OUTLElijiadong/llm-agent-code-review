"""沙箱多 Agent 报告的证据与输出完整性门禁。"""

import json
from types import SimpleNamespace

import pytest

from app.agents import test_review_reporter_agent as reporter_module
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


def test_knowledge_references_compact_each_source_and_keep_last_anchor(monkeypatch) -> None:
    """三段知识都应有可追溯来源；尾段不能被合并后的 2000 字符上限抹掉。"""
    agent = ReporterAgent()
    agent._api_key = "test-key"
    hits = [
        {"doc_id": index, "title": f"方法 {index}", "owner_type": "user",
         "content": "x" * 2_100 + f"KNOWLEDGE_TAIL_{index}"}
        for index in range(1, 4)
    ]
    monkeypatch.setattr(reporter_module.agent_knowledge_service, "unified_retrieve", lambda *a, **k: hits)
    calls = []

    def role_call(system, user, ctx=None, max_tokens=None):
        calls.append(user)
        source = user.split("原始知识片段:\n", 1)[1]
        head_anchor = source[:16]
        middle_anchor = source[len(source) // 2 - 8:len(source) // 2 + 8]
        tail_anchor = source[-16:]
        source_id = user.split("source_id=", 1)[1].split("\n", 1)[0]
        digest = user.split("sha256=", 1)[1].split("\n", 1)[0]
        return AgentResult(
            success=True,
            data=json.dumps({
                "summary": f"保留中段{middle_anchor}与尾部{tail_anchor}",
                "head_quote": head_anchor,
                "middle_quote": middle_anchor,
                "tail_quote": tail_anchor,
                "covered_source_ids": [source_id],
                "source_sha256": digest,
                "coverage_complete": True,
            }),
            finish_reason="stop",
        )

    monkeypatch.setattr(agent, "_role_call", role_call)
    refs = agent._knowledge_refs(None, 1, "whitebox")

    assert len(calls) == 3
    assert all(f"KNOWLEDGE_TAIL_{index}" in refs for index in range(1, 4))
    assert all(f'"doc_id": {index}' in refs for index in range(1, 4))
    assert refs.count('"sha256"') == 3
    assert refs.count("sha256=") == 3


def test_knowledge_24k_source_keeps_tail_rule_and_every_chunk_fingerprint(monkeypatch) -> None:
    """24k 末尾规则必须进参考文本，每片都向压缩模型独立提供 ID 与哈希。"""
    agent = ReporterAgent()
    agent._api_key = "test-key"
    middle_rule = "中段规则：失败须留证"
    tail_rule = "最终规则：失败不得判通过"
    middle_at = 10_000
    content = (
        "x" * middle_at + middle_rule
        + "x" * (24_000 - middle_at - len(middle_rule) - len(tail_rule)) + tail_rule
    )
    monkeypatch.setattr(
        reporter_module.agent_knowledge_service, "unified_retrieve",
        lambda *a, **k: [{"doc_id": 9, "content": content}],
    )
    inputs = []

    def role_call(system, user, ctx=None, max_tokens=None):
        inputs.append(user)
        part = user.split("原始知识片段:\n", 1)[1]
        source_id = user.split("source_id=", 1)[1].split("\n", 1)[0]
        digest = user.split("sha256=", 1)[1].split("\n", 1)[0]
        middle = part[len(part) // 2 - 8:len(part) // 2 + 8]
        tail = part[-16:]
        return AgentResult(success=True, data=json.dumps({
            "summary": f"已提炼中段{middle}与尾部{tail}",
            "head_quote": part[:16], "middle_quote": middle, "tail_quote": tail,
            "covered_source_ids": [source_id], "source_sha256": digest,
            "coverage_complete": True,
        }, ensure_ascii=False))

    monkeypatch.setattr(agent, "_role_call", role_call)
    refs = agent._knowledge_refs(None, 1, "whitebox")

    assert len(inputs) == 6
    assert all(f"片段 {index}/6" in refs for index in range(1, 7))
    assert all("sha256=" in user for user in inputs)
    assert middle_rule in refs
    assert tail_rule in refs
    assert len(refs.split("压缩摘要（非原文）：\n", 1)[1]) <= 2_000


def test_knowledge_chunks_balance_4001_character_boundary(monkeypatch) -> None:
    """4001 字不能切出单字尾片，使首中尾引文契约无法满足。"""
    agent = ReporterAgent()
    agent._api_key = "test-key"
    tail_rule = "末尾规则不得丢失"
    content = "x" * (4_001 - len(tail_rule)) + tail_rule
    monkeypatch.setattr(
        reporter_module.agent_knowledge_service, "unified_retrieve",
        lambda *a, **k: [{"doc_id": 9, "content": content}],
    )
    parts = []

    def role_call(system, user, ctx=None, max_tokens=None):
        part = user.split("原始知识片段:\n", 1)[1]
        parts.append(part)
        source_id = user.split("source_id=", 1)[1].split("\n", 1)[0]
        digest = user.split("sha256=", 1)[1].split("\n", 1)[0]
        middle = part[len(part) // 2 - 8:len(part) // 2 + 8]
        tail = part[-16:]
        return AgentResult(success=True, data=json.dumps({
            "summary": f"已提炼中段{middle}与尾部{tail}",
            "head_quote": part[:16], "middle_quote": middle, "tail_quote": tail,
            "covered_source_ids": [source_id], "source_sha256": digest,
            "coverage_complete": True,
        }))

    monkeypatch.setattr(agent, "_role_call", role_call)
    refs = agent._knowledge_refs(None, 1, "whitebox")

    assert len(parts) == 2
    assert min(map(len, parts)) >= 2_000
    assert tail_rule in refs


def test_knowledge_rejects_fake_whole_source_coverage_without_tail_quote(monkeypatch) -> None:
    """模型只引用长来源开头却声称整条覆盖时，不能接受为完整参考。"""
    agent = ReporterAgent()
    agent._api_key = "test-key"
    content = "仅开头规则" + "x" * 12_000 + "末尾不能忽略"
    monkeypatch.setattr(
        reporter_module.agent_knowledge_service, "unified_retrieve",
        lambda *a, **k: [{"doc_id": 9, "content": content}],
    )

    def role_call(system, user, ctx=None, max_tokens=None):
        source_id = user.split("source_id=", 1)[1].split("\n", 1)[0]
        digest = user.split("sha256=", 1)[1].split("\n", 1)[0] if "sha256=" in user else ""
        return AgentResult(success=True, data=json.dumps({
            "summary": "只看开头", "anchors": ["仅开头规则"],
            "head_quote": "仅开头规则", "middle_quote": "仅开头规则",
            "tail_quote": "仅开头规则",
            "covered_source_ids": [source_id], "coverage_complete": True,
            "source_sha256": digest,
        }))

    monkeypatch.setattr(agent, "_role_call", role_call)

    with pytest.raises(reporter_module.KnowledgeReferenceError):
        agent._knowledge_refs(None, 1, "whitebox")


def test_knowledge_rejects_wrong_middle_chunk_fingerprint(monkeypatch) -> None:
    """即使首片通过，第二片回传错误指纹也不得拼成完整摘要。"""
    agent = ReporterAgent()
    agent._api_key = "test-key"
    content = "A" * 4_000 + "B" * 4_000 + "C" * 200
    monkeypatch.setattr(
        reporter_module.agent_knowledge_service, "unified_retrieve",
        lambda *a, **k: [{"doc_id": 9, "content": content}],
    )
    calls = []

    def role_call(system, user, ctx=None, max_tokens=None):
        calls.append(user)
        part = user.split("原始知识片段:\n", 1)[1]
        source_id = user.split("source_id=", 1)[1].split("\n", 1)[0]
        digest = user.split("sha256=", 1)[1].split("\n", 1)[0]
        middle = part[len(part) // 2 - 8:len(part) // 2 + 8]
        tail = part[-16:]
        return AgentResult(success=True, data=json.dumps({
            "summary": f"已提炼中段{middle}与尾部{tail}",
            "head_quote": part[:16], "middle_quote": middle, "tail_quote": tail,
            "covered_source_ids": [source_id],
            "source_sha256": "wrong" if len(calls) == 2 else digest,
            "coverage_complete": True,
        }))

    monkeypatch.setattr(agent, "_role_call", role_call)

    with pytest.raises(reporter_module.KnowledgeReferenceError):
        agent._knowledge_refs(None, 1, "whitebox")
    assert len(calls) == 2


def test_knowledge_rejects_generic_summary_with_only_anchor_fields(monkeypatch) -> None:
    """只回“已检查”并把引文放在字段中，不足以当成覆盖完整的摘要。"""
    agent = ReporterAgent()
    agent._api_key = "test-key"
    content = "开头测试规则" + "x" * 4_000 + "尾部测试规则"
    monkeypatch.setattr(
        reporter_module.agent_knowledge_service, "unified_retrieve",
        lambda *a, **k: [{"doc_id": 9, "content": content}],
    )

    def role_call(system, user, ctx=None, max_tokens=None):
        part = user.split("原始知识片段:\n", 1)[1]
        source_id = user.split("source_id=", 1)[1].split("\n", 1)[0]
        digest = user.split("sha256=", 1)[1].split("\n", 1)[0]
        middle = part[len(part) // 2 - 8:len(part) // 2 + 8]
        return AgentResult(success=True, data=json.dumps({
            "summary": "已检查", "head_quote": part[:16],
            "middle_quote": middle, "tail_quote": part[-16:],
            "covered_source_ids": [source_id], "source_sha256": digest,
            "coverage_complete": True,
        }))

    monkeypatch.setattr(agent, "_role_call", role_call)
    with pytest.raises(reporter_module.KnowledgeReferenceError):
        agent._knowledge_refs(None, 1, "whitebox")


def test_review_limits_total_knowledge_compaction_calls(monkeypatch) -> None:
    """四阶段各自检索长来源时，总压缩调用数必须受单次报告预算限制。"""
    agent = ReporterAgent()
    agent._api_key = "test-key"
    monkeypatch.setattr(
        reporter_module.agent_knowledge_service, "unified_retrieve",
        lambda *a, **k: [{"doc_id": 9, "content": "x" * 24_000}],
    )
    calls = []

    def role_call(system, user, ctx=None, max_tokens=None):
        calls.append(system)
        assert "知识压缩 Agent" in system
        part = user.split("原始知识片段:\n", 1)[1]
        source_id = user.split("source_id=", 1)[1].split("\n", 1)[0]
        digest = user.split("sha256=", 1)[1].split("\n", 1)[0]
        middle = part[len(part) // 2 - 8:len(part) // 2 + 8]
        tail = part[-16:]
        return AgentResult(success=True, data=json.dumps({
            "summary": f"已提炼中段{middle}与尾部{tail}",
            "head_quote": part[:16], "middle_quote": middle, "tail_quote": tail,
            "covered_source_ids": [source_id], "source_sha256": digest,
            "coverage_complete": True,
        }))

    monkeypatch.setattr(agent, "_role_call", role_call)
    review = agent.review(None, environment=_environment(), conclusion={"passed": True})

    assert review.success is False
    assert review.failure_kind == "knowledge_compaction_budget_exhausted"
    assert len(calls) == 12


def test_three_normal_knowledge_hits_keep_all_sources_without_model_call(monkeypatch) -> None:
    """现有 700 字切片无需额外模型调用，第三条仍保留完整原文。"""
    agent = ReporterAgent()
    hits = [{"doc_id": index, "content": "x" * 680 + f"TAIL_{index}"} for index in range(1, 4)]
    monkeypatch.setattr(reporter_module.agent_knowledge_service, "unified_retrieve", lambda *a, **k: hits)
    monkeypatch.setattr(agent, "_role_call", lambda *a, **k: pytest.fail("短切片不应再次调用模型"))

    refs = agent._knowledge_refs(None, 1, "whitebox")

    assert all(f"TAIL_{index}" in refs for index in range(1, 4))
    assert refs.count("原文：") == 3


@pytest.mark.parametrize("result", [
    AgentResult(success=False, error="模型不可用", failure_kind="upstream_error"),
    AgentResult(success=True, data=json.dumps({
        "summary": "只概括了开头", "anchors": ["x"],
        "covered_source_ids": [], "coverage_complete": True,
    })),
    AgentResult(success=True, data=json.dumps({
        "summary": "只概括了开头", "anchors": ["x"],
        "covered_source_ids": ["wrong"], "coverage_complete": False,
    })),
])
def test_knowledge_reference_compaction_failure_prevents_partial_report(monkeypatch, result) -> None:
    """任一来源未覆盖或模型失败时，不能只带成功来源继续生成完整 AI 报告。"""
    agent = ReporterAgent()
    agent._api_key = "test-key"
    monkeypatch.setattr(
        reporter_module.agent_knowledge_service, "unified_retrieve",
        lambda *a, **k: [{"doc_id": 1, "content": "x" * 3000}],
    )
    calls = []

    def role_call(system, user, ctx=None, max_tokens=None):
        calls.append(system)
        return result

    monkeypatch.setattr(agent, "_role_call", role_call)
    review = agent.review(None, environment=_environment(), conclusion={"passed": True})

    assert review.success is False
    assert review.failure_kind in {"upstream_error", "coverage_incomplete"}
    assert all("白盒测试审查" not in system for system in calls)


def test_knowledge_reference_rejects_anchor_missing_from_source(monkeypatch) -> None:
    """即使模型声称覆盖且 ID 正确，编造的原文锚点也必须被拒绝。"""
    agent = ReporterAgent()
    agent._api_key = "test-key"
    monkeypatch.setattr(
        reporter_module.agent_knowledge_service, "unified_retrieve",
        lambda *a, **k: [{"doc_id": 1, "content": "x" * 3000}],
    )

    def role_call(system, user, ctx=None, max_tokens=None):
        source_id = user.split("source_id=", 1)[1].split("\n", 1)[0]
        return AgentResult(success=True, data=json.dumps({
            "summary": "伪造引用", "anchors": ["不存在的原文"],
            "covered_source_ids": [source_id], "coverage_complete": True,
        }))

    monkeypatch.setattr(agent, "_role_call", role_call)
    review = agent.review(None, environment=_environment(), conclusion={"passed": True})

    assert review.success is False
    assert review.failure_kind == "coverage_incomplete"


def test_knowledge_retrieval_error_is_explicit_failure(monkeypatch) -> None:
    """知识服务报错不能被当成无知识命中。"""
    agent = ReporterAgent()
    agent._api_key = "test-key"

    def retrieve(*args, **kwargs):
        raise RuntimeError("数据库不可用")

    monkeypatch.setattr(reporter_module.agent_knowledge_service, "unified_retrieve", retrieve)
    monkeypatch.setattr(agent, "_role_call", lambda *a, **k: pytest.fail("不得进入报告角色"))

    review = agent.review(None, environment=_environment(), conclusion={"passed": True})

    assert review.success is False
    assert review.failure_kind == "knowledge_retrieval_failed"
