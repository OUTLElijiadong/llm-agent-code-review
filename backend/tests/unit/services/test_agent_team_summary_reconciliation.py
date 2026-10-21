"""最终汇总必须逐项绑定成员输出，不能把执行完成升级为结论已核验。"""

from copy import deepcopy

import pytest

from app.services.agent_team_summary import summarize_dependencies
from tests.unit.services.test_site_agent_service import actor as summary_actor  # noqa: F401
from tests.unit.services.test_site_agent_service import claimed_team


def _entry(**result):
    return {"status": "completed", "result": {"status": "completed", **result}}


def _receipt(data=None, **changes):
    output = {"capability": "forum.posts.list", "status_code": 200,
              "data": data if data is not None else {"items": [{"id": 44}], "total": 1}}
    return {"request_id": "read-posts", "request_params": {}, "output": output,
            "readback": {**deepcopy(output), "request_params": {}}, **changes}


def _checks(result, kind):
    return [item for item in result["claim_checks"] if item["kind"] == kind]


@pytest.mark.parametrize("field,value", [("total", 99), ("items", [{"id": 45}])])
def test_same_query_readback_must_match_actual_numbers_and_items(field, value):
    receipt = _receipt()
    receipt["readback"]["data"][field] = value
    result = summarize_dependencies({"verify": _entry(result_receipt=receipt)})
    business = result["business_results"][0]
    assert business["verification_scope"] == "readback_mismatch"
    assert business["completed"] is False
    assert result["status"] == "failed"
    assert any(item["status"] == "conflict" for item in _checks(result, "business_result"))


@pytest.mark.parametrize("status_code", [202, 400, 500, True, None])
def test_unsuccessful_or_pending_receipt_cannot_be_verified(status_code):
    receipt = _receipt()
    receipt["output"]["status_code"] = status_code
    result = summarize_dependencies({"verify": _entry(result_receipt=receipt)})
    assert result["status"] == "failed"
    assert result["business_results"][0]["completed"] is False
    assert result["business_results"][0]["verification_scope"] == "invalid_execution_receipt"


def test_declared_scope_without_readback_is_not_proof():
    receipt = _receipt(verification_scope="same_query_readback")
    receipt.pop("readback")
    result = summarize_dependencies({"verify": _entry(result_receipt=receipt)})
    assert result["business_results"][0]["verification_scope"] == "execution_receipt_only"
    assert _checks(result, "business_result")[0]["status"] == "unverified"
    assert result["verification_status"] == "unverified"


def test_free_text_numbers_findings_and_success_are_explicitly_unverified():
    text = "已核验 999 个文件，发现 12 个漏洞，全部修复并测试通过。"
    result = summarize_dependencies({"worker": _entry(summary=text)})
    assert result["status"] == "completed"  # 终态基于持久化状态，不能凭措辞反转。
    assert result["verification_status"] == "unverified"
    check = _checks(result, "member_summary")[0]
    assert check["status"] == "unverified" and check["source_task"] == "worker"
    assert "未逐项核验" in result["artifacts"][0]["data"]["outcomes"][0]["summary"]
    assert "汇总保留问题 0 条" not in result["summary"]
    assert "不能推断无问题" in result["summary"]


def test_matching_readback_retains_items_and_field_provenance():
    dependencies = {"verify": _entry(result_receipt=_receipt())}
    original = deepcopy(dependencies)
    result = summarize_dependencies(dependencies)
    business = result["business_results"][0]
    assert result["status"] == "completed"
    assert business["verification_scope"] == "same_query_readback"
    checks = _checks(result, "business_result")
    assert any(item["path"].endswith("/data/total") and item["value"] == 1
               and item["status"] == "supported" for item in checks)
    assert all(item["source_task"] == "verify" and item["source_path"].startswith("/result/")
               for item in checks)
    assert dependencies == original


def test_json_boolean_is_not_the_number_one():
    receipt = _receipt()
    receipt["readback"]["data"]["total"] = True
    result = summarize_dependencies({"verify": _entry(result_receipt=receipt)})
    assert result["status"] == "failed"


