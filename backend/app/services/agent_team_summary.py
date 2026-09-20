"""按团队依赖证据汇总；不把运行完成等同于漏洞已复现。"""

from __future__ import annotations

from typing import Any


def _result_blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [result]
    for collection in ("evidence", "artifacts"):
        for item in result.get(collection) or []:
            if isinstance(item, dict):
                blocks.append(item["data"] if isinstance(item.get("data"), dict) else item)
    return blocks


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


def summarize_dependencies(dependencies: dict[str, Any]) -> dict[str, Any]:
    outcomes = []
    evidence = []
    findings: dict[tuple, dict[str, Any]] = {}
    references: dict[tuple, dict[str, Any]] = {}
    failed = []
    bounded_tasks = []
    for task_key, entry in sorted(dependencies.items()):
        entry = entry if isinstance(entry, dict) else {}
        result = entry.get("result")
        result = result if isinstance(result, dict) else {}
        status = str(entry.get("status") or "unknown")
        result_status = str(result.get("status") or "unknown")
        complete = status == "completed" and result_status == "completed"
        if not complete:
            failed.append(task_key)
        outcomes.append({
            "task_key": task_key, "status": status, "result_status": result_status,
            "summary": str(result.get("summary") or "缺少结果说明"),
        })
        evidence.append({"source": "agent_team_dependency", "task_key": task_key, "data": entry})
        blocks = _result_blocks(result)
        finding_summary = entry.get("finding_summary")
        if isinstance(finding_summary, dict):
            # 队列提供来自数据库的最小字段摘要；不再读取已被事件脱敏截断的嵌套条目。
            blocks = [{key: value for key, value in block.items() if key not in {"findings", "issues"}}
                      for block in blocks]
            blocks.append({"findings": finding_summary.get("items") or []})
            if finding_summary.get("omitted_count") or finding_summary.get("source_truncated"):
                bounded_tasks.append(task_key)
        for data in blocks:
            project_id = data.get("project_id")
            task_id = data.get("task_id")
            if isinstance(task_id, int) and task_id > 0:
                references[("review_task", task_id)] = {
                    "type": "review_task", "task_id": task_id,
                    "route": f"/reviews/{task_id}", "source_task": task_key,
                }
            for item in data.get("findings") or data.get("issues") or []:
                if not isinstance(item, dict) or not item.get("title"):
                    continue
                # 没有可验证项目标识时只在本节点去重，避免合并不同项目同路径问题。
                scope = item.get("project_id") or project_id or f"task:{task_key}"
                key = (
                    str(scope), str(item.get("file_id") or item.get("file_path") or item.get("file_name")
                                    or f"unlocated:{task_key}:{len(findings)}"),
                    str(item.get("line_number") or item.get("line") or ""),
                    str(item.get("title")), str(item.get("severity") or ""),
                )
                if key not in findings:
                    findings[key] = {**item, "source_tasks": []}
                if task_key not in findings[key]["source_tasks"]:
                    findings[key]["source_tasks"].append(task_key)
    summary = {
        "verification": "dependency_evidence_reconciled",
        "outcomes": outcomes,
        "incomplete_tasks": failed,
        "unique_finding_count": len(findings),
        "bounded_finding_tasks": bounded_tasks,
        "findings": list(findings.values()),
        "references": list(references.values()),
        "scope": "仅核对依赖节点返回的证据；问题数为已返回条目的精确去重，不代表全项目漏洞总数或实测确认数。",
    }
    complete = bool(outcomes) and not failed
    bounded_note = (
        f" {len(bounded_tasks)} 个节点的问题明细有截断，请查看原任务/报告中的完整结果。" if bounded_tasks else ""
    )
    return {
        "status": "completed" if complete else "failed",
        # 公开团队结果会再次脱敏，关键结论保持浅层；详细聚合仍保留在原始产物。
        "unique_finding_count": len(findings),
        "findings": [{**item, "source_task_keys": ",".join(item["source_tasks"])}
                     for item in list(findings.values())[:20]],
        "findings_preview_truncated": len(findings) > 20,
        "references": list(references.values())[:20],
        "bounded_finding_tasks": bounded_tasks,
        "scope": summary["scope"],
        "summary": (
            f"已核对 {len(outcomes)} 个子任务的终态与证据，汇总保留问题 {len(findings)} 条；"
            f"实测范围见各项证据。{bounded_note}"
            if complete else f"团队结果未全部完成；待处理节点：{', '.join(failed) or '缺少依赖结果'}。已保留可用证据。"
        ),
        "evidence": evidence,
        "artifacts": [{"type": "agent_team_summary", "data": summary}],
        "errors": [] if complete else [{"code": "incomplete_dependencies", "task_keys": failed}],
        "next_action": None if complete else {"review_incomplete_tasks": failed},
        "retryable": False,
    }
