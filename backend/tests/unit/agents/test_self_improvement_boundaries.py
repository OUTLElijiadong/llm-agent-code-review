"""自进化 Skill 的只读反思和管理员独占变更边界测试。"""

from __future__ import annotations

from typing import Any

from app.agents.skills.self_improvement import SelfImprovementSkill


class _DemoSkill(SelfImprovementSkill):
    def aggregate_feedback(self, db: Any, window_days: int) -> list[dict]:
        return []

    def evolve_target(self, db: Any, stats: list[dict]) -> list[dict]:
        return []

    def apply_proposal(self, db: Any, proposal: dict) -> int:
        raise AssertionError("Skill 不得直接应用提案")

    def reflect_from_logs(self, db: Any, window_days: int = 7) -> list[dict]:
        return [{"window_days": window_days, "finding": "只读反思"}]


def test_reflect_from_logs_is_supported_as_read_only_action() -> None:
    skill = _DemoSkill("demo")
    result = skill.run({"action": "reflect_from_logs", "window_days": 3, "_db": object()})
    assert result.success is True
    assert result.effect == "observed"
    assert result.data == {"reflections": [{"window_days": 3, "finding": "只读反思"}]}


def test_apply_and_rollback_are_denied_before_database_mutation() -> None:
    skill = _DemoSkill("demo")
    for action in ("apply", "rollback"):
        result = skill.run({"action": action, "proposal_id": 1, "_db": object()})
        assert result.success is False
        assert result.effect == "denied"
        assert "管理员" in (result.error or "")
    assert skill.rollback_proposal(object(), 1) is False


def test_evaluation_gate_fails_closed_by_default_and_on_exception() -> None:
    skill = _DemoSkill("demo")
    assert skill.evaluate_gate(object(), {})["passed"] is False

    def broken_gate(_db: Any, _proposal: dict) -> dict:
        raise RuntimeError("gate unavailable")

    skill.evaluate_gate = broken_gate  # type: ignore[method-assign]
    result = skill._safe_evaluate_gate(object(), {"title": "candidate"})
    assert result["passed"] is False
    assert "gate unavailable" in result["reason"]


def test_self_improvement_schema_exposes_no_direct_mutation_actions() -> None:
    actions = _DemoSkill("demo")._params_schema()["properties"]["action"]["enum"]
    assert actions == ["evolve", "reflect_from_logs"]