@pytest.mark.parametrize("coverage", [
    {"semantic_complete": False}, {"static_complete": False}, {"truncated": True},
    {"failed_chunks": 1}, {"semantic_failed_batch_count": 2}, {"stage": "failed"},
])
def test_incomplete_member_coverage_is_explicit_even_if_task_completed(coverage):
    result = summarize_dependencies({"audit": _entry(coverage=coverage, findings=[])})
    assert result["status"] == "failed"
    assert result["bounded_finding_tasks"] == ["audit"]
    assert result["verification_status"] == "incomplete"
    assert _checks(result, "coverage")


def test_nested_success_does_not_overwrite_explicit_failed_coverage():
    result = summarize_dependencies({"audit": _entry(
        coverage={"semantic_complete": False},
        evidence=[{"data": {"coverage": {"semantic_complete": True}}}],
    )})
    assert result["status"] == "failed"
    assert result["coverage_summary"][0]["semantic_complete"] is False


def test_projected_coverage_cannot_invent_member_numbers():
    member = _entry(coverage={"total_files": 3, "processed_files": 2})
    member["coverage_summary"] = {"total_files": 999, "processed_files": 999, "scan_complete": True}
    result = summarize_dependencies({"audit": member})
    assert result["coverage_summary"][0]["total_files"] == 3
    assert result["coverage_summary"][0]["processed_files"] == 2
    assert "scan_complete" not in result["coverage_summary"][0]
    assert result["status"] == "failed"


def test_failed_member_evidence_is_retained_without_verified_success():
    member = _entry(findings=[{"title": "真实返回的问题", "file_name": "a.py"}])
    member["status"] = "failed"
    result = summarize_dependencies({"audit": member})
    assert result["status"] == "failed"
    assert result["unique_finding_count"] == 1
    assert result["incomplete_tasks"] == ["audit"]
    assert _checks(result, "finding")[0]["status"] == "incomplete"
    assert "audit" in result["summary"]


def test_each_deduplicated_finding_has_actual_member_sources():
    finding = {"project_id": 7, "title": "SQL注入", "file_name": "a.py", "line_number": 9}
    result = summarize_dependencies({key: _entry(findings=[deepcopy(finding)]) for key in ("a", "b")})
    checks = _checks(result, "finding")
    assert result["unique_finding_count"] == len(checks) == 1
    assert checks[0]["status"] == "supported"
    assert checks[0]["source_tasks"] == ["a", "b"]
    assert checks[0]["verification_scope"] == "member_output_only"
    assert len(checks[0]["sources"]) == 2


def test_nested_summary_cannot_launder_fabricated_finding_or_count():
    actual = _entry(findings=[{"project_id": 7, "title": "真实问题", "file_name": "a.py"}])
    first = summarize_dependencies({"worker": actual})
    first["findings"].append({"project_id": 7, "title": "凭空新增", "file_name": "fake.py"})
    first["unique_finding_count"] = 99
    result = summarize_dependencies({"verifier": {"status": "completed", "result": first}})
    assert result["status"] == "failed"
    assert result["unique_finding_count"] == 1
    assert all(item["title"] != "凭空新增" for item in result["findings"])
    assert any(item["status"] == "conflict" for item in result["claim_checks"])


@pytest.mark.parametrize("text", ["已完成", "核验完成", "工作结束，结果见附件", "仅给出分析建议，未做实测"])
def test_completed_task_is_never_reclassified_from_summary_wording(text):
    result = summarize_dependencies({"worker": _entry(summary=text, findings=[])})
    assert result["status"] == "completed"
    assert _checks(result, "task_status")[0]["status"] == "supported"


def test_no_members_is_explicitly_uncovered():
    result = summarize_dependencies({})
    assert result["status"] == "failed"
    assert result["verification_status"] == "incomplete"
    assert "无成员输出" in result["summary"]


