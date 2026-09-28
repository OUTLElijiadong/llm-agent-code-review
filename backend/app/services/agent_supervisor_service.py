"""小菱监督子 Agent 的确定性风险分类与团队计划复核。

监督器只提供可审计的风险复核，不授予权限，也不能覆盖拒绝策略。
未识别的动作按高风险升级；高风险需要当前用户确认。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

LOW = "low"
MEDIUM = "medium"
HIGH = "high"
CRITICAL = "critical"

ALLOW = "allow"
ESCALATE = "escalate"
DENY = "deny"

_RISK_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2, CRITICAL: 3}
_HIGH_RISK_MARKERS = (
    "permission", "role", "delete", "drop", "truncate", "production_config", "prod_config",
    "production.deploy", "deploy.production", "release.publish", "publish", "agent.toggle",
    "remote_target", "external_target", "pentest", "penetration", "credential", "secret",
    "private_key", "access_token", "account_role", "user_role", "project_members.add",
    "project_members.update_role", "members.add", "members.update", "jobs.update", "jobs.run",
    "jobs.create", "scheduler", "shell.write", "shell.exec.write",
)
_READ_SUFFIXES = (".read", ".list", ".get", ".search", ".status", ".inspect", ".describe")
_KNOWN_MEDIUM_MARKERS = (
    "sandbox", "isolated_test", "run_project_tests", "run_full_project_validation", "review.start",
    "security.scan", "audit.project",
)
_READONLY_TEAM_AGENTS = frozenset({
    "language_detector", "project_analyzer", "code_reviewer", "project_manager", "code_file_manager",
    "review_orchestrator", "dashboard", "reporter", "rule_manager", "ai_prompt", "security_sentinel",
    "monitor", "supervisor", "test_verifier", "sandbox_deployer", "custom", "temporary",
})
_LOW_RESPONSE_TOOLS = frozenset({
    "admin_governance_overview", "admin_list_agent_release_approvals", "admin_list_agents",
    "admin_list_approvals", "admin_list_roles", "admin_list_users", "admin_system_status",
    "analyze_project", "dashboard_summary", "detect_language", "get_agent_team", "get_pentest_status",
    "get_project_detail", "list_agent_skills", "list_agents", "list_code_files", "list_projects",
    "list_reports", "list_review_issues", "list_review_tasks", "list_rules", "recall_knowledge",
    "review_code", "search_published_agents", "admin_describe_capabilities", "user_describe_capabilities",
    "get_remote_project_import", "download_project_source", "download_report", "download_code_file",
    "get_roundtable_discussion", "admin_release_health",
})
_MEDIUM_RESPONSE_TOOLS = frozenset({
    "cancel_agent_team", "retry_agent_team", "create_project", "update_project", "start_review",
    "generate_ai_prompt_for_issue", "generate_ai_prompt_for_task", "generate_ai_prompt_for_project",
    "audit_security_for_file", "audit_security_for_task", "audit_security_for_project",
    "run_full_project_validation", "deploy_project_sandbox", "close_sandbox", "extend_sandbox",
    "save_knowledge_note", "start_roundtable_discussion", "control_roundtable_discussion",
    "invoke_published_agent", "create_pentest_engagement", "send_message", "create_agent_team",
    "run_project_tests",
})
_HIGH_RESPONSE_TOOLS = frozenset({
    "delete_project", "start_pentest_engagement", "change_own_password", "trigger_evolution",
    "admin_set_user_role", "admin_delete_user", "admin_delete_users", "admin_toggle_agent",
    "admin_decide_agent_release", "admin_execute_operation", "admin_execute_capability",
    "user_execute_capability", "queue_remote_project_import",
})


@dataclass(frozen=True)
class SupervisionReview:
    decision: str
    risk_level: str
    reason: str
    needs_confirmation: bool
    classification: str


def _text(*values: Any) -> str:
    return " ".join(str(value or "") for value in values).casefold()


def maximum_risk(*levels: str) -> str:
    """返回多个风险等级中的最高值；未知等级按 critical 处理。"""
    valid = [level if level in _RISK_ORDER else CRITICAL for level in levels if level]
    return max(valid or [LOW], key=lambda level: _RISK_ORDER[level])


def review_action(
    action: str,
    resource: str = "",
    *,
    declared_risk: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> SupervisionReview:
    """以服务端动作和注册能力做风险复核，不信任模型给出的风险级别。"""
    context = context or {}
    text = _text(action, resource, context.get("target"), context.get("command"))
    if any(marker in text for marker in _HIGH_RISK_MARKERS):
        risk = maximum_risk(declared_risk or LOW, HIGH)
        return SupervisionReview(ESCALATE, risk, "命中高风险操作边界，等待当前账号确认", True, "classified_high")

    if declared_risk in {LOW, MEDIUM, HIGH, CRITICAL}:
        risk = declared_risk
        if risk in {HIGH, CRITICAL}:
            return SupervisionReview(
                ESCALATE, risk, "注册能力被标记为高风险，等待当前账号确认", True, "server_registry",
            )
        return SupervisionReview(ALLOW, risk, "动作由服务端能力注册表分类", False, "server_registry")

    action_text = (action or "").casefold()
    if action_text.endswith(_READ_SUFFIXES) or action_text.startswith(("knowledge.read", "profile.read")):
        return SupervisionReview(ALLOW, LOW, "已识别为只读动作", False, "known_read")
    if any(marker in text for marker in _KNOWN_MEDIUM_MARKERS):
        return SupervisionReview(ALLOW, MEDIUM, "已识别为受限的隔离测试或审查动作", False, "known_isolated_operation")

    return SupervisionReview(ESCALATE, HIGH, "动作未被监督规则分类，停止自动执行并升级确认", True, "unclassified")


def review_agent_team_plan(payload: Mapping[str, Any]) -> dict[str, Any]:
    """审阅小菱拟创建的团队工作图，返回稳定摘要及高风险任务列表。"""
    members = payload.get("members") if isinstance(payload.get("members"), list) else []
    tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
    member_by_key = {
        str(item.get("member_key") or ""): item
        for item in members
        if isinstance(item, Mapping)
    }
    reviews: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            reviews.append({"task_key": "unknown", "risk_level": HIGH, "decision": ESCALATE,
                            "reason": "团队任务结构无法识别"})
            continue
        member = member_by_key.get(str(task.get("member_key") or ""), {})
        address = str(member.get("address") or "")
        code = _team_agent_code(address, member)
        task_input = task.get("input") if isinstance(task.get("input"), Mapping) else {}
        remote_target = any(
            str(task_input.get(key) or "").strip()
            for key in ("remote_target_url", "target_url", "external_target_url")
        )
        operation = str(task_input.get("operation") or "").casefold()
        review = _review_team_task(
            code, operation, title=str(task.get("title") or ""),
            instructions=str(task.get("instructions") or ""), task_input=task_input,
        )
        # 远程黑盒、正式渗透、管理/运维和职责未知成员均需确认。
        if remote_target or code in {"operations", "incident_responder"} or code not in _READONLY_TEAM_AGENTS:
            review = SupervisionReview(
                ESCALATE, maximum_risk(review.risk_level, HIGH),
                "团队任务包含外部目标、管理职责或无法确认的成员能力，等待当前账号确认",
                True, "team_scope_review",
            )
        reviews.append({
            "task_key": str(task.get("task_key") or ""),
            "member_key": str(task.get("member_key") or ""),
            "agent": code,
            "decision": review.decision,
            "risk_level": review.risk_level,
            "reason": review.reason,
            "classification": review.classification,
            "fingerprint": task_fingerprint(task, address),
        })

    risk = maximum_risk(*(item["risk_level"] for item in reviews))
    needs_confirmation = any(item["decision"] != ALLOW for item in reviews)
    canonical_payload = {
        "title": str(payload.get("title") or ""),
        "objective": str(payload.get("objective") or ""),
        "members": [
            {
                "member_key": item.get("member_key"),
                "address": item.get("address"),
                "role": item.get("role", "worker"),
                "definition": item.get("definition"),
            }
            for item in members if isinstance(item, Mapping)
        ],
        "tasks": [
            {
                "task_key": item.get("task_key"),
                "member_key": item.get("member_key"),
                "title": item.get("title"),
                "instructions": item.get("instructions"),
                "depends_on": item.get("depends_on", []),
                "input": {
                    str(key): value
                    for key, value in (item.get("input", {}) or {}).items()
                    if key != "operation" and key != "remote_target_authorized" and not str(key).startswith("_")
                } if isinstance(item.get("input", {}), Mapping) else {},
            }
            for item in tasks if isinstance(item, Mapping)
        ],
    }
    canonical = json.dumps(canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "decision": ESCALATE if needs_confirmation else ALLOW,
        "risk_level": risk,
        "needs_confirmation": needs_confirmation,
        "plan_sha256": digest,
        "tasks": reviews,
    }


def task_fingerprint(task: Mapping[str, Any], address: str) -> str:
    """绑定一项任务的目标、内容和授权相关输入，忽略服务端补齐的 operation。"""
    raw_input = task.get("input") if isinstance(task.get("input"), Mapping) else {}
    payload = {
        "task_key": task.get("task_key"),
        "member_key": task.get("member_key"),
        "address": address,
        "title": task.get("title"),
        "instructions": task.get("instructions"),
        "input": {
            str(key): value for key, value in raw_input.items()
            if key != "operation" and key != "remote_target_authorized" and not str(key).startswith("_")
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def review_team_task(
    *, address: str, task_key: str, title: str, instructions: str, task_input: Mapping[str, Any],
) -> SupervisionReview:
    code = _team_agent_code(address, {})
    operation = str(task_input.get("operation") or "").casefold()
    return _review_team_task(code, operation, title=title, instructions=instructions, task_input=task_input)


def _team_agent_code(address: str, member: Mapping[str, Any]) -> str:
    """保留动态 Agent 类型前缀；temporary:access 的 access 是实例键而非能力名。"""
    prefix, separator, suffix = str(address or "").partition(":")
    if separator and prefix in {"temporary", "custom"}:
        return prefix
    if separator and prefix == "agent":
        return suffix
    kind = str(member.get("kind") or "")
    return kind if kind in {"temporary", "custom"} else (suffix if separator else prefix)


def _review_team_task(
    code: str, operation: str, *, title: str, instructions: str, task_input: Mapping[str, Any],
) -> SupervisionReview:
    has_external_target = any(
        str(task_input.get(key) or "").strip()
        for key in ("remote_target_url", "target_url", "external_target_url")
    )
    if has_external_target:
        return SupervisionReview(ESCALATE, HIGH, "任务包含外部目标，等待当前账号确认", True, "external_target")
    if code in {"operations", "incident_responder"}:
        return SupervisionReview(ESCALATE, HIGH, "运维或事件处置任务需当前账号确认", True, "privileged_agent")
    if code in {"custom", "temporary"}:
        return SupervisionReview(ALLOW, MEDIUM, "该任务成员只能读取当前团队提供的有界源码或文本并返回分析", False,
                                 "bounded_readonly_agent")
    if code == "test_verifier":
        if operation == "inspect_existing_results":
            return SupervisionReview(ALLOW, LOW, "仅读取已存在的测试结果", False, "known_read")
        if operation in {"", "run_project_tests", "run_full_project_validation"}:
            return SupervisionReview(ALLOW, MEDIUM, "测试限定在当前项目隔离沙箱", False, "isolated_test")
        return SupervisionReview(ESCALATE, HIGH, "测试操作不在已登记清单中", True, "unclassified_operation")
    if code == "sandbox_deployer":
        if operation in {"", "deploy", "close", "extend", "deploy_project_sandbox", "close_sandbox"}:
            return SupervisionReview(ALLOW, MEDIUM, "操作限定在项目隔离沙箱", False, "isolated_sandbox")
        return SupervisionReview(ESCALATE, HIGH, "沙箱部署 Agent 请求了未登记操作", True, "unclassified_operation")
    if code in {"review_orchestrator", "security_sentinel"}:
        if operation in {"", "list", "get", "issues", "run_review", "audit_security_for_project"}:
            return SupervisionReview(ALLOW, MEDIUM, "项目审查/审计限定为当前账号可见源码的只读分析",
                                     False, "project_readonly_review")
        return SupervisionReview(ESCALATE, HIGH, "审查 Agent 请求了未登记操作", True, "unclassified_operation")
    if code in _READONLY_TEAM_AGENTS:
        return SupervisionReview(ALLOW, LOW, "已登记的只读分析 Agent", False, "known_read")
    return SupervisionReview(ESCALATE, HIGH, "成员能力未被监督器识别，等待当前账号确认", True, "unclassified_agent")


def review_task_result(result: Mapping[str, Any]) -> SupervisionReview:
    """复核子 Agent 的结构化结果；不把自述成功当作证据。"""
    status = str(result.get("status") or "").casefold()
    evidence = result.get("evidence")
    errors = result.get("errors")
    if status in {"completed", "success"} and not isinstance(evidence, list):
        return SupervisionReview(
            ESCALATE, HIGH, "子 Agent 声称完成但未提供结构化证据，需复核", True, "missing_evidence",
        )
    if status in {"failed", "blocked", "needs_clarification", "approval_required"}:
        return SupervisionReview(
            ALLOW, LOW, "监督器确认子 Agent 未报告成功；保留原始失败状态", False, "non_success_result",
        )
    if status not in {"completed", "success"}:
        return SupervisionReview(ESCALATE, HIGH, "子 Agent 返回未知状态，停止合并为成功", True, "unknown_result_status")
    if errors and not isinstance(errors, list):
        return SupervisionReview(ESCALATE, HIGH, "子 Agent 错误字段结构无效，需复核", True, "invalid_evidence_shape")
    return SupervisionReview(ALLOW, MEDIUM, "结构化结果通过基础监督检查；语义准确性仍由 verifier 独立复核",
                             False, "structured_result")


def declared_capability_risk(risk: str) -> str:
    """把受控能力注册表的 READ/WRITE/CRITICAL 映射成统一风险等级。"""
    return {"read": LOW, "write": MEDIUM, "critical": CRITICAL}.get(str(risk).casefold(), HIGH)


def review_response_tool(tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """在 Responses 工具统一入口复核主 Agent 发起的每项工具调用。"""
    name = str(tool_name or "")
    action = f"responses.{name}"
    resource = "current_account"
    extra: dict[str, Any] = {}
    declared_risk: str | None = None

    if name == "create_agent_team":
        team_review = review_agent_team_plan(arguments)
        review = SupervisionReview(
            team_review["decision"], team_review["risk_level"],
            "团队计划通过监督复核" if not team_review["needs_confirmation"]
            else "团队计划包含高风险或未分类任务，等待当前账号确认",
            bool(team_review["needs_confirmation"]), "team_plan",
        )
        extra = team_review
    elif name in {"admin_execute_capability", "user_execute_capability"}:
        capability = str(arguments.get("capability") or "")
        if name == "admin_execute_capability":
            from app.services.admin_capability_registry import CAPABILITY_BY_CODE

            spec = CAPABILITY_BY_CODE.get(capability)
        else:
            from app.services.user_capability_registry import CAPABILITY_BY_CODE

            spec = CAPABILITY_BY_CODE.get(capability)
        declared_risk = declared_capability_risk(spec.risk) if spec is not None else None
        resource = str(spec.page if spec is not None else "unregistered_capability")
        action = f"responses.{name}.{capability or 'missing'}"
        review = review_action(action, resource, declared_risk=declared_risk)
    elif name == "admin_execute_operation":
        operation = str(arguments.get("action") or "")
        from app.services import ops_service

        declared_risk = LOW if operation in ops_service.READ_ONLY_ACTIONS else HIGH
        action = f"operations.{operation or 'unknown'}"
        resource = "production"
        review = review_action(action, resource, declared_risk=declared_risk)
    elif name == "run_project_tests":
        remote = str(arguments.get("remote_target_url") or "").strip()
        if remote:
            review = SupervisionReview(ESCALATE, HIGH, "测试包含外部目标，等待当前账号确认", True, "external_target")
            extra = {"remote_target_url": remote, "mode": arguments.get("test_mode")}
        else:
            review = SupervisionReview(ALLOW, MEDIUM, "测试任务限定在当前项目隔离沙箱", False, "isolated_test")
    elif name == "send_message":
        target = str(arguments.get("send_to") or "")
        message = arguments.get("message") if isinstance(arguments.get("message"), Mapping) else {}
        raw_payload = message.get("payload") if isinstance(message.get("payload"), Mapping) else message
        review = review_team_task(
            address=target,
            task_key="message",
            title=str(message.get("subject") or "Agent 消息"),
            instructions=str(raw_payload.get("instructions") or raw_payload.get("task") or ""),
            task_input=raw_payload,
        )
        action = f"agent_message.{target or 'unknown'}"
        resource = target or "unknown_agent"
    elif name in _HIGH_RESPONSE_TOOLS:
        review = SupervisionReview(ESCALATE, HIGH, "该工具涉及高风险写入或权限边界，等待当前账号确认", True,
                                  "registered_high_risk_tool")
    elif name in _MEDIUM_RESPONSE_TOOLS:
        review = SupervisionReview(ALLOW, MEDIUM, "已登记的可逆或隔离中风险操作", False, "registered_medium_tool")
    elif name in _LOW_RESPONSE_TOOLS:
        review = SupervisionReview(ALLOW, LOW, "已登记的只读操作", False, "registered_read_tool")
    elif name.startswith("mcp_"):
        review = SupervisionReview(ESCALATE, HIGH, "外部 MCP 工具必须由当前账号确认", True, "external_mcp")
    else:
        review = SupervisionReview(ESCALATE, HIGH, "工具未登记到监督清单，停止自动执行并升级确认", True,
                                  "unregistered_tool")

    return {
        "tool_name": name,
        "action": action,
        "resource": resource,
        **public_review(review),
        "details": extra,
    }


def review_mcp_tool(
    *,
    tool_name: str,
    managed_kind: str,
    permission: str,
    requires_approval: bool,
    declared_risk: str,
) -> dict[str, Any]:
    """只按精确受管能力白名单自动通过 MCP；其余 MCP 一律升级确认。"""

    name = str(tool_name or "")
    kind = str(managed_kind or "")
    if not kind:
        review = SupervisionReview(
            ESCALATE, HIGH, "外部 MCP 能力必须由当前账号确认", True, "external_mcp",
        )
    elif requires_approval or permission != "allow":
        review = SupervisionReview(
            ESCALATE, maximum_risk(declared_risk, HIGH),
            "MCP 注册策略要求人工审批或权限级别不允许自动执行", True, "managed_mcp_approval_required",
        )
    else:
        # 工具本身的 risk_level 只能提高精确能力的基准风险，不能把写操作降为只读。
        exact_risks = {
            ("prism-code", "list_project_source"): LOW,
            ("prism-code", "download_project_source"): LOW,
            ("prism-sandbox", "create_test"): MEDIUM,
            ("prism-sandbox", "create_deployment"): MEDIUM,
            ("prism-sandbox", "close"): MEDIUM,
            ("prism-sandbox", "extend"): MEDIUM,
        }
        baseline = exact_risks.get((kind, name))
        if baseline is None:
            review = SupervisionReview(
                ESCALATE, HIGH, "受管 MCP 能力未在监督白名单中，等待当前账号确认", True,
                "unclassified_managed_mcp",
            )
        else:
            risk = maximum_risk(baseline, declared_risk)
            review = SupervisionReview(
                ESCALATE if risk in {HIGH, CRITICAL} else ALLOW,
                risk,
                "已登记的项目只读能力" if risk == LOW else "已登记的当前项目隔离沙箱能力",
                risk in {HIGH, CRITICAL},
                "registered_managed_mcp",
            )
    return {
        "tool_name": f"mcp:{kind or 'external'}:{name or 'unknown'}",
        "action": f"mcp.{kind or 'external'}.{name or 'unknown'}",
        "resource": "current_account",
        **public_review(review),
        "details": {},
    }


def public_review(review: SupervisionReview) -> dict[str, Any]:
    return asdict(review)
