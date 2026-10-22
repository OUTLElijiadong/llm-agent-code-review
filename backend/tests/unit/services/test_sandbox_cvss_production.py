"""Real review entry: runner v2 receipts -> Verify JSON -> sandbox/report persistence.

Only model transport, knowledge lookup and the execution lease are substituted.
No worker, model network request or production database is used.
"""
from __future__ import annotations

import base64
import copy
import json
from datetime import datetime, timedelta

import pytest

from app.agents.base import AgentResult
from app.agents.test_review_reporter_agent import TestReviewReporterAgent as Reporter
from app.models.agent_capability import SandboxEnvironment
from app.models.project import Project
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import report_service, sandbox_service

VECTOR = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"
QUOTE = "unauthenticated GET /private returned HTTP 200 with another user's private record"
REPORT = "\n".join(f"## {title}\n{body}" for title, body in [
    ("总体结论", "执行检查通过并不代表安全。"),
    ("白盒结果", "参见执行回执。"), ("黑盒结果", "参见执行回执。"),
    ("问题清单", "1. **[高] 未授权读取私有记录**：参见结构化执行证据。"),
    ("攻击面待验证清单", "未覆盖路径需继续核验。"),
    ("下一步建议", "修复鉴权并重测。"), ("证据附录", QUOTE),
])


def _receipt(mode: str, *, failed=False, output=QUOTE):
    file_name = "_agent_tests/blackbox.py" if mode == "blackbox" else "_agent_tests/security.py"
    status = "fail" if failed else "pass"
    record = {
        "protocol_version": 2,
        "files": {file_name: status},
        "file_results": {file_name: {
            "status": status, "phase": "execute", "failure_kind": "execution_failure" if failed else "",
            "exit_code": 1 if failed else 0, "output_encoding": "base64",
            "output_base64": base64.b64encode(output.encode()).decode(),
        }},
    }
    log = f"PRISM_AGENT_TESTS_BEGIN {json.dumps(record)} PRISM_AGENT_TESTS_END"
    result = sandbox_service._extract_agent_tests_result(log)
    result = sandbox_service._reconcile_agent_tests_result({file_name}, result)
    return file_name, result


def _finding(file_name: str):
    return {
        "title": "未授权读取私有记录", "severity": "高", "status": "confirmed",
        "description": "未携带认证信息的请求读取了其他用户的私有数据。",
        "impact": "远程匿名攻击者可读取受保护记录。",
        "cvss_vector": VECTOR,
        "cvss_rationale": "无需认证及用户交互；同一安全域的机密性受高影响，未实证完整性及可用性影响。",
        "evidence_refs": [{"test_file": file_name, "quote": QUOTE}],
    }


def _entry(db, monkeypatch, *, mode="combined", mutate=None, failed=False, raw_verify=None,
           output=QUOTE, compacted=False):
    file_name, receipt = _receipt(mode, failed=failed, output=output)
    conclusion = {
        "passed": not failed, "execution_status": "failed" if failed else "succeeded",
        "test_mode": mode, "summary": "本次执行的检查结果", "duration_ms": 30,
        "evidence": {
            "deterministic_test_execution": {"requested_mode": mode, "status": "passed"},
            "agent_test_generation": {"status": "generated", "mode": mode, "count": 1},
            "agent_tests": receipt,
        },
    }
    if mode in {"blackbox", "combined"}:
        conclusion["evidence"]["blackbox_execution"] = {
            "status": "passed", "route": "/private", "status_code": 200,
            "route_origin": "source_route", "basis": "route_and_agent_assertions",
        }
    findings = [_finding(file_name)]
    if mutate:
        mutate(findings, conclusion)
    owner = User(username="cvss_sandbox", password="fixture", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="cvss fixture", language="python", status="active")
    db.add(project)
    db.flush()
    env = SandboxEnvironment(
        public_id="sbx_cvss_entry", project_id=project.id, owner_id=owner.id,
        agent_code="test_verifier", purpose="test", language="python", test_mode=mode,
        status="finalizing", runtime="runsc", image_ref="fixture", source_sha256="a" * 64,
        resource_policy_json="{}", agent_config_json="{}", result_json=json.dumps(conclusion),
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.add(env)
    db.commit()
    agent = Reporter()
    agent._api_key = "fixture-model"
    calls = []

    def call(user, **_kwargs):
        system = agent._system_prompt
        calls.append((system, user))
        if "对抗复检 Agent" in system:
            data = raw_verify if raw_verify is not None else json.dumps({
                "confirmed": [], "downgraded": [], "contradictions": [], "notes": "执行证据复检",
                "security_findings": findings,
            }, ensure_ascii=False)
        elif "extra_roles" in system:
            data = '{"extra_roles": []}'
        elif "报告 Agent" in system:
            data = REPORT
        else:
            data = "## 白盒结果\n执行回执已复核。" if "白盒" in system else "## 黑盒结果\n执行回执已复核。"
        return AgentResult(success=True, data=data)

    monkeypatch.setattr(agent, "call", call)
    if compacted:
        monkeypatch.setattr(agent, "_compact_evidence", lambda *_args: AgentResult(
            success=True, data={"text": "压缩摘要未包含漏洞输出", "sources": ["fixture-source"]},
        ))
    monkeypatch.setattr(agent, "_knowledge_refs", lambda *_args: "")
    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda *_args: agent)
    monkeypatch.setattr(sandbox_service, "_execution_model_guard", lambda *_args: lambda: None)
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *_args, **_kwargs: None)
    summary = sandbox_service._run_test_review_report(db, env, conclusion)
    assert summary["report_persisted"] is True
    task = db.get(ReviewTask, summary["report_task_id"])
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()
    return env, conclusion, task, report, calls


