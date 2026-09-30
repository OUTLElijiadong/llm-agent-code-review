"""小菱团队业务合同：真实执行参数、部分证据及可信汇总。"""

import hashlib
import json
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
    sentinel = SimpleNamespace(scan_project=Mock(return_value=AgentResult(success=True, data={
        "findings": [], "summary": "已扫描指定项目", "compliance": {"scan_mode": "static_full"},
    })))
    _orch(monkeypatch, audit_security_for_project=sentinel.scan_project)
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
    _orch(monkeypatch, audit_security_for_project=sentinel.scan_project)
    result = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "security_sentinel", {
        "payload": {"project_id": 8, "scan_mode": "full"},
    })
    assert result["status"] != "completed"
    assert result["evidence"][0]["data"] == {**partial, "project_id": 8}
    assert result["errors"][0]["code"] == "output_truncated"


def test_code_review_empty_model_object_is_not_completed_zero_findings(monkeypatch):
    reviewer = SimpleNamespace(review_code=Mock(return_value=AgentResult(success=True, data={})))
    _orch(monkeypatch, review_code=reviewer.review_code)
    result = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "code_reviewer", {
        "payload": {"code": "dangerous_call(user_input)", "file_name": "review.py"},
    })
    assert result["status"] != "completed"
    assert result["errors"][0]["code"] == "invalid_review_result"


def test_code_review_valid_zero_and_invalid_issue_are_distinguished(monkeypatch):
    response = {"data": {"issues": [], "summary": "已核对所给片段，未发现问题"}}
    reviewer = SimpleNamespace(review_code=Mock(side_effect=lambda *_args, **_kwargs: AgentResult(
        success=True, data=response["data"],
    )))
    _orch(monkeypatch, review_code=reviewer.review_code)
    message = {"payload": {"code": "print('ok')", "file_name": "sample.py"}}
    valid = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "code_reviewer", message)
    assert valid["status"] == "completed"
    assert valid["evidence"][0]["data"]["issues"] == []

    response["data"] = {"issues": [{"unexpected": "missing finding identity"}]}
    invalid = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "code_reviewer", message)
    assert invalid["status"] != "completed"
    assert invalid["errors"][0]["code"] == "invalid_review_result"


def test_irrecoverable_code_review_truncation_is_explicitly_non_retryable(monkeypatch):
    reviewer = SimpleNamespace(review_code=Mock(return_value=AgentResult(
        success=False,
        error="代码审查覆盖不完整：原文件第 1 行仍被截断",
        failure_kind="coverage_incomplete",
        finish_reason="length",
    )))
    _orch(monkeypatch, review_code=reviewer.review_code)

    result = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "code_reviewer", {
        "payload": {"code": "unusually_long_single_line()", "file_name": "review.py"},
    })

    assert result["status"] == "failed"
    assert result["retryable"] is False
    assert result["errors"][0]["code"] == "coverage_incomplete"


def test_code_review_mixed_valid_and_invalid_issues_is_not_complete(monkeypatch):
    reviewer = Mock(return_value=AgentResult(success=True, data={
        "issues": [{"title": "已定位问题", "description": "真实问题"}, {"unexpected": "missing identity"}],
        "summary": "模型输出含一条无效问题",
    }))
    _orch(monkeypatch, review_code=reviewer)
    result = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "code_reviewer", {
        "payload": {"code": "dangerous_call(user_input)", "file_name": "review.py"},
    })
    assert result["status"] != "completed"
    assert result["errors"][0]["code"] == "invalid_review_result"
    assert result["evidence"][0]["data"]["issues"][0]["title"] == "已定位问题"


def test_code_review_upstream_invalid_issue_count_is_not_hidden(monkeypatch):
    reviewer = Mock(return_value=AgentResult(success=True, data={
        "issues": [], "summary": "保留了有效条目", "invalid_issue_count": 1,
        "diagnostics": ["issue_missing_identity"],
    }))
    _orch(monkeypatch, review_code=reviewer)
    result = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "code_reviewer", {
        "payload": {"code": "dangerous_call(user_input)", "file_name": "review.py"},
    })
    assert result["status"] != "completed"
    assert result["errors"][0]["code"] == "invalid_review_result"


