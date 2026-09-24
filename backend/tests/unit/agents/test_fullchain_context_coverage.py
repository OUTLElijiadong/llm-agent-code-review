"""全链审计不可把风险子集、漏评高危伪装为完整审计。"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from app.agents.audit_board import AuditBoard
from app.agents.base import AgentResult
from app.agents.fullchain_audit_agent import FullChainAuditOrchestrator, ReconReport


class _Sentinel:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.prompts: list[str] = []
        self.fail_index: int | None = None

    def _emit(self, *_args, **_kwargs) -> None:
        return None

    def scan_project(self, project_id, **kwargs):
        self.calls.append({"project_id": project_id, **kwargs})
        return AgentResult(success=True, data={"findings": []})

    def call_json(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        if self.fail_index == len(self.prompts):
            return AgentResult(success=False, error="模型输出超限")
        return AgentResult(success=True, data={"verdict": "plausible", "poc": "只读检查"})


class _BoundedSentinel(_Sentinel):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []

    def _project_input(self, user_message, **_kwargs):
        return user_message, len(user_message) > 1800

    def call_json(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        assert len(prompt) <= 1800, "不能把超窗证据交给模型后才发现失败"
        if "只输出 JSON 摘要" in prompt:
            source_id = re.search(r"\[来源#\d+:片段\d+/\d+\]", prompt).group()
            source = prompt.split("来源原文:\n", 1)[1]
            return AgentResult(success=True, data={
                "source_id": source_id, "quote": source[:8], "summary": "发现原文证据 " + source[-16:],
            })
        return AgentResult(success=True, data={"verdict": "plausible", "poc": "只读检查"})

    def call(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        assert len(prompt) <= 1800
        index = int(re.search(r"PRISM_POC_RESULT index=(\d+)", prompt).group(1))
        script = f"#!/bin/sh\nprintf 'PRISM_POC_RESULT index={index} verdict=inconclusive evidence=待核实\\n'\n"
        self.scripts.append(script)
        return AgentResult(success=True, data=script)


def test_fullchain_requests_full_semantic_coverage() -> None:
    sentinel = _Sentinel()
    result = FullChainAuditOrchestrator(sentinel)._analysis(7, 100, True, AuditBoard(), None)
    assert result.success
    assert sentinel.calls[0]["scan_mode"] == "full"


def test_fullchain_rejects_successful_but_cropped_sentinel_result() -> None:
    sentinel = _Sentinel()
    sentinel.scan_project = lambda *_args, **_kwargs: AgentResult(
        success=True, data={"findings": [], "compliance": {"findings_truncated": True}}
    )
    result = FullChainAuditOrchestrator(sentinel)._analysis(7, 100, True, AuditBoard(), None)
    assert result.success is False
    assert result.failure_kind == "partial_coverage"


def test_fullchain_verifies_every_high_risk_finding_with_full_evidence() -> None:
    sentinel = _Sentinel()
    orchestrator = FullChainAuditOrchestrator(sentinel)
    targets = [
        {"severity": "高", "evidence": "证据" * 400 + f"尾部事实{i}", "exploit_scenario": "影响" * 200}
        for i in range(11)
    ]
    verdicts = orchestrator._llm_verify(targets, None)
    assert len(verdicts) == len(targets) == len(sentinel.prompts)
    assert all(f"尾部事实{i}" in sentinel.prompts[i] for i in range(len(targets)))


def test_fullchain_model_failure_does_not_return_partial_verdicts() -> None:
    sentinel = _Sentinel()
    sentinel.fail_index = 2
    orchestrator = FullChainAuditOrchestrator(sentinel)
    targets = [{"severity": "高", "evidence": f"E{i}"} for i in range(3)]
    try:
        orchestrator._llm_verify(targets, None)
    except RuntimeError as exc:
        assert "未完成模型验证" in str(exc)
    else:
        raise AssertionError("漏评高危必须失败关闭")


def test_fullchain_splits_oversize_finding_and_preserves_all_source_parts(monkeypatch) -> None:
    monkeypatch.setattr("app.agents.security_sentinel_agent._knowledge_context", lambda *_: "规则\n")
    sentinel = _BoundedSentinel()
    finding = {"severity": "高", "evidence": "危险调用 " * 850 + "尾部关键证据"}
    verdict = FullChainAuditOrchestrator(sentinel)._llm_verify([finding], None)
    assert verdict[0]["verdict"] == "plausible"
    assert len(sentinel.prompts) > 2
    sources = [re.search(r"\[来源#\d+:片段\d+/\d+\]", prompt).group()
               for prompt in sentinel.prompts[:-1]]
    assert len(sources) == len(set(sources))
    assert all(source in sentinel.prompts[-1] for source in sources)
    assert "尾部关键证据" in sentinel.prompts[-1]


def test_fullchain_rejects_unattributed_evidence_summary(monkeypatch) -> None:
    monkeypatch.setattr("app.agents.security_sentinel_agent._knowledge_context", lambda *_: "规则\n")
    sentinel = _BoundedSentinel()
    sentinel.call_json = lambda *_args, **_kwargs: AgentResult(
        success=True, data={"source_id": "[来源#错]", "quote": "", "summary": "已检查"}
    )
    with pytest.raises(RuntimeError, match="来源|摘要"):
        FullChainAuditOrchestrator(sentinel)._llm_verify(
            [{"severity": "高", "evidence": "证据" * 1200}], None,
        )


def test_fullchain_poc_generation_bounds_each_target_and_checks_all_indices() -> None:
    sentinel = _BoundedSentinel()
    targets = [{"category": "SQL注入", "evidence": "长证据" * 900},
               {"category": "路径遍历", "evidence": "长证据" * 900}]
    script = FullChainAuditOrchestrator(sentinel)._generate_poc_script(targets, None)
    assert len(sentinel.scripts) == len(targets)
    assert "PRISM_POC_RESULT index=0" in script
    assert "PRISM_POC_RESULT index=1" in script


def test_fullchain_poc_generation_rejects_missing_index() -> None:
    sentinel = _BoundedSentinel()
    sentinel.call = lambda *_args, **_kwargs: AgentResult(
        success=True, data="#!/bin/sh\necho 'PRISM_POC_RESULT index=0 verdict=inconclusive evidence=x'",
    )
    with pytest.raises(RuntimeError, match="index=1|索引"):
        FullChainAuditOrchestrator(sentinel)._generate_poc_script(
            [{"category": "SQL注入"}, {"category": "路径遍历"}], None,
        )


def test_fullchain_rejects_missing_runtime_poc_result() -> None:
    orchestrator = FullChainAuditOrchestrator(_Sentinel())
    with pytest.raises(RuntimeError, match="1|缺失"):
        orchestrator._parse_poc_result(
            {"stdout": "PRISM_POC_RESULT index=0 verdict=inconclusive evidence=待核实"},
            [{"category": "SQL注入"}, {"category": "路径遍历"}],
        )


def test_fullchain_rejects_duplicate_runtime_poc_result() -> None:
    orchestrator = FullChainAuditOrchestrator(_Sentinel())
    line = "PRISM_POC_RESULT index=0 verdict=inconclusive evidence=待核实"
    with pytest.raises(RuntimeError, match="重复"):
        orchestrator._parse_poc_result({"logs": {"text": f"{line}\n{line}"}}, [{}])


def test_fullchain_rejects_poc_marker_embedded_in_response_text() -> None:
    orchestrator = FullChainAuditOrchestrator(_Sentinel())
    with pytest.raises(RuntimeError, match="缺失"):
        orchestrator._parse_poc_result(
            {"logs": {"text": "noisePRISM_POC_RESULT index=0 verdict=confirmed evidence=x"}},
            [{}],
        )


def test_fullchain_plain_report_marks_preview_omissions() -> None:
    findings = [{"severity": "高", "title": f"问题 {i}", "description": "证据" * 150}
                for i in range(11)]
    report = FullChainAuditOrchestrator(_Sentinel())._report(
        SimpleNamespace(project_name="项目"), ReconReport(),
        AgentResult(success=True, data={"findings": findings}),
        {"verified": 0}, AuditBoard(), 1,
    )
    preview = report["plain_report"]
    assert preview["findings_total"] == 11
    assert preview["findings_preview_truncated"] is True
    assert len(preview["findings"]) == 10
    assert preview["findings"][0]["what_it_means_truncated"] is True
    assert preview["findings"][0]["what_it_means_total_chars"] == 300


def test_fullchain_llm_confirmation_is_not_reported_as_sandbox_reproduction() -> None:
    sentinel = _Sentinel()
    sentinel.call_json = lambda *_args, **_kwargs: AgentResult(
        success=True, data={"verdict": "confirmed", "poc": "仅模型推理"}
    )
    orchestrator = FullChainAuditOrchestrator(sentinel)
    findings = [{"severity": "高", "evidence": "证据"}]
    verification = orchestrator._verification(
        SimpleNamespace(project_name="项目"), SimpleNamespace(),
        findings, AuditBoard(), None, enable_sandbox=False,
    )
    assert findings[0]["exploit_verdict"] == "confirmed"
    assert findings[0]["exploit_verdict_source"] == "llm_inference"
    assert findings[0]["exploit_verdict_status"] == "pending_independent_verification"
    assert verification["verified"] == 0
    assert verification["llm_confirmed"] == 1
    report = orchestrator._report(
        SimpleNamespace(project_name="项目"), ReconReport(),
        AgentResult(success=True, data={"findings": findings}),
        verification, AuditBoard(), 1,
    )
    assert "实际验证" not in report["plain_report"]["verdict_detail"]


def test_marker_only_sandbox_script_report_remains_pending_review(monkeypatch) -> None:
    """只有脚本打印的 marker、没有独立请求响应时不得写成实测复现。"""
    monkeypatch.setattr("app.agents.security_sentinel_agent._knowledge_context", lambda *_: "规则\n")
    sentinel = _BoundedSentinel()
    orchestrator = FullChainAuditOrchestrator(sentinel)
    findings = [{"severity": "高", "category": "代码风险", "evidence": "待检查证据"}]
    script = orchestrator._generate_poc_script(findings, None)
    assert "PRISM_POC_RESULT index=0" in script
    marker = "PRISM_POC_RESULT index=0 verdict=confirmed evidence=脚本自报"
    orchestrator._sandbox_verify = lambda *_args, **_kwargs: orchestrator._parse_poc_result(
        {"logs": {"text": marker}}, findings,
    )

    verification = orchestrator._verification(
        SimpleNamespace(project_name="项目"), SimpleNamespace(),
        findings, AuditBoard(), None, enable_sandbox=True,
    )

    assert verification["sandbox_used"] is True
    assert verification["sandbox_script_reported_confirmed"] == 1
    assert verification["verified"] == 0
    assert verification["verified_findings"] == []
    assert findings[0]["sandbox_script_report"]["status"] == "pending_independent_verification"
    report = orchestrator._report(
        SimpleNamespace(project_name="项目"), ReconReport(),
        AgentResult(success=True, data={"findings": findings}),
        verification, AuditBoard(), 1,
    )
    assert "沙箱脚本回报" in report["plain_report"]["verdict_detail"]
    assert "待复核" in report["plain_report"]["verdict_detail"]
    assert "实际验证" not in report["plain_report"]["verdict_detail"]
    assert "稳定复现" not in report["plain_report"]["verdict_detail"]
    assert "实际验证" not in str(report)
    assert "稳定复现" not in str(report)
