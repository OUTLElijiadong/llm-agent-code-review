"""小菱团队业务合同：真实执行参数、部分证据及可信汇总。"""

from types import SimpleNamespace
from unittest.mock import Mock

from app.agents.base import AgentResult
from app.services import agent_mesh_dispatcher as dispatcher


def _orch(monkeypatch, **agents):
    from app.agents import orchestrator

    orch = SimpleNamespace(**agents)
    monkeypatch.setattr(orchestrator, "get_request_orchestrator", lambda *_args, **_kwargs: orch)
    return orch


def test_project_security_member_preserves_explicit_scan_mode(monkeypatch):
    sentinel = SimpleNamespace(scan_project=Mock(return_value=AgentResult(success=True, data={})))
    _orch(monkeypatch, security_sentinel=sentinel)
    result = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "security_sentinel", {
        "payload": {"project_id": 8, "scan_mode": "static_full"},
    })
    assert result["status"] == "completed"
    assert sentinel.scan_project.call_args.kwargs["scan_mode"] == "static_full"
    assert result["evidence"][0]["data"]["project_id"] == 8


def test_partial_security_result_keeps_coverage_and_findings(monkeypatch):
    partial = {"findings": [{"title": "输入未经验证"}], "compliance": {"semantic_failed_batch_count": 1}}
    sentinel = SimpleNamespace(scan_project=Mock(return_value=AgentResult(
        success=False, data=partial, error="语义审计未完成", failure_kind="output_truncated",
    )))
    _orch(monkeypatch, security_sentinel=sentinel)
    result = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "security_sentinel", {
        "payload": {"project_id": 8, "scan_mode": "full"},
    })
    assert result["status"] != "completed"
    assert result["evidence"][0]["data"] == {**partial, "project_id": 8}
    assert result["errors"][0]["code"] == "output_truncated"


def test_summary_does_not_promote_nested_partial_to_success():
    from app.services.agent_team_summary import summarize_dependencies

    result = summarize_dependencies({"audit": {
        "status": "completed", "result": {"status": "failed", "summary": "仅部分完成"},
    }})
    assert result["status"] == "failed"
    assert result["retryable"] is False


def test_summary_deduplicates_exact_findings_with_traceable_sources():
    from app.services.agent_team_summary import summarize_dependencies

    finding = {"file_path": "app.py", "line_number": 3, "title": "SQL 注入", "severity": "高"}
    dependencies = {key: {"status": "completed", "result": {
        "status": "completed", "evidence": [{"data": {"project_id": 8, "findings": [finding]}}],
    }} for key in ("audit", "review")}
    result = summarize_dependencies(dependencies)
    summary = result["artifacts"][0]["data"]
    assert result["status"] == "completed"
    assert summary["unique_finding_count"] == 1
    assert summary["findings"][0]["source_tasks"] == ["audit", "review"]
    assert summary["verification"] == "dependency_evidence_reconciled"
    assert len(result["evidence"]) == 2


def test_run_review_rejects_untrusted_message_before_business_dispatch(monkeypatch):
    _orch(monkeypatch)
    result = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "review_orchestrator", {
        "payload": {"operation": "run_review", "project_id": 8, "_agent_team": {"team_id": 1}},
        "context": {"team_id": 1, "agent_team_task_id": 2, "lease_token": "forged"},
    })
    assert result["status"] == "approval_required"


def test_team_dependency_findings_survive_public_depth_bound(db):
    import json

    from app.models.agent_team import AgentTeamTask
    from app.services.agent_team_service import _dependency_context
    from app.services.agent_team_summary import summarize_dependencies

    finding = {"file_id": 1, "line_number": 9, "title": "SQL注入", "severity": "高",
               "description": "password=unsafe-secret", "api_key": "private-secret"}
    row = AgentTeamTask(team_id=81, member_id=1, task_key="audit", title="审计",
                        instructions="审计合成样本", status="completed",
                        result_json=json.dumps({"status": "completed", "evidence": [{"data": {
                            "project_id": 1, "findings": [finding],
                        }}]}))
    db.add(row)
    db.flush()
    context = _dependency_context(db, SimpleNamespace(id=81), SimpleNamespace(
        dependency_keys_json='["audit"]',
    ))
    summary = summarize_dependencies(context)["artifacts"][0]["data"]
    assert summary["unique_finding_count"] == 1
    assert summary["findings"][0]["title"] == "SQL注入"
    assert "private-secret" not in json.dumps(summary)
    assert "unsafe-secret" not in json.dumps(summary)


def test_dependency_projection_keeps_audit_bound_and_masks_sensitive_title():
    from app.services.agent_team_service import _public
    from app.services.agent_team_summary import dependency_finding_summary

    projection = dependency_finding_summary({"evidence": [{"data": {
        "project_id": 3, "compliance": {"findings_truncated": True},
        "findings": [{"title": "password=do-not-expose", "file_id": 5}],
    }}]}, redact=_public)
    assert projection["source_truncated"] is True
    assert projection["items"][0]["project_id"] == 3
    assert "do-not-expose" not in projection["items"][0]["title"]


def test_summary_does_not_merge_different_files_or_unknown_file_locations():
    from app.services.agent_team_summary import summarize_dependencies

    items = [{"file_name": name, "title": "SQL注入", "line_number": 9, "severity": "高", "project_id": 1}
             for name in ("one.py", "two.py", "", "")]
    summary = summarize_dependencies({"review": {"status": "completed", "result": {
        "status": "completed", "findings": items,
    }}})["artifacts"][0]["data"]
    assert summary["unique_finding_count"] == 4


def test_summary_keeps_shallow_findings_in_public_team_final_result():
    from app.services.agent_team_service import _public
    from app.services.agent_team_summary import summarize_dependencies

    result = summarize_dependencies({"review": {"status": "completed", "result": {
        "status": "completed", "task_id": 17, "project_id": 2,
        "findings": [{"title": "SQL注入", "file_name": "app.py", "line_number": 3}],
    }}})
    public = _public({"completed_tasks": 3, "final_result": _public(result)})["final_result"]
    assert public["unique_finding_count"] == 1
    assert public["findings"][0]["title"] == "SQL注入"
    assert public["findings"][0]["source_task_keys"] == "review"
    assert public["references"][0]["route"] == "/reviews/17"