def test_review_retry_preserves_original_snippet_even_with_unrelated_project_dependency(monkeypatch):
    from app.services.agent_team_service import _apply_execution_strategy

    original_code = "report = db.get(Report, report_id)\n"
    original_sha = hashlib.sha256(original_code.encode()).hexdigest()
    row = SimpleNamespace(
        input_json=json.dumps({"code": original_code, "file_name": "user-snippet.py",
                               "source_revision_id": 81, "project_id": 7}),
        attempt_count=1,
    )
    member = SimpleNamespace(address="agent:code_reviewer")
    _apply_execution_strategy(row, member, instruction="补充鉴权证据", error="上一轮证据不足", automatic=False)
    retried = json.loads(row.input_json)
    assert hashlib.sha256(retried["code"].encode()).hexdigest() == original_sha
    assert retried["source_revision_id"] == 81
    assert retried["file_name"] == "user-snippet.py"

    reviewer = Mock(return_value=AgentResult(success=True, data={"issues": [], "summary": "仅此片段未发现问题"}))
    _orch(monkeypatch, review_code=reviewer)
    result = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "code_reviewer", {
        "payload": {**retried, "dependency_context": {"foreign": {
            "project_id": 999, "file_name": "other-project.py", "code": "different_source()",
        }}},
    })
    assert result["status"] == "completed"
    assert reviewer.call_args.args[0] == original_code
    assert reviewer.call_args.kwargs["file_name"] == "user-snippet.py"


def test_security_success_without_structured_evidence_cannot_report_zero_findings(monkeypatch):
    sentinel = Mock(return_value=AgentResult(success=True, data={}))
    _orch(monkeypatch, audit_security_for_task=sentinel)
    result = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "security_sentinel", {
        "payload": {"task_id": 18},
    })
    assert result["status"] != "completed"
    assert result["errors"][0]["code"] == "invalid_audit_result"


def test_security_explicit_incomplete_coverage_is_not_completed(monkeypatch):
    audit = {"findings": [], "summary": "仅保留部分扫描结果", "file_count": 1,
             "compliance": {"scan_complete": False, "findings_truncated": True}}
    sentinel = Mock(return_value=AgentResult(success=True, data=audit))
    _orch(monkeypatch, audit_security_for_file=sentinel)
    result = dispatcher._runtime_handler(None, SimpleNamespace(id=3), "security_sentinel", {
        "payload": {"file_id": 18},
    })
    assert result["status"] != "completed"
    assert result["errors"][0]["code"] == "invalid_audit_result"
    assert result["evidence"][0]["data"] == audit


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


def test_dependency_handoff_keeps_private_full_source_but_public_preview_is_bounded(db):
    import json

    from app.models.agent_team import AgentTeamTask
    from app.services.agent_team_service import _dependency_context, _public

    findings = [{"title": f"真实结论 {index}", "detail": "依据" * 300} for index in range(25)]
    findings[-1]["detail"] += "LAST_SOURCE_EVIDENCE"
    db.add(AgentTeamTask(
        team_id=183, member_id=1, task_key="audit", title="审计", instructions="读取全部依赖",
        status="completed", result_json=json.dumps({
        "status": "completed", "findings": findings, "summary": "原始结论已入账",
        "api_key": "sk-example-secret-12345678",
        }),
    ))
    db.flush()
    context = _dependency_context(db, SimpleNamespace(id=183), SimpleNamespace(
        dependency_keys_json='["audit"]',
    ))
    assert len(context["audit"]["result"]["findings"]) == 25
    assert "LAST_SOURCE_EVIDENCE" in context["audit"]["result"]["findings"][-1]["detail"]
    assert context["audit"]["result"]["api_key"] == "[REDACTED]"
    public = _public(context)
    assert "LAST_SOURCE_EVIDENCE" not in json.dumps(public)
    assert "TRUNCATED" in json.dumps(public)


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

    result = summarize_dependencies({"review": {"status": "completed", "verified_review_task_id": 17,
        "result": {
        "status": "completed", "task_id": 17, "project_id": 2,
        "artifacts": [{"type": "review_task", "task_id": 17}],
        "findings": [{"title": "SQL注入", "file_name": "app.py", "line_number": 3}],
    }}})
    public = _public({"completed_tasks": 3, "final_result": _public(result)})["final_result"]
    assert public["unique_finding_count"] == 1
    assert public["findings"][0]["title"] == "SQL注入"
    assert public["findings"][0]["source_task_keys"] == "review"
    assert public["references"][0]["route"] == "/reviews/17"


