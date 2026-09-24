"""全链审计不可把风险子集、漏评高危伪装为完整审计。"""

from __future__ import annotations

from app.agents.audit_board import AuditBoard
from app.agents.base import AgentResult
from app.agents.fullchain_audit_agent import FullChainAuditOrchestrator


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