def test_nested_summary_does_not_promote_execution_receipt_to_verified():
    receipt = _receipt()
    receipt.pop("readback")
    first = summarize_dependencies({"worker": _entry(result_receipt=receipt)})
    result = summarize_dependencies({"summarizer": {"status": "completed", "result": first}})
    assert result["status"] == "completed"
    assert result["business_results"][0]["verification_scope"] == "execution_receipt_only"
    assert _checks(result, "business_result")[0]["status"] == "unverified"


def test_nested_failed_member_finding_is_still_incomplete():
    first = summarize_dependencies({"worker": {"status": "failed", "result": {
        "status": "failed", "findings": [{"title": "部分证据", "file_name": "a.py"}],
    }}})
    result = summarize_dependencies({"summarizer": {"status": "completed", "result": first}})
    assert result["status"] == "failed"
    assert _checks(result, "finding")[0]["status"] == "incomplete"


def test_valid_nested_summary_preserves_formal_review_reference():
    first = summarize_dependencies({"review": {
        **_entry(findings=[{"title": "证据", "file_name": "a.py"}]), "verified_review_task_id": 17,
    }})
    result = summarize_dependencies({"summarizer": {"status": "completed", "result": first}})
    assert result["status"] == "completed"
    assert result["references"][0]["route"] == "/reviews/17"


def test_contradictory_coverage_values_cannot_both_be_supported():
    result = summarize_dependencies({"worker": _entry(
        coverage={"total_files": 3}, evidence=[{"data": {"coverage": {"total_files": 99}}}],
    )})
    assert result["status"] == "failed"
    checks = _checks(result, "coverage")
    assert any(check["status"] == "conflict" for check in checks)
    assert all(check["value"] == 3 for check in checks if check["path"].endswith("/total_files"))


def test_claimed_finding_total_is_checked_against_actual_items():
    result = summarize_dependencies({"review": _entry(
        findings=[{"title": "证据", "file_name": "a.py"}], finding_count_total=99,
    )})
    assert result["status"] == "failed"
    assert result["unique_finding_count"] == 1
    assert any(check["status"] == "conflict" for check in _checks(result, "finding_count"))


@pytest.mark.parametrize("bad_first", [True, False])
def test_repeated_receipts_never_hide_a_conflict_with_a_later_success(bad_first):
    good, bad = _receipt(), _receipt()
    bad["readback"]["data"]["total"] = 99
    receipts = [bad, good] if bad_first else [good, bad]
    result = summarize_dependencies({"verify": _entry(result_receipts=receipts)})
    assert result["status"] == "failed"
    assert result["business_results"][0]["completed"] is False
    assert _checks(result, "business_result")[0]["status"] == "conflict"


def test_worker_receipt_and_verifier_readback_are_merged_without_losing_scope():
    worker = _receipt()
    worker.pop("readback")
    result = summarize_dependencies({"a_worker": _entry(result_receipt=worker),
                                     "z_verify": _entry(result_receipt=_receipt())})
    assert result["status"] == "completed"
    assert len(result["business_results"]) == 1
    assert _checks(result, "business_result")[0]["status"] == "supported"


def test_explicitly_different_readback_query_is_rejected():
    receipt = _receipt(request_params={"page": 2})
    receipt["readback"]["request_params"] = {"page": 1}
    result = summarize_dependencies({"verify": _entry(result_receipt=receipt)})
    assert result["status"] == "failed"


def test_nested_summary_without_findings_cannot_invent_a_verified_zero():
    first = summarize_dependencies({"worker": _entry(coverage={"complete": True})})
    result = summarize_dependencies({"summarizer": {"status": "completed", "result": first}})
    assert result["status"] == "completed"
    assert _checks(result, "finding_count")[-1]["status"] == "unverified"
    assert "不能推断无问题" in result["summary"]


