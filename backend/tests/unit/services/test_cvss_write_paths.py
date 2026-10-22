"""Business scoring writes use one vector-recomputed composite result."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.agents.base import AgentResult
from app.agents.review_agent import CodeReviewerAgent
from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.services import review_service

VECTOR = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"


def test_review_task_persists_score_version_and_breakdown_from_one_composite(db, monkeypatch):
    task = SimpleNamespace(
        id=7, review_type="standard", processed_files=0, total_files=1,
        total_issues=0, severe_issues=0, high_issues=0, medium_issues=0,
        low_issues=0, score=0, score_version=None, score_breakdown=None,
        summary=None, status="running", end_time=None, coverage=None,
        duration_ms=0, error_message=None,
    )
    issue = SimpleNamespace(severity="高", cvss_vector=VECTOR, status="unfixed")
    monkeypatch.setattr(review_service, "_check_cancelled", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_service, "_review_one_file", lambda *args, **kwargs: [issue])
    monkeypatch.setattr(review_service, "_safe_commit", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_service, "_emit_review_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_service, "_build_summary", lambda *args, **kwargs: "summary")

    review_service._execute_review(
        SimpleNamespace(refresh=lambda _task: None), None, None, task,
        SimpleNamespace(id=9), [SimpleNamespace(id=3, file_name="a.py", content="x=1")],
        [], (SimpleNamespace(code="general"),), "",
    )

    assert task.score == 29
    assert task.score_version == task.score_breakdown["version"] == "cvss-weighted-v1"
    assert task.score_breakdown["policy_version"] == "cvss-weighted-v1"
    assert task.score == task.score_breakdown["score"]


def test_review_agent_ignores_model_score_and_returns_vector_composite(monkeypatch):
    agent = CodeReviewerAgent()
    monkeypatch.setattr(
        agent,
        "call",
        lambda *args, **kwargs: AgentResult(
            success=True,
            data=json.dumps({
                "summary": "finding",
                "score": 1,
                "issues": [{
                    "severity": "高", "issue_type": "安全漏洞", "line_number": 1,
                    "title": "injection", "description": "evidence-backed",
                    "suggestion": "parameterize", "evidence": "query(user_input)",
                    "cvss_score": 0.1, "cvss_vector": VECTOR,
                }],
            }),
        ),
    )

    result = agent.execute_review(
        code="query(user_input)\n", rules=[], language="python", file_name="a.py",
    )

    assert result.success
    assert result.data["score"] == 29
    assert result.data["score_version"] == result.data["score_breakdown"]["version"]
    assert result.data["score_breakdown"]["components"]["cvss"]["max_base_score"] == 9.8


def test_review_agent_recovery_ignores_model_score_and_scores_converted_findings(monkeypatch):
    agent = CodeReviewerAgent()
    monkeypatch.setattr(
        agent,
        "call",
        lambda *args, **kwargs: AgentResult(
            success=False, failure_kind="output_truncated", error="truncated",
        ),
    )
    monkeypatch.setattr(
        agent,
        "_recover_truncated_review",
        lambda **kwargs: AgentResult(
            success=True,
            data={
                "score": 1,
                "summary": "recovered",
                "issues": [{
                    "severity": "高", "issue_type": "安全漏洞", "line_number": 1,
                    "title": "injection", "description": "evidence-backed",
                    "suggestion": "parameterize", "evidence": "query(user_input)",
                    "cvss_score": 0.1, "cvss_vector": VECTOR,
                }],
                "coverage": {"stage": "complete"},
            },
        ),
    )

    result = agent.execute_review(
        code="query(user_input)\n", rules=[], language="python", file_name="a.py",
    )

    assert result.success
    assert result.data["score"] == 29
    assert result.data["score_version"] == result.data["score_breakdown"]["version"]
    assert result.data["score_breakdown"]["components"]["cvss"]["max_base_score"] == 9.8


def test_sentinel_risk_score_uses_recomputed_cvss_not_model_score():
    agent = SecuritySentinelAgent.__new__(SecuritySentinelAgent)

    score = agent._compute_risk_score([{
        "severity": "高", "cvss_score": 0.1, "cvss_vector": VECTOR,
    }])

    assert score == 29
