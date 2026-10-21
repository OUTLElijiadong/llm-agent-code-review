"""按团队依赖证据汇总；不把运行完成等同于漏洞已复现。"""

from __future__ import annotations

import json
from typing import Any

from app.services.agent_team_projection import pointer_child


def _result_blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [block for _, block in _blocks_with_paths(result)]


def dependency_finding_summary(result: dict[str, Any], *, redact: Any) -> dict[str, Any]:
    """从持久化结果提取有界标量；逐项脱敏，避免外层事件深度截断整个问题。"""
    fields = ("issue_id", "file_id", "file_name", "file_path", "line_number", "line", "title",
              "severity", "issue_type", "project_id", "task_id")
    findings = []
    omitted = 0
    source_truncated = False
    for block in _result_blocks(result):
        compliance = block.get("compliance")
        compliance = compliance if isinstance(compliance, dict) else {}
        source_truncated = source_truncated or bool(
            block.get("findings_truncated") or compliance.get("findings_truncated")
        )
        for item in block.get("findings") or block.get("issues") or []:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            if len(findings) >= 200:
                omitted += 1
                continue
            minimal = {key: item[key] for key in fields if key in item
                       and isinstance(item[key], (str, int, float)) and not isinstance(item[key], bool)}
            if not minimal.get("project_id") and isinstance(block.get("project_id"), int):
                minimal["project_id"] = block["project_id"]
            findings.append(redact(minimal))
    return {"items": findings, "omitted_count": omitted, "source_truncated": source_truncated}