def _resolve(value, path):
    for part in path.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def test_conflicting_duplicate_retains_the_correct_data_source_path():
    first, second = _receipt(), _receipt(data={"items": [{"id": 99}], "total": 99})
    dependencies = {"verify": _entry(result_receipts=[first, second])}
    result = summarize_dependencies(dependencies)
    assert result["status"] == "failed"
    for check in _checks(result, "business_result"):
        if "/data/" in check["path"]:
            assert _resolve(dependencies[check["source_task"]], check["source_path"]) == check["value"]


def test_nested_numeric_provenance_resolves_to_the_actual_leaf_output():
    original = summarize_dependencies({"worker": _entry(result_receipt=_receipt())})
    dependencies = {"summarizer": {"status": "completed", "result": original}}
    result = summarize_dependencies(dependencies)
    check = next(check for check in _checks(result, "business_result") if check["path"].endswith("/data/total"))
    assert _resolve(dependencies[check["source_task"]], check["source_path"]) == check["value"] == 1


def test_public_finding_preview_does_not_replace_full_member_output():
    title = "完整问题依据" * 500
    member = _entry(findings=[{"title": title, "file_name": "a.py"}])
    member["finding_summary"] = {"items": [{"title": "[TRUNCATED]", "file_name": "a.py"}],
                                 "omitted_count": 0, "source_truncated": False}
    result = summarize_dependencies({"review": member})
    assert result["status"] == "completed"
    assert result["findings"][0]["title"] == title


def test_report_like_completed_coverage_does_not_verify_the_textual_conclusion():
    text = "L110-190、L318+ 未逐行核，不构成完整报告。"
    result = summarize_dependencies({"review": _entry(
        summary=text, coverage={"stage": "complete", "total_files": 1, "processed_files": 1},
        findings=[{"title": "成员发现", "file_name": "a.py"}],
    )})
    assert result["status"] == "completed"
    assert result["verification_status"] == "unverified"
    assert _checks(result, "member_summary")[0]["status"] == "unverified"
    assert _checks(result, "finding")[0]["verification_scope"] == "member_output_only"


def test_explicit_partial_coverage_is_not_completed():
    result = summarize_dependencies({"review": _entry(coverage={"coverage_status": "partial"})})
    assert result["status"] == "failed"
    assert result["bounded_finding_tasks"] == ["review"]


def test_same_target_readback_uses_existing_capability_contract():
    receipt = {"capability": "forum.posts.create", "scope": "user", "request_params": {"title": "标题"},
               "output": {"capability": "forum.posts.create", "status_code": 201,
                          "data": {"id": 44, "title": "标题"}},
               "readback": {"capability": "forum.posts.get", "status_code": 200,
                            "request_params": {"post_id": 44}, "data": {"id": 44, "title": "标题"}}}
    result = summarize_dependencies({"verify": _entry(result_receipt=receipt)})
    assert result["status"] == "completed"
    assert result["business_results"][0]["verification_scope"] == "same_target_readback"
    receipt["readback"]["data"]["id"] = 45
    assert summarize_dependencies({"verify": _entry(result_receipt=receipt)})["status"] == "failed"


@pytest.mark.parametrize("read_status,expected", [(404, "completed"), (200, "failed"), (True, "failed")])
def test_delete_requires_a_registered_target_and_404(read_status, expected):
    receipt = {"capability": "forum.posts.delete", "scope": "user", "request_params": {"post_id": 44},
               "output": {"capability": "forum.posts.delete", "status_code": 200, "data": {}},
               "readback": {"status_code": read_status, "data": None,
                            "capability": "forum.posts.get", "request_params": {"post_id": 44}}}
    assert summarize_dependencies({"verify": _entry(result_receipt=receipt)})["status"] == expected


@pytest.mark.parametrize("count", [21, 201])
def test_nested_summary_preserves_all_findings_beyond_public_preview(count):
    findings = [{"project_id": 7, "file_name": "a.py", "line_number": index + 1,
                 "title": f"问题{index}"} for index in range(count)]
    first = summarize_dependencies({"worker": _entry(findings=findings)})
    result = summarize_dependencies({"summarizer": {"status": "completed", "result": first}})
    assert result["status"] == "completed"
    assert len(result["findings"]) == 20
    assert len(result["artifacts"][0]["data"]["findings"]) == result["unique_finding_count"] == count
    assert len(_checks(result, "finding")) == count
    assert _checks(result, "finding")[-1]["verification_scope"] == "member_output_only"