def test_summary_does_not_turn_team_task_id_into_formal_review_link():
    from app.services.agent_team_summary import summarize_dependencies

    result = summarize_dependencies({
        "ordinary": {"status": "completed", "result": {
            "status": "completed", "task_id": 216,
            "findings": [{"title": "代码建议", "file_name": "a.py"}],
        }},
        "formal": {"status": "completed", "verified_review_task_id": 180, "result": {
            "status": "completed", "task_id": 180, "project_id": 167,
            "artifacts": [{"type": "review_task", "task_id": 180}],
        }},
    })
    assert result["references"] == [{
        "type": "review_task", "task_id": 180,
        "route": "/reviews/180", "source_task": "formal",
    }]


def test_dependency_context_verifies_formal_review_record_before_linking(db):
    import json

    from app.models.agent_team import AgentTeamTask
    from app.models.review_task import ReviewTask
    from app.services.agent_team_service import _dependency_context

    row = AgentTeamTask(id=216, team_id=81, member_id=1, task_key="review", title="审查",
                        instructions="正式审查", status="completed", result_json="{}")
    db.add(row)
    db.flush()
    review = ReviewTask(id=180, user_id=3, project_id=5, review_type="full", status="success",
                        agent_team_id=81, agent_team_task_id=row.id)
    db.add(review)
    db.flush()
    row.result_json = json.dumps({"status": "completed", "task_id": review.id,
                                  "project_id": 5, "artifacts": [{"type": "review_task", "task_id": review.id}]})
    db.flush()
    team = SimpleNamespace(id=81, user_id=3)
    task = SimpleNamespace(dependency_keys_json='["review"]')
    assert _dependency_context(db, team, task)["review"]["verified_review_task_id"] == review.id

    team.user_id = 4
    assert "verified_review_task_id" not in _dependency_context(db, team, task)["review"]
    team.user_id = 3
    row.result_json = json.dumps({"status": "completed", "task_id": review.id, "project_id": 5})
    db.flush()
    assert "verified_review_task_id" not in _dependency_context(db, team, task)["review"]

    unrelated_id = row.id
    row.result_json = json.dumps({"status": "completed", "task_id": unrelated_id,
                                  "project_id": 5, "artifacts": [{"type": "review_task", "task_id": unrelated_id}]})
    db.flush()
    assert "verified_review_task_id" not in _dependency_context(db, team, task)["review"]


def test_team_summary_counts_last_finding_from_full_private_dependency():
    from app.services.agent_team_summary import summarize_dependencies

    findings = [
        {"title": f"问题 {index}", "file_name": "source.py", "line_number": index, "severity": "高"}
        for index in range(201)
    ]
    result = summarize_dependencies({"audit": {
        "status": "completed", "result": {"status": "completed", "findings": findings},
        "finding_summary": {"items": findings[:200], "omitted_count": 1, "source_truncated": False},
    }})
    full = result["artifacts"][0]["data"]
    assert result["status"] == "completed"
    assert result["unique_finding_count"] == full["unique_finding_count"] == 201
    assert any(item["title"] == "问题 200" for item in full["findings"])
    assert result["findings_preview_truncated"] is True
    assert result["bounded_finding_tasks"] == []