def _coverage_values(result: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """只投影已持久化的覆盖标量；未知字段留空，不从完成状态推断覆盖。"""
    numeric_fields = (
        "total_files", "processed_files", "total_chunks", "completed_chunks", "failed_chunks",
        "total_file_count", "scanned_file_count", "skipped_file_count", "coverage_ratio",
        "static_scanned_file_count", "semantic_file_count", "semantic_candidate_source_chars",
        "semantic_attempted_source_chars", "semantic_source_chars", "semantic_failed_batch_count",
        "semantic_char_coverage_ratio", "archive_text_source_chars",
        "included_file_count", "source_chars_included", "omitted_files",
        "max_files", "max_file_chars", "max_context_chars",
    )
    values = []
    for block_path, block in _blocks_with_paths(result):
        sources = [(block_path, block)]
        sources.extend((f"{block_path}/{name}", block[name]) for name in ("coverage", "compliance")
                       if isinstance(block.get(name), dict))
        for path, source in sources:
            for field in numeric_fields:
                value = source.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values.append((field, f"{path}/{field}", value))
            for field in ("semantic_complete", "static_complete", "truncated", "complete",
                          "formal_review", "scan_complete", "semantic_execution_complete",
                          "audit_inputs_complete"):
                if isinstance(source.get(field), bool):
                    values.append((field, f"{path}/{field}", source[field]))
            if source.get("coverage_status") in {"complete", "partial", "failed", "unknown"}:
                values.append(("coverage_status", f"{path}/coverage_status", source["coverage_status"]))
            if source.get("execution_mode") == "analysis_only":
                values.append(("execution_mode", f"{path}/execution_mode", "analysis_only"))
            if isinstance(source.get("scan_mode"), str) and source["scan_mode"] in {"full", "static_full", "triage"}:
                values.append(("scan_mode", f"{path}/scan_mode", source["scan_mode"]))
            if isinstance(source.get("stage"), str) and source["stage"] in {
                "complete", "failed", "cancelled", "running", "queued",
            }:
                values.append(("coverage_stage", f"{path}/stage", source["stage"]))
    return values


def _blocks_with_paths(result: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    blocks = [("/result", result)]
    for collection in ("evidence", "artifacts"):
        for index, item in enumerate(result.get(collection) or []):
            if isinstance(item, dict):
                path = f"/result/{collection}/{index}"
                blocks.append((path + "/data", item["data"]) if isinstance(item.get("data"), dict)
                              else (path, item))
    return blocks


def _json(value: Any) -> str:
    """JSON equality must distinguish booleans from numbers (True != 1)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _incomplete_coverage(coverage: dict[str, Any]) -> bool:
    if any(coverage.get(flag) is False for flag in (
        "semantic_complete", "static_complete", "complete", "scan_complete",
        "semantic_execution_complete", "audit_inputs_complete",
    )) or coverage.get("truncated") is True:
        return True
    if coverage.get("coverage_status") in {"partial", "failed", "unknown"}:
        return True
    if coverage.get("coverage_stage") in {"failed", "cancelled", "running", "queued"}:
        return True
    if any(coverage.get(key, 0) > 0 for key in (
        "failed_chunks", "semantic_failed_batch_count", "omitted_files",
    )):
        return True
    if any(coverage.get(field, 1) < 1 for field in ("coverage_ratio", "semantic_char_coverage_ratio")):
        return True
    return any(left in coverage and right in coverage and coverage[left] < coverage[right]
               for left, right in (("processed_files", "total_files"),
                                   ("completed_chunks", "total_chunks"),
                                   ("scanned_file_count", "total_file_count"),
                                   ("semantic_source_chars", "semantic_candidate_source_chars")))


def dependency_coverage_summary(result: dict[str, Any]) -> dict[str, Any]:
    """保留成员的实际覆盖；后续成功标志不能覆盖已有失败。"""
    projected: dict[str, Any] = {}
    for field, _, value in _coverage_values(result):
        if field not in projected:
            projected[field] = value
        elif isinstance(value, bool):
            projected[field] = (projected[field] or value) if field == "truncated" else (projected[field] and value)
        elif field in {"coverage_ratio", "semantic_char_coverage_ratio"}:
            projected[field] = min(projected[field], value)
        elif field in {"failed_chunks", "semantic_failed_batch_count", "omitted_files"}:
            projected[field] = max(projected[field], value)
        elif field in {"coverage_stage", "coverage_status"} and value != "complete":
            projected[field] = value
    return projected


def _receipt_verification(receipt: dict[str, Any]) -> tuple[str, str, str]:
    """复用站点 verifier 的注册表读回契约；此处只核对成员输出，不重做执行账本认证。"""
    from app.services.site_agent_service import ADMIN_CAPABILITIES, USER_CAPABILITIES, _readback_spec

    output = receipt.get("output")
    if not isinstance(output, dict):
        return "invalid_execution_receipt", "conflict", "缺少结构化执行回执"
    code = output.get("status_code")
    capability = output.get("capability")
    if (type(code) is not int or not 200 <= code < 300 or code == 202 or "data" not in output
            or not capability or ("capability" in receipt and receipt["capability"] != capability)
            or ("status_code" in receipt and receipt["status_code"] != code)):
        return "invalid_execution_receipt", "conflict", "执行回执并非已完成的成功响应"
    readback = receipt.get("readback")
    if not isinstance(readback, dict):
        return "execution_receipt_only", "unverified", "只有执行回执，未提供独立读回"
    scope = receipt.get("scope", "user")
    registry = {**USER_CAPABILITIES, **ADMIN_CAPABILITIES} if scope == "admin" else USER_CAPABILITIES
    spec = registry.get(capability) if scope in {"user", "admin"} else None
    params = receipt.get("request_params", {})
    if spec is None or not isinstance(params, dict):
        return "execution_receipt_only", "unverified", "缺少可匹配的注册表读回契约"
    pair = _readback_spec(scope, spec, params, output)
    if pair is None:
        return "execution_receipt_only", "unverified", "该执行仅有同步 API 回执，不能证明页面或目标状态"
    if ("request_params" in readback and _json(readback["request_params"]) != _json(pair[1])):
        return "readback_mismatch", "conflict", "读回使用了不同的筛选、排序、分页或目标参数"
    if "capability" in readback and readback["capability"] != pair[0].code:
        return "readback_mismatch", "conflict", "读回 capability 与既有契约不符"
    if "capability" not in readback or "request_params" not in readback:
        return "execution_receipt_only", "unverified", "读回缺少实际调用 capability 或完整参数，目标身份未确认"
    if spec.method == "DELETE":
        if type(readback.get("status_code")) is int and readback["status_code"] == 404:
            return "same_target_readback", "supported", "删除目标按既有 verifier 契约读回 404"
        return "readback_mismatch", "conflict", "删除目标仍可读或缺少 404 回执"
    read_code = readback.get("status_code")
    if (type(read_code) is not int or not 200 <= read_code < 300 or read_code == 202
            or readback.get("capability") != pair[0].code or "data" not in readback):
        return "readback_mismatch", "conflict", "读回状态或 capability 与既有契约不符"
    original, data = output["data"], readback["data"]
    if spec.method == "GET":
        if _json(original) != _json(data):
            return "readback_mismatch", "conflict", "查询条目、分页或数值与原回执不一致"
        return "same_query_readback", "supported", "成员的同查询读回与执行输出逐项一致"
    if not isinstance(original, dict) or not isinstance(data, dict):
        return "execution_receipt_only", "unverified", "读回未提供可核对的目标字段"
    if (("id" in original and _json(original["id"]) != _json(data.get("id")))
            or any(key in data and _json(value) != _json(data[key]) for key, value in params.items())):
        return "readback_mismatch", "conflict", "目标 ID 或请求字段与读回不一致"
    if "id" not in original and not any(key in data for key in params):
        return "execution_receipt_only", "unverified", "未返回可核对的目标 ID 或请求字段"
    return "same_target_readback", "supported", "目标 ID 及返回的请求字段与成员读回一致"


def _finding_key(item: dict[str, Any], project_id: Any, task_key: str, index: int) -> tuple:
    return (
        str(item.get("project_id") or project_id or f"task:{task_key}"),
        str(item.get("file_id") or item.get("file_path") or item.get("file_name")
            or f"unlocated:{task_key}:{index}"),
        str(item.get("line_number") or item.get("line") or ""),
        str(item["title"]), str(item.get("severity") or ""),
    )


def summarize_dependencies(dependencies: dict[str, Any], *, _depth: int = 0) -> dict[str, Any]:
    outcomes, evidence, checks, failed, bounded_tasks, conflicts, coverage_summary = [], [], [], [], [], [], []
    findings: dict[tuple, dict[str, Any]] = {}
    finding_sources: dict[tuple, list[dict[str, Any]]] = {}
    references: dict[tuple, dict[str, Any]] = {}
    businesses: dict[tuple, dict[str, Any]] = {}
    finding_data_available = False

    def check(kind, path, value, source_task, source_path, status="supported",
              scope="member_output_only", reason="与成员结构化输出一致", **extra):
        checks.append({"kind": kind, "path": path, "value": value, "status": status,
                       "source_task": source_task, "source_path": source_path,
                       "verification_scope": scope, "reason": reason, **extra})

    for task_key, raw_entry in sorted(dependencies.items()):
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        raw_result = entry.get("result")
        raw_result = raw_result if isinstance(raw_result, dict) else {}
        result = raw_result
        status = str(entry.get("status") or "unknown")
        result_status = str(raw_result.get("status") or "unknown")
        complete = status == "completed" and result_status == "completed"
        if not complete:
            failed.append(task_key)
        check("task_status", f"/outcomes/{len(outcomes)}/result_status", result_status, task_key,
              "/result/status", "supported" if complete else "incomplete",
              reason="任务及结果的结构化状态均为 completed" if complete else "成员任务或结果尚未完成")
        text = raw_result.get("summary")
        outcomes.append({"task_key": task_key, "status": status, "result_status": result_status,
                         "summary": f"成员原文（未逐项核验）：{text}" if text else "缺少结果说明"})
        if text:
            check("member_summary", f"/outcomes/{len(outcomes)-1}/summary", str(text), task_key,
                  "/result/summary", "unverified", reason="自由文字未提供逐条结构化依据，不能作为已核验结论")
        evidence.append({"source": "agent_team_dependency", "task_key": task_key, "data": entry})

        # 多级 summarizer 只能从其实际依赖重新计算，不能用自己的 summary 产物自证。
        nested = any(isinstance(item, dict) and item.get("type") == "agent_team_summary"
                     for item in raw_result.get("artifacts") or [])
        nested_sources = {}
        nested_checks = {}
        if nested:
            source_dependencies = {}
            prefixes = {}
            for index, item in enumerate(raw_result.get("evidence") or []):
                if (isinstance(item, dict) and item.get("source") == "agent_team_dependency"
                        and isinstance(item.get("task_key"), str) and isinstance(item.get("data"), dict)):
                    if item["task_key"] in source_dependencies:
                        conflicts.append(task_key)
                        check("summary_claim", f"/dependencies/{task_key}/evidence/{index}", item["data"],
                              task_key, f"/result/evidence/{index}/data", "conflict",
                              reason="汇总包含重复成员证据，不能覆盖较早输出")
                        continue
                    source_dependencies[item["task_key"]] = item["data"]
                    prefixes[item["task_key"]] = f"/result/evidence/{index}/data"
            if source_dependencies and _depth < 8:
                recomputed = summarize_dependencies(source_dependencies, _depth=_depth + 1)
                for prior in recomputed["claim_checks"]:
                    nested_checks[prior["path"]] = prior
                    source_key = prior.get("source_task")
                    if source_key in prefixes:
                        nested_sources[prior["path"]] = {
                            "source_task": task_key,
                            "source_path": prefixes[source_key] + prior["source_path"],
                        }
                result = {**recomputed["artifacts"][0]["data"], "status": recomputed["status"]}
                reconciled_fields = (
                    "unique_finding_count", "findings", "business_results", "coverage_summary",
                    "incomplete_tasks", "conflicting_tasks", "bounded_finding_tasks", "references",
                    "verification_status", "findings_data_available",
                )
                for name in (*reconciled_fields, "status"):
                    if _json(raw_result.get(name)) != _json(recomputed.get(name)):
                        conflicts.append(task_key)
                        check("summary_claim", f"/dependencies/{task_key}/{name}", raw_result.get(name),
                              task_key, f"/result/{name}", "conflict",
                              reason="上级汇总结论与其成员实际输出不一致")
                for item in raw_result.get("artifacts") or []:
                    if isinstance(item, dict) and item.get("type") == "agent_team_summary":
                        claimed = item.get("data")
                        actual = recomputed["artifacts"][0]["data"]
                        if not isinstance(claimed, dict) or any(
                            _json(claimed.get(name)) != _json(actual.get(name)) for name in reconciled_fields
                        ):
                            conflicts.append(task_key)
                            check("summary_claim", f"/dependencies/{task_key}/artifacts", claimed, task_key,
                                  "/result/artifacts", "conflict", reason="汇总产物与底层成员证据不一致")
                for reference in recomputed["artifacts"][0]["data"]["references"]:
                    references[(reference["type"], reference["task_id"])] = {
                        **reference, "source_task": task_key,
                    }
                if recomputed["status"] != "completed":
                    bounded_tasks.append(task_key)
            else:
                result = {}
                bounded_tasks.append(task_key)
                check("summary_claim", f"/dependencies/{task_key}", None, task_key, "/result/evidence",
                      "incomplete", reason="汇总缺少可追溯的成员输出或依赖嵌套超过上限")

        def source(path):
            return nested_sources.get(path, {"source_task": task_key, "source_path": path})

        coverage = (result.get("coverage_summary") if nested else dependency_coverage_summary(result))
        # 嵌套汇总仍保留每个底层任务的覆盖行，不能将其相互覆盖。
        coverage_rows = coverage if isinstance(coverage, list) else [coverage or {}]
        for row_index, coverage in enumerate(coverage_rows):
            row = {**coverage, "task_key": task_key}
            coverage_index = len(coverage_summary)
            coverage_summary.append(row)
            if _incomplete_coverage(coverage):
                bounded_tasks.append(task_key)
            values = (_coverage_values(result) if not nested else [
                (key, f"/coverage_summary/{row_index}/{key}", value)
                for key, value in coverage.items() if key != "task_key"
            ])
            for field in dict.fromkeys(field for field, _, _ in values):
                field_sources = [{**source(path), "value": value} for key, path, value in values if key == field]
                conflict = len({_json(item["value"]) for item in field_sources}) > 1
                if conflict:
                    conflicts.append(task_key)
                selected = next(item for item in field_sources if _json(item["value"]) == _json(coverage[field]))
                check("coverage", f"/coverage_summary/{coverage_index}/{field}", coverage[field],
                      selected["source_task"], selected["source_path"],
                      "conflict" if conflict else "incomplete" if not complete or _incomplete_coverage(coverage)
                      else "supported", sources=field_sources,
                      reason="成员覆盖数据相互矛盾，保留保守值" if conflict else "与成员覆盖字段逐项一致")
            if not values:
                check("coverage", f"/coverage_summary/{coverage_index}", None, task_key,
                      "/result", "unverified", reason="成员未提供覆盖数据，覆盖范围未知")
        projected = entry.get("coverage_summary")
        if not nested and isinstance(projected, dict):
            for field, value in projected.items():
                if field not in coverage or _json(value) != _json(coverage[field]):
                    conflicts.append(task_key)
                    check("coverage", f"/dependencies/{task_key}/coverage_summary/{field}", value,
                          task_key, f"/coverage_summary/{field}", "conflict",
                          reason="覆盖投影无法在成员实际输出中找到一致值")

        blocks = _blocks_with_paths(result) if not nested else [("/result", result)]
        for block_path, block in blocks:
            if block.get("findings_truncated") or any(
                isinstance(block.get(name), dict) and block[name].get("findings_truncated")
                for name in ("coverage", "compliance")
            ):
                bounded_tasks.append(task_key)
            for field in ("findings", "issues"):
                items = block.get(field)
                if not isinstance(items, list):
                    continue
                finding_data_available = finding_data_available or (
                    result.get("findings_data_available", False) if nested else True
                )
                for index, item in enumerate(items):
                    if not isinstance(item, dict) or not item.get("title"):
                        bounded_tasks.append(task_key)
                        continue
                    key = _finding_key(item, block.get("project_id"), task_key, len(findings))
                    if key not in findings:
                        findings[key] = {**item, "source_tasks": []}
                        if not findings[key].get("project_id") and type(block.get("project_id")) is int:
                            findings[key]["project_id"] = block["project_id"]
                        finding_sources[key] = []
                    if task_key not in findings[key]["source_tasks"]:
                        findings[key]["source_tasks"].append(task_key)
                    item_source = source(f"/findings/{index}" if nested else f"{block_path}/{field}/{index}")
                    prior = nested_checks.get(f"/findings/{index}") if nested else None
                    finding_sources[key].append({**item_source, "completed": complete and (
                        prior is None or prior["status"] == "supported"
                    )})
            if not nested and "finding_count_total" in block:
                items = block.get("findings", block.get("issues"))
                declared = block["finding_count_total"]
                matches = (isinstance(items, list) and type(declared) is int and declared == len(items))
                if not matches:
                    (bounded_tasks if block.get("findings_truncated") else conflicts).append(task_key)
                check("finding_count", f"/dependencies/{task_key}{block_path}/finding_count_total",
                      declared, task_key, f"{block_path}/finding_count_total",
                      "supported" if matches and complete else "incomplete" if block.get("findings_truncated")
                      or not complete else "conflict", reason="声明的问题数与成员返回的完整条目数对账")
        projection = entry.get("finding_summary")
        if not nested and isinstance(projection, dict):
            # 公开字段摘要可能因长度被裁剪；始终用完整成员输出聚合，不能让预览反客为主。
            has_raw_items = any(isinstance(block.get(field), list) for _, block in blocks
                                for field in ("findings", "issues"))
            if (projection.get("source_truncated") or not has_raw_items and (
                    projection.get("items") or projection.get("omitted_count"))):
                bounded_tasks.append(task_key)
                check("finding", f"/dependencies/{task_key}/finding_summary", projection, task_key,
                      "/finding_summary", "incomplete", reason="问题投影未被成员完整输出支持或明确截断")

        verified_review_id = entry.get("verified_review_task_id")
        if type(verified_review_id) is int and verified_review_id > 0 and complete:
            references[("review_task", verified_review_id)] = {
                "type": "review_task", "task_id": verified_review_id,
                "route": f"/reviews/{verified_review_id}", "source_task": task_key,
            }
        receipts = result.get("result_receipts") or [result.get("result_receipt")]
        for index, receipt in enumerate(receipts if isinstance(receipts, list) else []):
            if not isinstance(receipt, dict):
                continue
            output = receipt.get("output")
            output = output if isinstance(output, dict) else {}
            scope, state, reason = _receipt_verification(receipt)
            from app.services.agent_responses_service import redact_agent_model_context_value

            data = redact_agent_model_context_value(output.get("data"))
            capability = str(output.get("capability") or "")
            receipt_path = (f"/result/result_receipts/{index}" if result.get("result_receipts")
                            else "/result/result_receipt")
            key = (capability, receipt.get("request_id") or _json(data))
            candidate = {"source_task": task_key, "capability": capability, "data": data,
                         "verification_scope": scope, "completed": complete and state != "conflict"}
            origin = {"source_task": task_key, "source_path": receipt_path + "/output"}
            record = businesses.get(key)
            if record:
                if _json(record["value"]["data"]) != _json(data):
                    scope, state, reason = "readback_mismatch", "conflict", "同一业务回执的成员输出相互冲突"
                if record["state"] != "conflict" and state == "supported":
                    record.update(value=candidate, state=state, reason=reason, origin=origin)
                if state == "conflict":
                    record.update(state=state, reason=reason)
                    record["value"].update(verification_scope=scope, completed=False)
            else:
                record = businesses[key] = {"value": candidate, "state": state, "reason": reason,
                                            "origin": origin, "sources": [], "all_complete": True}
            record["sources"].append(origin)
            record["all_complete"] = record["all_complete"] and complete
            if state == "conflict":
                conflicts.append(task_key)
            if not record["all_complete"]:
                record["value"]["completed"] = False
                if record["state"] != "conflict":
                    record["state"] = "incomplete"
                    record["reason"] = "同一回执包含未完成或失败的成员输出"
        if nested:
            for index, business in enumerate(result.get("business_results") or []):
                key = ("nested", task_key, index)
                state = nested_checks[f"/business_results/{index}"]["status"]
                businesses[key] = {"value": {**business, "source_task": task_key,
                                             "completed": business["completed"] and complete},
                                   "state": state if complete or state == "conflict" else "incomplete",
                                   "reason": "从底层成员回执重新核对" if complete else "外层成员未完成",
                                   "origin": source(f"/business_results/{index}"),
                                   "sources": [source(f"/business_results/{index}")]}

    for index, (key, item) in enumerate(findings.items()):
        sources = finding_sources[key]
        check("finding", f"/findings/{index}", item,
              **{k: v for k, v in sources[0].items() if k != "completed"},
              status="supported" if all(part["completed"] for part in sources) else "incomplete",
              source_tasks=item["source_tasks"], sources=sources,
              reason="已与成员返回的问题逐条对账；不代表漏洞实测确认")
    business_results = []
    for index, record in enumerate(businesses.values()):
        value, state = record["value"], record["state"]
        business_results.append(value)
        path = f"/business_results/{index}"
        check("business_result", path, value, **record["origin"], status=state,
              scope=value["verification_scope"], reason=record["reason"], sources=record["sources"])
        data = value["data"]
        if isinstance(data, dict):
            for field, field_value in data.items():
                origin = record["origin"]
                check("business_result", pointer_child(f"{path}/data", field), field_value, origin["source_task"],
                      pointer_child(f"{origin['source_path']}/data", field),
                      state, value["verification_scope"], record["reason"])
    failed = sorted(set(failed))
    bounded_tasks = sorted(set(bounded_tasks))
    conflicts = sorted(set(conflicts))
    complete = bool(outcomes) and not failed and not bounded_tasks and not conflicts
    scope = "仅核对依赖节点返回的证据；问题数为已返回条目的精确去重，不代表全项目漏洞总数或实测确认数。"
    count_sources = [part for sources in finding_sources.values() for part in sources]
    check("finding_count", "/unique_finding_count", len(findings), "", "/evidence",
          "supported" if finding_data_available else "unverified", scope="returned_findings_only",
          reason="对有来源的问题条目精确去重" if finding_data_available else "未提供问题明细，不能推断无问题",
          sources=count_sources)
    verification_status = ("incomplete" if not outcomes or failed or bounded_tasks else "conflict" if conflicts
                           else "unverified" if any(item["status"] == "unverified" for item in checks)
                           else "supported")
    summary = {
        "verification": "dependency_evidence_reconciled", "verification_status": verification_status,
        "outcomes": outcomes, "incomplete_tasks": failed, "conflicting_tasks": conflicts,
        "findings_data_available": finding_data_available,
        "unique_finding_count": len(findings), "bounded_finding_tasks": bounded_tasks,
        "findings": list(findings.values()), "references": list(references.values()),
        "coverage_summary": coverage_summary, "business_results": business_results,
        "claim_checks": checks, "scope": scope,
    }
    note = (f"保留业务结果 {len(business_results)} 组；" if business_results
            else f"汇总保留成员返回的问题 {len(findings)} 条；" if finding_data_available
            else "未提供问题明细，不能推断无问题；")
    message = (f"汇总已完成：已核对 {len(outcomes)} 个子任务的结构化终态；{note}"
               "逐项结论的来源及核验范围见 claim_checks，成员原文未逐项核验。" if complete else
               "团队结果未完整覆盖；" + ("无成员输出；" if not outcomes else "") +
               f"待处理节点：{', '.join(failed) or '无'}；明细或覆盖不完整的节点：{', '.join(bounded_tasks) or '无'}；"
               f"证据冲突节点：{', '.join(conflicts) or '无'}。已保留可用证据。")
    return {
        "status": "completed" if complete else "failed", "verification_status": verification_status,
        "outcomes": outcomes, "findings_data_available": finding_data_available,
        "unique_finding_count": len(findings), "incomplete_tasks": failed, "conflicting_tasks": conflicts,
        "business_results": business_results,
        "findings": [{**item, "source_task_keys": ",".join(item["source_tasks"])}
                     for item in list(findings.values())[:20]],
        "findings_preview_truncated": len(findings) > 20,
        "references": list(references.values())[:20], "coverage_summary": coverage_summary[:20],
        "coverage_summary_truncated": len(coverage_summary) > 20,
        "bounded_finding_tasks": bounded_tasks, "claim_checks": checks, "scope": scope, "summary": message,
        "evidence": evidence, "artifacts": [{"type": "agent_team_summary", "data": summary}],
        "errors": [] if complete else [{
            "code": ("incomplete_dependencies" if failed else "dependency_evidence_conflict" if conflicts
                     else "finding_coverage_incomplete"),
            "task_keys": failed, "bounded_task_keys": bounded_tasks, "conflicting_task_keys": conflicts,
        }],
        "next_action": None if complete else {"review_incomplete_tasks": failed,
            "review_bounded_tasks": bounded_tasks, "review_conflicting_tasks": conflicts},
        "retryable": False,
    }