def test_nested_summary_rejects_duplicate_dependency_evidence():
    first = summarize_dependencies({"worker": _entry(findings=[])})
    first["evidence"].append(deepcopy(first["evidence"][0]))
    first["evidence"][-1]["data"]["result"]["findings"] = [{"title": "伪造问题"}]
    result = summarize_dependencies({"summarizer": {"status": "completed", "result": first}})
    assert result["status"] == "failed"
    assert result["unique_finding_count"] == 0
    assert result["conflicting_tasks"] == ["summarizer"]


def test_nested_summary_depth_limit_is_explicitly_incomplete():
    result = summarize_dependencies({"worker": _entry(findings=[])})
    for _ in range(10):
        result = {"status": "completed", "artifacts": [{"type": "agent_team_summary", "data": {}}],
                  "evidence": [{"source": "agent_team_dependency", "task_key": "nested",
                                "data": {"status": "completed", "result": result}}]}
    final = summarize_dependencies({"summarizer": {"status": "completed", "result": result}})
    assert final["status"] == "failed"
    assert final["verification_status"] == "incomplete"
    assert final["bounded_finding_tasks"] == ["summarizer"]


@pytest.mark.parametrize("field", ["incomplete_tasks", "bounded_finding_tasks", "verification_status"])
def test_nested_summary_cannot_self_certify_its_artifact_metadata(field):
    first = summarize_dependencies({"worker": _entry(summary="未经核验的结论", findings=[])})
    first["artifacts"][0]["data"][field] = "supported" if field == "verification_status" else ["invented"]
    final = summarize_dependencies({"summarizer": {"status": "completed", "result": first}})
    assert final["status"] == "failed"
    assert final["conflicting_tasks"] == ["summarizer"]


def test_business_field_provenance_uses_existing_json_pointer_escaping():
    dependencies = {"verify": _entry(result_receipt=_receipt(data={"part/total~": 3}))}
    result = summarize_dependencies(dependencies)
    check = next(check for check in _checks(result, "business_result") if "/data/" in check["path"])
    assert _resolve(result, check["path"]) == _resolve(
        dependencies[check["source_task"]], check["source_path"],
    ) == check["value"] == 3


def test_reporter_result_and_claim_checks_use_existing_owner_scoped_tool(db, request, monkeypatch):
    import json

    from app.agents import orchestrator
    from app.agents.orchestrator import Orchestrator
    from app.models.agent_team import AgentTeam, AgentTeamMember, AgentTeamTask
    from app.services import agent_mesh_dispatcher, agent_team_service

    current_user = request.getfixturevalue("summary_actor")
    created, claim = claimed_team(db, current_user, capability="forum.posts.list", params={"page": 1})
    team = db.get(AgentTeam, created["team_id"])
    worker = db.get(AgentTeamTask, claim["task_id"])
    worker.status, worker.result_json = "completed", json.dumps(_entry(result_receipt=_receipt())["result"])
    summarizer = db.query(AgentTeamTask).filter_by(team_id=team.id, task_key="verify").one()
    member = db.get(AgentTeamMember, summarizer.member_id)
    member.address, member.role = "agent:reporter", "summarizer"
    dependencies = agent_team_service._dependency_context(db, team, summarizer)
    orch = Orchestrator.__new__(Orchestrator)
    orch._db, orch._user = db, current_user
    monkeypatch.setattr(orchestrator, "get_request_orchestrator", lambda *_args, **_kwargs: orch)
    report = agent_mesh_dispatcher._runtime_handler(db, current_user, "reporter", {
        "payload": {"dependency_context": dependencies},
    })
    assert report["status"] == "completed"
    summarizer.status, summarizer.result_json = "completed", json.dumps(report)
    team.status, team.summary_json = "completed", "{}"
    db.commit()
    detail = orch.get_agent_team(team.id)
    assert detail.success and detail.data["status"] == "completed"
    assert detail.data["summary"]["final_result"]["claim_checks"] == report["claim_checks"]
    page = orch.get_agent_team(team.id, task_id=summarizer.id, result_path="/claim_checks", limit=20)
    assert page.success and page.data["items"] == report["claim_checks"]
    # 大结果折成引用后，仍沿相同任务按路径取回。
    report["evidence"].append({"source": "large_fixture", "data": "x" * 30000})
    summarizer.result_json = json.dumps(report)
    db.commit()
    final_result = orch.get_agent_team(team.id).data["summary"]["final_result"]
    assert final_result["result_reference"]["task_id"] == summarizer.id
    assert "claim_checks" not in final_result
    page = orch.get_agent_team(team.id, task_id=summarizer.id, result_path="/claim_checks", limit=20)
    assert page.success and page.data["items"] == report["claim_checks"]