@pytest.mark.parametrize("mode", ["whitebox", "blackbox", "combined"])
def test_real_review_entry_produces_cvss_and_persists_identical_findings(db, monkeypatch, mode):
    env, conclusion, task, report, calls = _entry(db, monkeypatch, mode=mode)
    findings = conclusion["cvss_findings"]
    assert len(findings) == 1
    assert findings[0]["cvss_score"] == 7.5
    assert findings[0]["cvss_source"] == "vector"
    assert report.content_json["findings"] == findings
    persisted = json.loads(env.result_json)
    assert persisted["cvss_findings"] == findings
    assert persisted["cvss_findings"][0]["evidence_refs"][0]["quote"] == QUOTE
    assert task.score_breakdown["cvss_status"] == "complete"
    assert task.score_breakdown["components"]["cvss"]["scored_findings"] == 1
    assert task.coverage["score_basis"] == sandbox_service.SANDBOX_SCORE_VERSION
    assert task.score == report.score == task.coverage["composite_scoring"]["score"]
    assert task.score < 100
    # Mainline reader independently recomputes the score from report findings and execution checks.
    stats = report_service.load_task_issue_stats(db, [task])[task.id]
    _, score, breakdown, _ = report_service._build_task_score_facts(task, stats)
    assert score == task.score
    assert breakdown["components"] == task.score_breakdown["components"]
    verify_input = next(user for system, user in calls if "对抗复检 Agent" in system)
    assert '"cvss_evidence"' in verify_input


@pytest.mark.parametrize("change", ["quote", "file", "protocol", "compile", "missing", "no_generation"])
def test_real_review_rejects_unbound_or_unexecuted_evidence(db, monkeypatch, change):
    def mutate(findings, conclusion):
        receipt = conclusion["evidence"]["agent_tests"]
        file_name = findings[0]["evidence_refs"][0]["test_file"]
        if change == "quote":
            findings[0]["evidence_refs"][0]["quote"] = "this quotation does not occur in the execution receipt"
        elif change == "file":
            findings[0]["evidence_refs"][0]["test_file"] = "_agent_tests/invented.py"
        elif change == "protocol":
            receipt["protocol_version"] = 1
        elif change == "compile":
            receipt["file_results"][file_name].update(phase="compile", failure_kind="compile_error", exit_code=1)
        elif change == "missing":
            receipt["missing"] = [file_name]
        else:
            conclusion["evidence"]["agent_test_generation"]["status"] = "skipped"
    _, conclusion, task, report, _ = _entry(db, monkeypatch, mutate=mutate)
    assert conclusion["cvss_findings"] == report.content_json["findings"] == []
    assert report.content_json["unconfirmed_findings"][0]["status"] == "suspected"
    assert task.score_breakdown["components"]["cvss"]["scored_findings"] == 0


@pytest.mark.parametrize("vector", [None, "invalid", "CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"])
def test_real_review_keeps_evidenced_finding_but_never_trusts_bare_cvss_score(db, monkeypatch, vector):
    def mutate(findings, _conclusion):
        findings[0].update(cvss_vector=vector, cvss_score=10)
    _, conclusion, task, report, _ = _entry(db, monkeypatch, mutate=mutate)
    finding = conclusion["cvss_findings"][0]
    assert finding["cvss_vector"] is None
    assert finding["cvss_score"] is None
    assert report.content_json["findings"] == conclusion["cvss_findings"]
    assert task.score_breakdown["version"] == sandbox_service.SANDBOX_SCORE_VERSION
    assert task.score_breakdown["policy_version"] == "cvss-weighted-v1"
    assert task.score_breakdown["cvss_status"] == "unavailable"


