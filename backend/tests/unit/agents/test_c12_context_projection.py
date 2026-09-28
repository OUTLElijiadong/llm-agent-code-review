"""C12：隔离验证原始内容到实际模型投影，不调用外部模型。"""

import json
import re

import pytest

from app.agents.base import AgentResult
from app.agents.security_sentinel_agent import SecuritySentinelAgent, _ProjectAuditPart
from app.ai import discussion_orchestrator as discussion
from app.models.code_file import CodeFile


@pytest.mark.parametrize("body", ["长发言证据" * 600, "source_tail()\r\n" * 600],
                         ids=["chinese", "code-crlf"])
@pytest.mark.parametrize("label", ["【审查员·第1轮】", ""], ids=["speaker", "no-label"])
def test_roundtable_split_history_compresses_body_instead_of_repeating_it_as_label(
    monkeypatch, body, label,
):
    monkeypatch.setattr(discussion.settings, "deepseek_context_window_tokens", 6000)
    source = label + body + "最后约束必须保留"
    source_parts = []

    def compact(_agent, *_args, **kwargs):
        first_level = "上一层摘要" not in kwargs["system_prompt"]
        entries = []
        for source_id, text in re.findall(
            r"【来源 ([^】]+)】\n(.*?)(?=\n\n【来源 |\Z)",
            kwargs["user_prompt"], re.S,
        ):
            if first_level:
                source_parts.append(text)
            prior = re.search(r"「([^」]+)」", text)
            quote = prior.group(1) if prior else text[-8:]
            entries.append({"source_id": source_id, "summary": "保留本段证据", "quotes": [quote]})
        return json.dumps({"entries": entries}, ensure_ascii=False), {"finish_reason": "stop"}

    monkeypatch.setattr(discussion, "_call_raw_for_task", compact)
    projected = discussion._compress_roundtable_history(
        [("S0001-T1", source)], agent=object(), task_id=1, user_id=1,
        file_id=1, target_tokens=1800,
    )

    assert "".join(source_parts) == source
    assert "最后约束必须保留" in projected
    assert len(projected) < len(source) // 2
    assert discussion.estimate_tokens(projected) <= 1800


@pytest.mark.parametrize("evidence_size,scenario_size", [(501, 1001), (1800, 3600)])
def test_sentinel_project_batch_retains_evidence_tail_for_adversarial_model(
    monkeypatch, evidence_size, scenario_size,
):
    evidence = "x" * evidence_size + "\nreturn parameter_bound_query()"
    scenario = "前置条件" * scenario_size + "末尾约束：参数已绑定，不能据此确认注入"
    source = "def endpoint():\n" + evidence + "\n"
    file = CodeFile(
        id=1, project_id=1, file_name="endpoint.py", file_path="endpoint.py",
        language="python", content=source, status="active",
    )
    agent = SecuritySentinelAgent()
    verification_prompts = []

    def answer(prompt, **_kwargs):
        if "候选漏洞:" in prompt:
            verification_prompts.append(prompt)
            return AgentResult(success=True, finish_reason="stop", data={"reviews": [
                {"index": 1, "verdict": "refuted", "reason": "证据尾部已参数绑定"},
            ]})
        assert source in prompt
        return AgentResult(success=True, finish_reason="stop", data={
            "output_limited": False,
            "findings": [{
                "file_path": "endpoint.py", "title": "候选注入", "severity": "高",
                "evidence": evidence, "exploit_scenario": scenario,
            }],
            "entry_points": [], "dangerous_sinks": [],
        })

    monkeypatch.setattr(agent, "call_json", answer)
    batch = agent._llm_project_audit_batch([_ProjectAuditPart(file, source, 0)], ctx=None)
    assert batch.success is True
    assert len(batch.findings) == 1
    findings = agent._dedup_findings(batch.findings)
    result = agent._adversarial_verify(findings, ctx=None)

    assert result["complete"] is True
    assert len(verification_prompts) == 1
    assert evidence in verification_prompts[0]
    assert scenario in verification_prompts[0]
    assert findings[0]["evidence"] == evidence
    assert findings[0]["exploit_scenario"] == scenario


def test_sentinel_preserved_single_evidence_over_budget_is_not_verified_from_prefix(monkeypatch):
    agent = SecuritySentinelAgent()
    evidence = "e" * 4000 + "不允许遗漏的尾部"
    file = CodeFile(id=1, file_name="long.py", file_path="long.py")
    finding = agent._normalize_finding(
        {"severity": "高", "evidence": evidence}, file, line_offset=0,
    )
    prompts = []
    monkeypatch.setattr(agent, "_project_input", lambda prompt, **_kwargs: (prompt, len(prompt) > 3500))
    monkeypatch.setattr(agent, "call_json", lambda prompt, **_kwargs: prompts.append(prompt))

    result = agent._adversarial_verify([finding], ctx=None)

    assert result["failure_kind"] == "input_exceeds_context"
    assert result["complete"] is False
    assert result["pending"] == 1
    assert prompts == []
    assert finding["evidence"] == evidence
    assert finding["verification"] == "unreviewed"