@pytest.mark.parametrize("change", [
    {"request_params": {"post_id": 45}}, {"capability": "forum.posts.list"},
])
def test_delete_readback_cannot_use_a_different_target_or_capability(change):
    receipt = {"capability": "forum.posts.delete", "request_params": {"post_id": 44},
               "output": {"capability": "forum.posts.delete", "status_code": 200, "data": {}},
               "readback": {"status_code": 404, "data": None, **change}}
    result = summarize_dependencies({"verify": _entry(result_receipt=receipt)})
    assert result["status"] == "failed"
    assert _checks(result, "business_result")[0]["status"] == "conflict"


def test_nested_business_result_inherits_outer_member_failure():
    first = summarize_dependencies({"worker": _entry(result_receipt=_receipt())})
    final = summarize_dependencies({"summarizer": {"status": "failed", "result": first}})
    assert final["status"] == "failed"
    assert final["business_results"][0]["completed"] is False
    assert all(check["status"] == "incomplete" for check in _checks(final, "business_result"))


@pytest.mark.parametrize("identity", [
    {}, {"capability": "forum.posts.get"}, {"request_params": {"post_id": 44}},
])
def test_delete_missing_actual_readback_identity_is_unverified(identity):
    receipt = {"scope": "user", "capability": "forum.posts.delete", "request_params": {"post_id": 44},
               "output": {"capability": "forum.posts.delete", "status_code": 200, "data": {}},
               "readback": {"status_code": 404, **identity}}
    result = summarize_dependencies({
        "verify": _entry(result_receipt=receipt, coverage={"complete": True}, findings=[]),
    })
    assert result["verification_status"] == "unverified"
    assert result["business_results"][0]["verification_scope"] == "execution_receipt_only"
    assert _checks(result, "business_result")[0]["status"] == "unverified"


@pytest.mark.parametrize("field", ["capability", "request_params"])
def test_query_missing_actual_readback_identity_is_unverified(field):
    receipt = _receipt()
    receipt["readback"].pop(field)
    result = summarize_dependencies({"verify": _entry(result_receipt=receipt)})
    assert _checks(result, "business_result")[0]["status"] == "unverified"


@pytest.mark.parametrize("coverage", [
    {"coverage_ratio": 0}, {"coverage_ratio": 0.25}, {"coverage_ratio": 0.9999},
    {"semantic_char_coverage_ratio": 0.1},
    {"semantic_candidate_source_chars": 1000, "semantic_source_chars": 10},
    {"complete": True, "coverage_ratio": 0.5},
    {"semantic_complete": True, "semantic_char_coverage_ratio": 0.5},
])
def test_explicit_partial_ratios_or_semantic_chars_cannot_certify_complete_coverage(coverage):
    result = summarize_dependencies({"audit": _entry(coverage=coverage, findings=[])})
    assert result["status"] == "failed"
    assert result["verification_status"] == "incomplete"
    assert result["bounded_finding_tasks"] == ["audit"]
    assert all(item["status"] == "incomplete" for item in _checks(result, "coverage"))