def test_real_review_discloses_suspected_refuted_and_deduplicates_confirmed(db, monkeypatch):
    def mutate(findings, _conclusion):
        findings.extend([copy.deepcopy(findings[0]), {**findings[0], "status": "suspected"},
                         {**findings[0], "status": "refuted"}])
    _, conclusion, task, report, _ = _entry(db, monkeypatch, mutate=mutate)
    assert len(conclusion["cvss_findings"]) == 1
    assert {f["status"] for f in report.content_json["unconfirmed_findings"]} == {"suspected", "refuted"}
    assert task.score_breakdown["components"]["cvss"]["scored_findings"] == 1


def test_real_review_markdown_is_never_a_cvss_producer(db, monkeypatch):
    _, conclusion, task, report, _ = _entry(db, monkeypatch, raw_verify='{"confirmed": [], "notes": "legacy"}')
    assert conclusion["cvss_findings"] == report.content_json["findings"] == []
    assert task.score_breakdown["version"] == sandbox_service.SANDBOX_SCORE_VERSION


def test_real_review_failed_execution_remains_failed_and_caps_security_score(db, monkeypatch):
    _, conclusion, task, report, _ = _entry(db, monkeypatch, failed=True)
    assert conclusion["passed"] is False
    assert task.status == "failed"
    quality = sandbox_service._sandbox_result_score(conclusion)["score"]
    assert task.score <= quality < 100
    assert report.content_json["score_breakdown"]["components"]["quality"]["score"] == quality


def test_real_review_markdown_and_issue_counts_use_evidence_bound_findings(db, monkeypatch):
    _, _, task, report, _ = _entry(db, monkeypatch)
    assert task.total_issues == 1
    assert task.high_issues == 1
    assert report.content_json["report_issue_summary"]["total"] == 1
    assert "CVSS" in report.content_json["report_md"]


@pytest.mark.parametrize("status", [[], {}, 2])
def test_real_review_invalid_json_field_types_cannot_supply_confirmed_findings(db, monkeypatch, status):
    def mutate(findings, _conclusion):
        findings[0]["status"] = status
        findings[0]["severity"] = []
    _, conclusion, task, report, _ = _entry(db, monkeypatch, mutate=mutate)
    assert conclusion["cvss_findings"] == report.content_json["findings"] == []
    assert task.score_breakdown["components"]["cvss"]["scored_findings"] == 0


def test_terminal_republication_uses_saved_findings_and_preserves_quality_cap(db, monkeypatch):
    env, conclusion, task, report, _ = _entry(db, monkeypatch, failed=True)
    original = copy.deepcopy(report.content_json["findings"])
    env.status = "failed"
    env.result_json = json.dumps(conclusion)
    db.commit()
    sandbox_service._publish_terminal_sandbox_report(db, env)
    db.refresh(task)
    db.refresh(report)
    assert report.content_json["findings"] == original
    assert task.score == report.score
    stats = report_service.load_task_issue_stats(db, [task])[task.id]
    assert report_service._build_task_score_facts(task, stats)[1] == task.score


def test_late_incomplete_review_does_not_erase_completed_findings(db, monkeypatch):
    env, conclusion, task, report, _ = _entry(db, monkeypatch)
    original = copy.deepcopy(report.content_json["findings"])
    late = copy.deepcopy(conclusion)
    for key in ("cvss_findings", "cvss_review", "unconfirmed_findings"):
        late.pop(key, None)
    late["ai_review_status"] = "incomplete"
    result = sandbox_service._publish_sandbox_report(db, env, late, REPORT, _sync_terminal_artifact=True)
    assert result["report_persisted"] is True
    db.refresh(task)
    db.refresh(report)
    assert report.content_json["findings"] == original
    stats = report_service.load_task_issue_stats(db, [task])[task.id]
    assert report_service._build_task_score_facts(task, stats)[1] == task.score


def test_combined_remote_review_accepts_only_locally_executed_whitebox_findings(db, monkeypatch):
    def mutate(_findings, conclusion):
        conclusion["evidence"]["agent_test_generation"]["mode"] = "whitebox"
    _, conclusion, task, report, _ = _entry(db, monkeypatch, mutate=mutate)
    assert len(conclusion["cvss_findings"]) == 1
    assert conclusion["cvss_findings"][0]["evidence_refs"][0]["scope"] == "whitebox"
    assert report.content_json["findings"] == conclusion["cvss_findings"]
    assert task.score_breakdown["components"]["cvss"]["scored_findings"] == 1


def test_review_uses_actual_environment_mode_when_conclusion_does_not_duplicate_it(db, monkeypatch):
    def mutate(_findings, conclusion):
        conclusion.pop("test_mode")
    _, conclusion, task, _, _ = _entry(db, monkeypatch, mutate=mutate)
    assert conclusion["test_mode"] == "combined"
    assert task.score_breakdown["coverage_status"] != "unknown"


