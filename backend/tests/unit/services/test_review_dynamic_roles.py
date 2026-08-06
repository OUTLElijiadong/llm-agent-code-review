"""多 Agent 审查动态角色编排回归:编排 Agent 可按证据追加专项角色。"""

from __future__ import annotations

from types import SimpleNamespace

from app.agents.test_review_reporter_agent import TestReviewReporterAgent


class _Ok:
    def __init__(self, data):
        self.success = True
        self.data = data
        self.error = ""
        self.finish_reason = "stop"
        self.model = "deepseek-v4-flash"
        self.duration_ms = 1
        self.tokens = {"total": 1}
        self.failure_kind = ""


def _environment() -> SimpleNamespace:
    return SimpleNamespace(
        public_id="sbx_dyn_roles", purpose="test", test_mode="blackbox",
        language="python", status="succeeded", project_id=1, owner_id=1,
    )


def test_orchestrator_adds_extra_roles(db, monkeypatch) -> None:
    agent = TestReviewReporterAgent()
    calls: list[str] = []

    def fake_role_call(system, user, ctx=None, max_tokens=None):
        calls.append(system.split("审查 Agent")[0].strip()[:12])
        if "编排" in system:
            return _Ok({"extra_roles": ["sast", "dast"]})
        if "SAST" in system:
            return _Ok("## SAST 静态审计\n发现硬编码密钥(建议验证)")
        if "DAST" in system:
            return _Ok("## DAST 动态测试\nSQL 注入探测:未发现可利用点")
        return _Ok("## 结果\nok")

    monkeypatch.setattr(agent, "_role_call", fake_role_call)
    monkeypatch.setattr(agent, "_knowledge_refs", lambda *_a, **_k: "")
    result = agent.review(
        db,
        environment=_environment(),
        conclusion={
            "passed": True,
            "summary": "ok",
            "evidence": {"worker_result": {"exit_code": 0, "logs": {"text": ""}}},
        },
    )
    assert result.success
    roles = result.data["roles"]
    assert "sast" in roles
    assert "dast" in roles
    assert result.data["roles_executed"] == 6  # whitebox/blackbox/verify/report + sast/dast
    assert any("SAST" in c for c in calls)