@pytest.mark.parametrize("coverage", [
    {"coverage_ratio": 1}, {"semantic_char_coverage_ratio": 1.0},
    {"semantic_candidate_source_chars": 10, "semantic_source_chars": 10},
    {"semantic_candidate_source_chars": 0, "semantic_source_chars": 0},
])
def test_complete_ratios_and_semantic_chars_keep_the_supported_boundary(coverage):
    result = summarize_dependencies({"audit": _entry(coverage=coverage, findings=[])})
    assert result["status"] == "completed"
    assert result["verification_status"] == "supported"
    assert result["bounded_finding_tasks"] == []


@pytest.mark.parametrize("field", ["coverage_ratio", "semantic_char_coverage_ratio"])
def test_later_partial_ratio_cannot_be_hidden_by_earlier_complete_projection(field):
    result = summarize_dependencies({"audit": _entry(
        coverage={field: 1}, evidence=[{"data": {"coverage": {field: 0.25}}}], findings=[],
    )})
    assert result["status"] == "failed"
    assert result["bounded_finding_tasks"] == ["audit"]
    assert result["coverage_summary"][0][field] == 0.25


@pytest.mark.parametrize("failed_first", [True, False])
@pytest.mark.parametrize("failed_status", ["failed", "blocked"])
def test_duplicate_receipt_member_failure_is_independent_of_task_key_order(failed_first, failed_status):
    failed = {**_entry(result_receipt=_receipt()), "status": failed_status}
    passed = _entry(result_receipt=_receipt())
    ordered = [failed, passed] if failed_first else [passed, failed]
    result = summarize_dependencies(dict(zip(("a", "z"), ordered)))
    assert result["status"] == "failed"
    assert len(result["business_results"]) == 1
    assert result["business_results"][0]["completed"] is False
    assert len(_checks(result, "business_result")[0]["sources"]) == 2
    assert all(item["status"] == "incomplete" for item in _checks(result, "business_result"))


@pytest.mark.parametrize("capability,params", [
    ("forum.posts.delete", {"post_id": 44}),
    ("forum.posts.create", {"title": "实际公告", "content": "版本说明"}),
    ("forum.posts.list", {"page": 2}),
])
def test_site_verifier_records_the_actual_readback_call_identity(db, request, monkeypatch, capability, params):
    from app.services import agent_team_service, site_agent_service
    from tests.unit.services.test_site_agent_service import run

    calls = []
    current_user = request.getfixturevalue("summary_actor")

    async def api(user, spec, actual_params, **kwargs):
        calls.append((spec.code, deepcopy(actual_params)))
        if spec.code == "forum.posts.get" and capability == "forum.posts.delete":
            raise site_agent_service.admin_capability_service.AdminCapabilityError("not found", status_code=404)
        data = ({"items": [{"id": 44}], "total": 1} if spec.code == "forum.posts.list"
                else {"id": 44, "title": "实际公告", "content": "版本说明"})
        return {"capability": spec.code, "status_code": 200, "data": data}

    monkeypatch.setattr(site_agent_service.admin_capability_service, "execute_api", api)
    created, claim = claimed_team(db, current_user, capability=capability, params=params, confirmed=True)
    worker = run(db, current_user, created, claim)
    assert worker["status"] == "completed"
    agent_team_service.complete_task(db, created["team_id"], claim["task_id"],
                                    lease_token=claim["lease_token"], result=worker, success=True)
    verify_claim = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    verified = run(db, current_user, created, verify_claim)
    assert verified["status"] == "completed"
    readback = verified["result_receipt"]["readback"]
    assert (readback["capability"], readback["request_params"]) == calls[-1]
    if capability == "forum.posts.delete":
        assert readback["status_code"] == 404
    result = summarize_dependencies({"verify": _entry(**{key: value for key, value in verified.items()
                                                       if key != "status"})})
    assert _checks(result, "business_result")[0]["status"] == "supported"