@pytest.mark.parametrize("verify", ["not JSON", '{"security_findings": []}', '{"security_findings": [1]}'])
def test_review_empty_or_malformed_json_cannot_infer_vector_from_markdown(db, monkeypatch, verify):
    _, conclusion, task, report, _ = _entry(db, monkeypatch, raw_verify=verify)
    assert conclusion["cvss_findings"] == report.content_json["findings"] == []
    assert task.score_breakdown["components"]["cvss"]["scored_findings"] == 0


def test_review_partial_vector_coverage_keeps_quality_cap_and_all_findings(db, monkeypatch):
    def mutate(findings, _conclusion):
        findings.append({**findings[0], "title": "第二项已确认的隐私暴露", "cvss_vector": None})
    _, conclusion, task, report, _ = _entry(db, monkeypatch, mutate=mutate, failed=True)
    assert len(conclusion["cvss_findings"]) == task.total_issues == 2
    assert task.score <= task.score_breakdown["components"]["quality"]["score"]
    assert report.content_json["score_breakdown"]["cvss_status"] == "partial"
    assert report.content_json["score_breakdown"]["components"]["cvss"]["scored_findings"] == 1


def test_generator_requests_observed_redacted_evidence_before_a_security_assertion(monkeypatch):
    from app.agents.test_case_generator_agent import TestCaseGeneratorAgent as Generator

    agent, prompts = Generator(), []
    files = [{"path": "test_first.py", "content": "assert True"},
             {"path": "test_second.py", "content": "assert True"}]
    def call(message, **_kwargs):
        prompts.append(message)
        return AgentResult(success=True, data={"files": files})
    monkeypatch.setattr(agent, "call_json", call)
    result = agent.generate(language="python", test_mode="whitebox", source_summary={
        "source_chunks": [{"path": "main.py", "text": "pass"}],
        "_compacted_source_context": {"covered_source_ids": [], "source_summaries": []},
    })
    assert result == {"files": files}
    assert "在安全断言失败前输出脱敏的实际观测" in prompts[0]
    assert "禁止输出凭据、完整私有记录或推测的 CVSS 向量" in prompts[0]


def test_compressed_review_still_verifies_against_original_execution_output(db, monkeypatch):
    _, conclusion, _, _, calls = _entry(db, monkeypatch, compacted=True)
    assert len(conclusion["cvss_findings"]) == 1
    verify_input = next(user for system, user in calls if "对抗复检 Agent" in system)
    assert "压缩摘要未包含漏洞输出" in verify_input
    original = json.loads(verify_input.split("本次可引用的执行原文（cvss_evidence）:\n", 1)[1])
    assert original["cvss_evidence"]["_agent_tests/security.py"]["output"] == QUOTE


def test_generated_test_actual_stdout_flows_through_review_and_database(db, monkeypatch, tmp_path):
    """Execute a generated assertion on a local synthetic app, then consume its receipt."""
    import subprocess
    import sys

    from app.agents.test_case_generator_agent import TestCaseGeneratorAgent as Generator

    source = "def read_private(user):\n    return {'owner': 'other', 'record': 'private record'}\n"
    test = ("from fixture_app import read_private\n"
            "response = read_private(None)\n"
            "if response['owner'] == 'other':\n"
            "    print(\"unauthenticated GET /private returned HTTP 200 with another user's \" + response['record'])\n"
            "assert response['owner'] != 'other', 'anonymous access must not read another user record'\n")
    agent = Generator()
    monkeypatch.setattr(agent, "call_json", lambda *_args, **_kwargs: AgentResult(success=True, data={
        "files": [{"path": "security.py", "content": test},
                  {"path": "test_import.py",
                   "content": "from fixture_app import read_private\nassert callable(read_private)"}],
    }))
    generated = agent.generate(language="python", test_mode="whitebox", source_summary={
        "source_chunks": [{"path": "fixture_app.py", "text": source}],
        "_compacted_source_context": {"covered_source_ids": ["fixture"], "source_summaries": []},
    })
    assert "error" not in generated
    (tmp_path / "fixture_app.py").write_text(source)
    script = tmp_path / generated["files"][0]["path"]
    script.write_text(generated["files"][0]["content"])
    run = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert run.returncode == 1
    assert QUOTE in run.stdout
    _, conclusion, task, report, _ = _entry(db, monkeypatch, mode="whitebox", failed=True, output=run.stdout)
    assert conclusion["cvss_findings"][0]["evidence_refs"][0]["quote"] == QUOTE
    assert report.content_json["findings"] == conclusion["cvss_findings"]
    assert task.total_issues == 1
    assert task.status == "failed"
    stats = report_service.load_task_issue_stats(db, [task])[task.id]
    assert report_service._build_task_score_facts(task, stats)[1] == task.score