def test_team_summary_rejects_completed_status_when_dependency_findings_are_truncated():
    from app.services.agent_team_summary import summarize_dependencies

    result = summarize_dependencies({"audit": {"status": "completed", "result": {
        "status": "completed", "findings": [{"title": "问题", "file_name": "a.py"}],
        "findings_truncated": True,
    }}})

    assert result["status"] == "failed"
    assert result["bounded_finding_tasks"] == ["audit"]
    assert result["errors"][0]["code"] == "finding_coverage_incomplete"


def test_team_summary_rejects_explicit_incomplete_audit_coverage():
    from app.services.agent_team_summary import summarize_dependencies

    result = summarize_dependencies({"audit": {"status": "completed", "result": {
        "status": "completed", "evidence": [{"data": {
            "project_id": 9, "findings": [], "compliance": {
                "scan_complete": False, "semantic_execution_complete": False,
            },
        }}],
    }}})
    assert result["status"] == "failed"
    assert result["bounded_finding_tasks"] == ["audit"]
    assert result["coverage_summary"][0]["scan_complete"] is False


def test_team_summary_keeps_same_path_findings_from_different_projects_separate():
    from app.services.agent_team_summary import summarize_dependencies

    finding = {"title": "SQL 注入", "file_path": "app.py", "line_number": 3, "severity": "高"}
    result = summarize_dependencies({
        str(project_id): {"status": "completed", "result": {
            "status": "completed", "evidence": [{"data": {
                "project_id": project_id, "findings": [finding],
            }}],
        }} for project_id in (8, 9)
    })
    assert result["unique_finding_count"] == 2
    assert {item["project_id"] for item in result["artifacts"][0]["data"]["findings"]} == {8, 9}


def test_audit_coverage_survives_dependency_and_double_public_projection(db):
    import json

    from app.models.agent_team import AgentTeamTask
    from app.services.agent_team_service import _dependency_context, _public
    from app.services.agent_team_summary import summarize_dependencies

    compliance = {"scan_mode": "full", "semantic_complete": True,
                  "semantic_source_chars": 337, "semantic_attempted_source_chars": 337,
                  "semantic_failed_batch_count": 0, "static_scanned_file_count": 1,
                  "static_complete": True, "api_key": "private-secret"}
    db.add(AgentTeamTask(team_id=82, member_id=1, task_key="audit", title="审计",
                        instructions="仅代码分析", status="completed",
                        result_json=json.dumps({"status": "completed", "evidence": [{"data": {
                            "project_id": 1, "compliance": compliance,
                        }}]})))
    db.flush()
    context = _dependency_context(db, SimpleNamespace(id=82), SimpleNamespace(
        dependency_keys_json='["audit"]',
    ))
    result = summarize_dependencies(context)
    public = _public({"final_result": _public(result)})["final_result"]
    coverage = public["coverage_summary"][0]
    assert coverage["task_key"] == "audit"
    assert coverage["semantic_complete"] is True
    assert coverage["semantic_source_chars"] == coverage["semantic_attempted_source_chars"] == 337
    assert coverage["semantic_failed_batch_count"] == 0
    assert coverage["static_scanned_file_count"] == 1
    assert "private-secret" not in json.dumps(coverage)


def test_coverage_summary_preserves_partial_counts_and_does_not_infer_unknown():
    from app.services.agent_team_summary import summarize_dependencies

    result = summarize_dependencies({"review": {"status": "completed", "result": {
        "status": "failed", "coverage": {"stage": "failed", "total_chunks": 2,
                                              "completed_chunks": 1, "failed_chunks": 1},
    }}, "unknown": {"status": "completed", "result": {"status": "completed"}}})
    coverage = {row["task_key"]: row for row in result["coverage_summary"]}
    assert result["status"] == "failed"
    assert coverage["review"]["completed_chunks"] == 1
    assert coverage["review"]["failed_chunks"] == 1
    assert coverage["unknown"] == {"task_key": "unknown"}
