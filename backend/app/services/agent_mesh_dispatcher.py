"""Agent Mesh 服务端消费者与显式目标 Handler。"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult
from app.agents.contracts import CONTRACTS
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.agent_governance import AgentAlert, AgentMetricSnapshot
from app.models.agent_mesh import AgentMeshMessage
from app.models.custom_agent import CustomAgent
from app.models.user import User

_scheduler = None

_READONLY_RUNTIME_CODES = frozenset(
    {
        "language_detector",
        "project_analyzer",
        "code_reviewer",
        "project_manager",
        "code_file_manager",
        "review_orchestrator",
        "dashboard",
        "reporter",
        "rule_manager",
        "ai_prompt",
        "security_sentinel",
    }
)
_APPROVAL_REQUIRED_CODES = frozenset(
    {
        "test_verifier",
        "sandbox_deployer",
        "operations",
        "incident_responder",
        "scheduler",
        "approval",
        "policy",
        "evolution",
        "alert",
    }
)
_PROTECTED_CODES = frozenset({"chat_assistant", "manager"})
_SANDBOX_FAILURE_STATES = frozenset({"failed", "blocked", "stopped", "expired"})
_SANDBOX_POLL_SECONDS = 2.0


def dispatch_state(address: str) -> str:
    """返回目标的真实服务端消费能力，不把契约画像冒充执行实现。"""
    if address.startswith("custom:"):
        return "executable"
    if not address.startswith("agent:"):
        return "unsupported"
    code = address.split(":", 1)[1]
    if code in _PROTECTED_CODES:
        return "session_only"
    if code == "monitor" or code in _READONLY_RUNTIME_CODES:
        return "executable"
    if code in _APPROVAL_REQUIRED_CODES:
        return "approval_required"
    return "needs_configuration"


def _result(
    status: str,
    summary: str,
    *,
    evidence: Optional[list[Any]] = None,
    artifacts: Optional[list[Any]] = None,
    errors: Optional[list[Any]] = None,
    next_action: Any = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary,
        "evidence": evidence or [],
        "artifacts": artifacts or [],
        "errors": errors or [],
        "next_action": next_action,
    }


def _missing(*fields: str) -> dict[str, Any]:
    names = [field for field in fields if field]
    result = _result(
        "needs_clarification",
        f"缺少执行所需字段：{', '.join(names)}",
        errors=[{"code": "missing_fields", "fields": names}],
        next_action={"provide_fields": names},
    )
    result["retryable"] = False
    return result


def _payload(message: dict[str, Any]) -> dict[str, Any]:
    value = message.get("payload")
    return value if isinstance(value, dict) else {}


def _readonly_team_task(data: dict[str, Any]) -> bool:
    if str(data.get("execution_mode") or "").casefold() == "read_only":
        return True
    instructions = str(data.get("instructions") or "")
    return any(
        marker in instructions
        for marker in ("只读", "只核对", "仅核对", "严禁运行新测试", "不得触发任何测试")
    )


def _ctx(message: dict[str, Any]) -> AgentContext:
    context = message.get("context") if isinstance(message.get("context"), dict) else {}
    data = _payload(message)
    strategy = data.get("_execution_strategy")
    strategy = strategy if isinstance(strategy, dict) else {}
    return AgentContext(
        user_id=int(message["user_id"]),
        task_id=context.get("task_id"),
        project_id=context.get("project_id"),
        file_id=context.get("file_id"),
        extra={
            "trace_id": message.get("trace_id", ""),
            "mesh_message_id": message.get("message_id", ""),
            "execution_strategy": strategy,
            "strategy_instruction": str(strategy.get("instruction") or data.get("instructions") or ""),
            "agent_team": {
                "team_id": context.get("team_id"),
                "task_id": context.get("agent_team_task_id"),
                "lease_token": context.get("lease_token"),
                "attempt": context.get("attempt"),
            },
        },
    )


def _as_mesh_result(result: AgentResult, *, action: str) -> dict[str, Any]:
    if result.success:
        return _result(
            "completed",
            f"{action}已完成",
            evidence=[{"source": "request_scoped_agent", "data": result.data}],
            next_action=None,
        )
    if result.failure_kind:
        raise RuntimeError(result.error or f"{action}调用失败")
    return _result(
        "blocked",
        result.error or f"{action}未完成",
        errors=[{"code": "agent_rejected", "message": result.error or "执行未完成"}],
    )


def _sandbox_failure_result(
    action: str,
    state: dict[str, Any],
    *,
    status: str,
    message: str,
    code: str,
) -> dict[str, Any]:
    result = _result(
        "failed",
        f"{action}终态为 {status}：{message}",
        evidence=[{"source": "sandbox_environment", "data": state}],
        artifacts=[{"type": "sandbox_environment", "data": state}],
        errors=[{"code": code, "status": status, "message": message}],
        next_action={"retry": "fresh_sandbox", "change_worker": True, "reduce_scope": True},
    )
    raw_agent_tests = state.get("result")
    raw_agent_tests = raw_agent_tests if isinstance(raw_agent_tests, dict) else {}
    raw_agent_tests = raw_agent_tests.get("agent_tests")
    raw_agent_tests = raw_agent_tests if isinstance(raw_agent_tests, dict) else {}
    raw_files = raw_agent_tests.get("files")
    raw_files = raw_files if isinstance(raw_files, dict) else {}
    failed_files = {
        str(file_name)
        for file_name, file_status in raw_files.items()
        if str(file_status or "").strip().lower() == "fail"
    }
    raw_file_results = raw_agent_tests.get("file_results")
    raw_file_results = raw_file_results if isinstance(raw_file_results, dict) else {}
    protocol_version = raw_agent_tests.get("protocol_version")
    protocol_trusted = type(protocol_version) is int and protocol_version == 2
    file_kinds: dict[str, str] = {}
    for file_name in failed_files if protocol_trusted else set():
        item = raw_file_results.get(file_name)
        if not isinstance(item, dict):
            continue
        kind = str(item.get("failure_kind") or "").strip().lower()
        phase = str(item.get("phase") or "").strip().lower()
        if kind and phase in {"compile", "execute", "protocol"}:
            file_kinds[file_name] = kind

    compile_files = sorted(name for name, kind in file_kinds.items() if kind == "compile_error")
    execution_files = sorted(
        name
        for name, kind in file_kinds.items()
        if kind in {"execution_failure", "assertion_failure", "runtime_error"}
    )
    infrastructure_files = sorted(
        name for name, kind in file_kinds.items() if kind in {"infrastructure_error", "protocol_error"}
    )
    unknown_files = sorted(failed_files - set(file_kinds))
    protocol_invalid = protocol_trusted and bool(raw_agent_tests.get("missing") or raw_agent_tests.get("unexpected"))

    if execution_files:
        # 真实执行失败优先于同批其他失败，避免重生成测试掩盖已经命中的业务缺陷。
        result["strategy_change"] = "保留动态测试执行失败证据，修复业务源码后由验证 Agent 重新审查"
        result["retryable"] = False
        result["next_action"] = {"fix_business_source": True, "requires_review": True}
        if compile_files:
            result["next_action"]["invalid_agent_test_files"] = compile_files
        if infrastructure_files or protocol_invalid:
            result["next_action"]["validate_runner_protocol"] = True
    elif infrastructure_files or protocol_invalid:
        result["strategy_change"] = "切换全新沙箱并校验 runner 协议完整性后重新执行原动态测试"
        result["retry_after_seconds"] = 5
        result["next_action"] = {
            "retry": "fresh_sandbox",
            "change_worker": True,
            "validate_runner_protocol": True,
        }
        result["retryable"] = True
    elif compile_files and not unknown_files:
        result["strategy_change"] = "重新生成并先编译复核失败的动态测试，业务源码保持不变"
        result["retry_after_seconds"] = 5
        result["next_action"] = {
            "retry": "fresh_sandbox",
            "regenerate_agent_tests": True,
            "preserve_business_source": True,
            "invalid_agent_test_files": compile_files,
        }
        result["retryable"] = True
    elif int(raw_agent_tests.get("failed") or 0) > 0:
        # v1/损坏协议无法可靠区分测试自身错误和真实业务失败，禁止猜测后自动弱化断言。
        result["strategy_change"] = "动态测试缺少可信失败类型，保留证据并转交人工复核"
        result["retryable"] = False
        result["next_action"] = {
            "requires_review": True,
            "reason": "agent_test_failure_kind_unknown",
        }
    else:
        result["strategy_change"] = "改用全新沙箱并释放固定 worker，缩小输入范围后重新执行"
        result["retryable"] = True
    return result


def _sandbox_state_message(state: dict[str, Any]) -> str:
    if str(state.get("error") or "").strip():
        return str(state["error"]).strip()
    raw_result = state.get("result")
    raw_result = raw_result if isinstance(raw_result, dict) else {}
    conclusion = raw_result.get("conclusion")
    conclusion = conclusion if isinstance(conclusion, dict) else {}
    summary = str(conclusion.get("summary") or raw_result.get("summary") or "沙箱执行未成功").strip()
    agent_tests = conclusion.get("agent_tests") or raw_result.get("agent_tests")
    agent_tests = agent_tests if isinstance(agent_tests, dict) else {}
    details = agent_tests.get("details")
    details = details if isinstance(details, dict) else {}
    if not details:
        return summary
    snippets = [f"{name}: {str(output).strip()[:240]}" for name, output in list(details.items())[:3]]
    return f"{summary}；动态用例失败证据：{' | '.join(snippets)}"


def _team_task_is_active(
    db: Session,
    team_id: Optional[int],
    task_id: Optional[int],
    lease_token: str,
) -> bool:
    if not team_id or not task_id:
        return True
    if not lease_token:
        return False
    from app.models.agent_team import AgentTeam, AgentTeamTask

    team = db.get(AgentTeam, int(team_id))
    task = db.get(AgentTeamTask, int(task_id))
    return bool(
        team is not None
        and task is not None
        and str(team.status) not in {"completed", "failed", "cancelled", "expired"}
        and str(task.status) == "running"
        and str(task.lease_token or "") == lease_token
    )


def _stop_inactive_team_sandbox(
    db: Session,
    user: User,
    public_id: str,
    state: dict[str, Any],
    *,
    action: str,
) -> dict[str, Any]:
    from app.services import sandbox_service

    stop_error = ""
    try:
        stopped_state = sandbox_service.stop_environment(db, user, public_id)
    except Exception as exc:  # noqa: BLE001 - 取消结果必须保留停止失败证据
        stop_error = str(exc)
        stopped_state = {**state, "stop_error": stop_error}
        logger.warning("[agent-team] stop sandbox {} after cancellation failed: {}", public_id, exc)
    return _result(
        "cancelled",
        f"{action}因团队任务取消、过期或租约回收而停止",
        evidence=[{"source": "sandbox_environment", "data": stopped_state}],
        artifacts=[{"type": "sandbox_environment", "data": stopped_state}],
        errors=([{"code": "sandbox_stop_failed", "message": stop_error}] if stop_error else []),
    )


def _wait_for_sandbox_terminal(
    db: Session,
    user: User,
    created: AgentResult,
    *,
    action: str,
    success_states: frozenset[str],
    team_id: Optional[int] = None,
    task_id: Optional[int] = None,
    lease_token: str = "",
) -> dict[str, Any]:
    """把沙箱异步入队结果收敛为团队任务的真实终态。"""

    if not created.success:
        return _as_mesh_result(created, action=action)
    if not isinstance(created.data, dict):
        return _sandbox_failure_result(
            action,
            {},
            status="invalid_result",
            message="未返回结构化沙箱结果",
            code="sandbox_invalid_result",
        )
    state = dict(created.data)
    public_id = str(state.get("public_id") or "").strip()
    if not public_id:
        return _sandbox_failure_result(
            action,
            state,
            status="invalid_result",
            message="未返回沙箱编号",
            code="sandbox_missing_public_id",
        )

    terminal_states = success_states | _SANDBOX_FAILURE_STATES
    deadline = time.monotonic() + int(settings.agent_full_validation_wait_seconds)
    while str(state.get("status") or "").casefold() not in terminal_states:
        # 刷新团队和任务状态；取消、过期或租约恢复后必须主动停止远端沙箱，
        # 不能只依赖旧 worker 回写时的租约拒绝。
        db.rollback()
        db.expire_all()
        if not _team_task_is_active(db, team_id, task_id, lease_token):
            return _stop_inactive_team_sandbox(db, user, public_id, state, action=action)
        if time.monotonic() >= deadline:
            last_status = str(state.get("status") or "unknown")
            return _sandbox_failure_result(
                action,
                state,
                status="timeout",
                message=f"等待沙箱 {public_id} 超时，最后状态 {last_status}",
                code="sandbox_terminal_timeout",
            )
        try:
            from app.services import sandbox_service

            state = sandbox_service.get_environment(db, user, public_id)
        except Exception as exc:  # noqa: BLE001 - 查询失败应转成可审计的团队结果
            return _sandbox_failure_result(
                action,
                state,
                status="read_failed",
                message=f"读取沙箱 {public_id} 失败：{exc}",
                code="sandbox_read_failed",
            )
        if str(state.get("status") or "").casefold() not in terminal_states:
            time.sleep(_SANDBOX_POLL_SECONDS)

    status = str(state.get("status") or "").casefold()
    if status in success_states:
        return _result(
            "completed",
            f"{action}已达到真实终态 {status}",
            evidence=[{"source": "sandbox_environment", "data": state}],
            artifacts=[{"type": "sandbox_environment", "data": state}],
        )
    return _sandbox_failure_result(
        action,
        state,
        status=status,
        message=_sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )


def _runtime_handler(db: Session, user: User, code: str, message: dict[str, Any]) -> dict[str, Any]:
    data = _payload(message)
    context = message.get("context") if isinstance(message.get("context"), dict) else {}
    team_task_id = context.get("agent_team_task_id") or context.get("task_id")
    if code == "operations":
        from app.services import ops_service

        action = str(data.get("action") or "")
        params = data.get("params", {})
        if action not in ops_service.READ_ONLY_ACTIONS:
            return _result(
                "approval_required",
                "运维写操作必须回到主小菱通过现有审批链执行",
                errors=[{"code": "main_session_approval_required", "action": action}],
                next_action={"use_main_xiaoling": True, "action": action},
            )
        try:
            validated = ops_service.validate_action_params(action, params if isinstance(params, dict) else {})
        except ValueError as exc:
            result = _result(
                "needs_clarification",
                str(exc),
                errors=[{"code": "invalid_operation_contract", "message": str(exc)}],
            )
            result["retryable"] = False
            return result
        request_id = (
            f"team-{context.get('team_id')}-task-{context.get('agent_team_task_id')}"
            f"-attempt-{context.get('attempt')}"
        )
        execution = ops_service.execute(
            db,
            user,
            action=action,
            params=validated,
            request_id=request_id,
            source="agent_team",
        )
        if execution.get("status") != "success":
            failure = _result(
                "failed",
                str(execution.get("error") or f"运维动作 {action} 失败"),
                evidence=[{"source": "ops_execution", "data": execution}],
                errors=[{"code": "ops_execution_failed", "action": action}],
            )
            failure["retryable"] = False
            return failure
        return _result(
            "completed",
            f"运维只读动作 {action} 已完成",
            evidence=[{"source": "ops_execution", "data": execution}],
            artifacts=[{"type": "ops_execution", "data": execution}],
        )

    from app.agents.orchestrator import get_request_orchestrator

    ctx = _ctx({**message, "user_id": user.id})
    orch = get_request_orchestrator(db, user=user)

    if code == "language_detector":
        if not data.get("project_name"):
            return _missing("project_name")
        return _as_mesh_result(
            orch.detect_language(str(data["project_name"]), str(data.get("description") or ""), ctx=ctx),
            action="语言识别",
        )
    if code == "project_analyzer":
        if str(data.get("operation") or "") == "inspect_project" or (
            _readonly_team_task(data) and (data.get("project_id") or context.get("project_id"))
        ):
            project_id = data.get("project_id") or context.get("project_id")
            if not project_id:
                return _missing("project_id")
            detail = orch.get_project_detail(int(project_id), ctx=ctx)
            files = orch.file_mgr.list_files(
                int(project_id),
                language=str(data.get("language") or ""),
                page=1,
                page_size=min(100, int(data.get("page_size") or 50)),
                ctx=ctx,
            )
            if not detail.success:
                return _as_mesh_result(detail, action="项目事实核验")
            return _result(
                "completed",
                "项目事实核验已完成",
                evidence=[
                    {"source": "project_detail", "data": detail.data},
                    {"source": "project_files", "data": files.data if files.success else {"error": files.error}},
                ],
            )
        folder_name = str(data.get("folder_name") or data.get("project_name") or "")
        file_names = data.get("file_names")
        if not folder_name or not isinstance(file_names, list):
            missing = []
            if not folder_name:
                missing.append("folder_name")
            if not isinstance(file_names, list):
                missing.append("file_names")
            return _missing(*missing)
        strategy_instruction = str((ctx.extra or {}).get("strategy_instruction") or "")
        return _as_mesh_result(
            orch.analyze_project(
                folder_name,
                file_names,
                strategy_instruction=strategy_instruction,
                ctx=ctx,
            ),
            action="项目分析",
        )
    if code == "code_reviewer":
        raw_code = data.get("code")
        if not isinstance(raw_code, str) or not raw_code.strip():
            return _missing("code")
        language = str(data.get("language") or "plaintext")
        rules = data.get("rules")
        rules_text = json.dumps(rules, ensure_ascii=False) if isinstance(rules, list) else str(rules or "通用质量审查")
        strategy_instruction = str((ctx.extra or {}).get("strategy_instruction") or "").strip()
        if strategy_instruction:
            rules_text = f"{rules_text}\n\n本次失败后改道策略：{strategy_instruction}"
        result = orch.review_code(
            raw_code,
            rules_text,
            language,
            file_name=str(data.get("file_name") or "snippet.txt"),
            line_offset=int(data.get("line_offset") or 0),
            ctx=ctx,
        )
        return _as_mesh_result(result, action="代码质量审查")
    if code == "test_verifier":
        project_id = data.get("project_id") or context.get("project_id")
        operation = str(data.get("operation") or "run_project_tests")
        if _readonly_team_task(data):
            operation = "inspect_existing_results"
        if operation == "inspect_existing_results":
            if not project_id:
                return _missing("project_id")
            result = orch.review_orch.list_tasks(
                project_id=int(project_id),
                status=str(data.get("status") or ""),
                page=1,
                page_size=min(100, int(data.get("page_size") or 50)),
                ctx=ctx,
            )
            return _as_mesh_result(result, action="历史测试结果核验")
        language = str(data.get("language") or "").strip()
        if not project_id or not language:
            return _missing("project_id", "language")
        released_deployments: list[dict[str, str]] = []
        if context.get("team_id") and team_task_id:
            from app.services import agent_team_service

            try:
                released_deployments = agent_team_service.handoff_dependency_runtime_resources(
                    db,
                    owner_user_id=int(user.id),
                    team_id=int(context["team_id"]),
                    task_id=int(team_task_id),
                    lease_token=str(context.get("lease_token") or ""),
                )
            except Exception as exc:  # noqa: BLE001 - 交接失败时禁止竞争同一 Worker
                failure = _result(
                    "failed",
                    f"验证前无法安全交接依赖部署资源：{exc}",
                    errors=[
                        {
                            "code": "dependency_sandbox_release_failed",
                            "message": str(exc),
                        }
                    ],
                    next_action={"reorder": "validate_before_deploy"},
                )
                failure["strategy_change"] = "改为先执行全量验证，验证通过后再启动持续部署沙箱"
                failure["retry_after_seconds"] = 5
                return failure
        if operation == "run_full_project_validation":
            result = orch.run_full_project_validation(
                project_id=int(project_id),
                language=language,
                worker_code=str(data.get("worker_code") or ""),
                source_revision_id=data.get("source_revision_id"),
                ctx=ctx,
            )
        elif operation == "run_project_tests":
            result = orch.run_project_tests(
                project_id=int(project_id),
                language=language,
                test_mode=str(data.get("test_mode") or "combined"),
                worker_code=str(data.get("worker_code") or ""),
                source_revision_id=data.get("source_revision_id"),
                remote_target_url=str(data.get("remote_target_url") or ""),
                remote_target_authorized=bool(data.get("remote_target_authorized", False)),
                ctx=ctx,
            )
        else:
            return _missing("operation(run_project_tests|run_full_project_validation)")
        final = _wait_for_sandbox_terminal(
            db,
            user,
            result,
            action="项目沙箱验证",
            success_states=frozenset({"succeeded"}),
            team_id=context.get("team_id"),
            task_id=team_task_id,
            lease_token=str(context.get("lease_token") or ""),
        )
        if released_deployments:
            final["evidence"] = [
                {"source": "dependency_sandbox_release", "data": item} for item in released_deployments
            ] + list(final.get("evidence") or [])
        return final
    if code == "sandbox_deployer":
        operation = str(data.get("operation") or "deploy")
        if operation == "deploy":
            project_id = data.get("project_id") or context.get("project_id")
            language = str(data.get("language") or "").strip()
            if not project_id or not language:
                return _missing("project_id", "language")
            result = orch.deploy_project_sandbox(
                project_id=int(project_id),
                language=language,
                ttl_hours=int(data.get("ttl_hours") or 72),
                worker_code=str(data.get("worker_code") or ""),
                source_revision_id=data.get("source_revision_id"),
                ctx=ctx,
            )
            action = "沙箱部署"
            return _wait_for_sandbox_terminal(
                db,
                user,
                result,
                action=action,
                success_states=frozenset({"ready"}),
                team_id=context.get("team_id"),
                task_id=team_task_id,
                lease_token=str(context.get("lease_token") or ""),
            )
        elif operation == "close":
            public_id = str(data.get("public_id") or "")
            if not public_id:
                return _missing("public_id")
            result = orch.close_sandbox(public_id=public_id, ctx=ctx)
            action = "沙箱关闭"
        elif operation == "extend":
            public_id = str(data.get("public_id") or "")
            if not public_id:
                return _missing("public_id")
            result = orch.extend_sandbox(public_id=public_id, hours=int(data.get("hours") or 24), ctx=ctx)
            action = "沙箱续期"
        else:
            return _missing("operation(deploy|close|extend)")
        return _as_mesh_result(result, action=action)
    if code == "project_manager":
        operation = str(data.get("operation") or "list")
        if operation == "list":
            result = orch.list_projects(
                keyword=str(data.get("keyword") or ""),
                language=str(data.get("language") or ""),
                status=str(data.get("status") or "active"),
                page=int(data.get("page") or 1),
                page_size=min(100, int(data.get("page_size") or 20)),
                ctx=ctx,
            )
        elif operation == "get":
            project_id = data.get("project_id") or context.get("project_id")
            if not project_id:
                return _missing("project_id")
            result = orch.get_project_detail(int(project_id), ctx=ctx)
        else:
            return _result("approval_required", "项目写操作必须回到小菱主会话走现有权限与审批链")
        return _as_mesh_result(result, action="项目查询")
    if code == "code_file_manager":
        operation = str(data.get("operation") or "list")
        if operation == "list":
            project_id = data.get("project_id") or context.get("project_id")
            if not project_id:
                return _missing("project_id")
            result = orch.file_mgr.list_files(
                int(project_id),
                language=str(data.get("language") or ""),
                page=int(data.get("page") or 1),
                page_size=min(100, int(data.get("page_size") or 50)),
                ctx=ctx,
            )
        elif operation == "get":
            file_id = data.get("file_id") or context.get("file_id")
            if not file_id:
                return _missing("file_id")
            result = orch.file_mgr.get_file(int(file_id), ctx=ctx)
        else:
            return _result("approval_required", "文件写操作必须回到小菱主会话走现有权限与审批链")
        return _as_mesh_result(result, action="代码文件查询")
    if code == "review_orchestrator":
        operation = str(data.get("operation") or "list")
        if operation == "list":
            result = orch.review_orch.list_tasks(
                project_id=data.get("project_id") or context.get("project_id"),
                status=str(data.get("status") or ""),
                page=int(data.get("page") or 1),
                page_size=min(100, int(data.get("page_size") or 20)),
                ctx=ctx,
            )
        elif operation == "get":
            task_id = data.get("task_id") or context.get("task_id")
            if not task_id:
                return _missing("task_id")
            result = orch.review_orch.get_task_detail(int(task_id), ctx=ctx)
        elif operation == "issues":
            task_id = data.get("task_id") or context.get("task_id")
            if not task_id:
                return _missing("task_id")
            result = orch.review_orch.list_issues(int(task_id), ctx=ctx)
        else:
            return _result("approval_required", "启动审查属于异步业务写操作，必须由小菱主会话明确发起")
        return _as_mesh_result(result, action="审查任务查询")
    if code == "dashboard":
        operation = str(data.get("operation") or "summary")
        if operation == "summary":
            result = orch.dashboard_agent.summary(ctx=ctx)
        elif operation == "risk_distribution":
            result = orch.dashboard_agent.risk_distribution(days=int(data.get("days") or 30), ctx=ctx)
        elif operation == "score_trend":
            result = orch.dashboard_agent.score_trend(limit=int(data.get("limit") or 10), ctx=ctx)
        else:
            return _missing("operation(summary|risk_distribution|score_trend)")
        return _as_mesh_result(result, action="仪表盘查询")
    if code == "reporter":
        dependency_context = data.get("dependency_context")
        if isinstance(dependency_context, dict) and dependency_context:
            return _result(
                "completed",
                "子 Agent 结果汇总已完成",
                evidence=[
                    {"source": "agent_team_dependency", "task_key": key, "data": value}
                    for key, value in dependency_context.items()
                ],
                artifacts=[{"type": "agent_team_summary", "data": dependency_context}],
            )
        task_id = data.get("task_id") or context.get("task_id")
        result = (
            orch.reporter.get_report_detail(int(task_id), ctx=ctx)
            if task_id
            else orch.reporter.list_reports(project_id=data.get("project_id") or context.get("project_id"), ctx=ctx)
        )
        return _as_mesh_result(result, action="报告查询")
    if code == "rule_manager":
        if str(data.get("operation") or "list") != "list":
            return _result("approval_required", "规则变更必须回到小菱主会话走确认与审计")
        return _as_mesh_result(orch.rule_mgr.list_rules(ctx=ctx), action="规则查询")
    if code == "ai_prompt":
        target_tool = str(data.get("target_tool") or "generic")
        if issue_id := data.get("issue_id"):
            result = orch.ai_prompt.execute_for_issue(int(issue_id), target_tool=target_tool, ctx=ctx)
        elif task_id := (data.get("task_id") or context.get("task_id")):
            result = orch.ai_prompt.execute_for_task(int(task_id), target_tool=target_tool, ctx=ctx)
        elif project_id := (data.get("project_id") or context.get("project_id")):
            result = orch.ai_prompt.execute_for_project(int(project_id), target_tool=target_tool, ctx=ctx)
        else:
            return _missing("issue_id|task_id|project_id")
        return _as_mesh_result(result, action="修复提示词生成")
    if code == "security_sentinel":
        if file_id := (data.get("file_id") or context.get("file_id")):
            result = orch.security_sentinel.scan_file(
                int(file_id),
                scan_depth=str(data.get("scan_depth") or "standard"),
                ctx=ctx,
            )
        elif task_id := (data.get("task_id") or context.get("task_id")):
            result = orch.security_sentinel.scan_task(int(task_id), ctx=ctx)
        elif project_id := (data.get("project_id") or context.get("project_id")):
            result = orch.security_sentinel.scan_project(int(project_id), top_n=int(data.get("top_n") or 50), ctx=ctx)
        else:
            return _missing("file_id|task_id|project_id")
        return _as_mesh_result(result, action="安全审查")
    return _result("needs_configuration", f"Agent {code} 尚未绑定消息 Handler")


def _monitor_handler(db: Session, user: User, message: dict[str, Any]) -> dict[str, Any]:
    data = _payload(message)
    window = data.get("window_minutes")
    metrics = data.get("metrics")
    if window is None or not isinstance(metrics, list) or not metrics:
        missing = []
        if window is None:
            missing.append("window_minutes")
        if not isinstance(metrics, list) or not metrics:
            missing.append("metrics")
        result = _missing(*missing)
        if metrics is not None and not isinstance(metrics, list):
            result["summary"] = "metrics 必须是非空指标名字符串列表，不能传入服务器事实对象"
            result["errors"] = [{"code": "invalid_field_type", "field": "metrics", "expected": "list[string]"}]
        return result
    if str(user.role) not in {"admin", "super_admin"}:
        return _result(
            "blocked",
            "普通账户不能读取全平台治理指标",
            errors=[{"code": "insufficient_scope", "message": "需要管理员会话"}],
        )
    minutes = max(1, min(int(window), 1440))
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    snapshots = (
        db.query(AgentMetricSnapshot)
        .filter(AgentMetricSnapshot.metric_key.in_([str(item) for item in metrics]))
        .filter(or_(AgentMetricSnapshot.window_end.is_(None), AgentMetricSnapshot.window_end >= since))
        .order_by(AgentMetricSnapshot.id.desc())
        .limit(200)
        .all()
    )
    alerts = (
        db.query(AgentAlert)
        .filter(AgentAlert.status == "open", AgentAlert.create_time >= since)
        .order_by(AgentAlert.id.desc())
        .limit(100)
        .all()
    )
    evidence = [
        {
            "source": "agent_metric_snapshot",
            "metric_key": row.metric_key,
            "metric_value": row.metric_value,
            "window_start": str(row.window_start or ""),
            "window_end": str(row.window_end or ""),
        }
        for row in snapshots
    ]
    evidence.extend(
        {
            "source": "agent_alert",
            "id": row.id,
            "severity": row.severity,
            "title": row.title,
            "status": row.status,
        }
        for row in alerts
    )
    if not snapshots:
        return _result(
            "needs_clarification",
            "指定窗口内没有对应指标快照，无法据此判断是否异常",
            evidence=evidence,
            next_action={"available_fact": "open_alerts", "requested_metrics": metrics},
        )
    return _result(
        "completed",
        f"已读取最近 {minutes} 分钟的 {len(snapshots)} 条指标快照和 {len(alerts)} 条未关闭告警",
        evidence=evidence,
    )


def _custom_handler(db: Session, user: User, code: str, message: dict[str, Any]) -> dict[str, Any]:
    from app.services import published_agent_tools

    data = _payload(message)
    raw_code = data.get("code")
    if not isinstance(raw_code, str) or not raw_code.strip():
        return _missing("code")
    team_context = data.get("_agent_team")
    team_context = team_context if isinstance(team_context, dict) else {}
    snapshot = team_context.get("member_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    strategy = data.get("_execution_strategy")
    strategy = strategy if isinstance(strategy, dict) else {}
    experience = str(data.get("experience") or "").strip()
    strategy_instruction = str(strategy.get("instruction") or data.get("instructions") or "").strip()
    if strategy_instruction and strategy_instruction not in experience:
        experience = f"{experience}\n本次改道策略：{strategy_instruction}".strip()
    result = published_agent_tools.invoke_published_agent(
        db,
        user,
        agent_code=code,
        code=raw_code,
        language=str(data.get("language") or "plaintext"),
        file_name=str(data.get("file_name") or "snippet.txt"),
        rules=data.get("rules") if isinstance(data.get("rules"), list) else [],
        line_offset=int(data.get("line_offset") or 0),
        experience=experience,
        release_id=int(snapshot["release_id"]) if snapshot.get("release_id") else None,
        version_id=int(snapshot["version_id"]) if snapshot.get("version_id") else None,
        package_checksum=str(snapshot.get("package_checksum") or ""),
        template_checksum=str(snapshot.get("template_checksum") or ""),
    )
    return _result(
        "completed",
        str(result.get("summary") or "已发布 Agent 审查完成"),
        evidence=[
            {
                "source": "published_agent_release",
                "release_id": result.get("release_id"),
                "version_id": result.get("version_id"),
            }
        ],
        artifacts=[{"type": "review_result", "data": result}],
    )


def _handle(
    db: Session,
    user: User,
    target_address: str,
    message: dict[str, Any],
    *,
    trusted_team_execution: bool = False,
) -> tuple[str, dict[str, Any]]:
    code = target_address.split(":", 1)[1]
    if target_address.startswith("custom:"):
        asset = (
            db.query(CustomAgent)
            .filter(
                CustomAgent.code == code,
                CustomAgent.is_enabled == 1,
                CustomAgent.status == "published",
            )
            .first()
        )
        if asset is None:
            return code, _result("blocked", "已发布 Agent 不存在或已停用")
        return asset.name, _custom_handler(db, user, code, message)
    contract = CONTRACTS[code]
    from app.services import agent_governance_service

    if not agent_governance_service.is_runtime_enabled(db, code):
        return contract.name, _result("blocked", f"{contract.name}已停用，消息未执行")
    state = dispatch_state(target_address)
    if state == "session_only":
        surface = "admin" if code == "manager" else "user"
        return contract.name, _result(
            "needs_clarification",
            f"{contract.name}是有状态入口，请改发到 session:{surface}:<session_id>",
        )
    if state == "approval_required" and not (
        trusted_team_execution and code in {"test_verifier", "sandbox_deployer", "operations"}
    ):
        return contract.name, _result(
            "approval_required",
            f"{contract.name}涉及受治理动作，必须由小菱主会话通过现有权限、确认和审计链调用",
        )
    if state == "needs_configuration":
        return contract.name, _result(
            "needs_configuration",
            f"{contract.name}当前只有职责契约，尚无可审计的消息 Handler",
        )
    if code == "monitor":
        return contract.name, _monitor_handler(db, user, message)
    return contract.name, _runtime_handler(db, user, code, message)


def _candidate_rows(db: Session, limit: int) -> list[tuple[str, int, str]]:
    now = datetime.now(timezone.utc)
    rows = (
        db.query(AgentMeshMessage.message_id, AgentMeshMessage.user_id, AgentMeshMessage.send_to)
        .filter(
            AgentMeshMessage.message_type == "task.request",
            or_(AgentMeshMessage.send_to.like("agent:%"), AgentMeshMessage.send_to.like("custom:%")),
            AgentMeshMessage.status.in_(("queued", "delivered")),
            or_(AgentMeshMessage.expires_at.is_(None), AgentMeshMessage.expires_at > now),
            or_(AgentMeshMessage.next_attempt_at.is_(None), AgentMeshMessage.next_attempt_at <= now),
        )
        .order_by(AgentMeshMessage.id.asc())
        .limit(max(1, min(int(limit), 20)))
        .all()
    )
    return [(str(row[0]), int(row[1]), str(row[2])) for row in rows]


def dispatch_once(*, limit: int = 4) -> dict[str, int]:
    """消费一批 Agent 请求；跨进程安全性由数据库租约保证。"""
    from app.services import agent_mesh_service

    stats = {"candidates": 0, "claimed": 0, "completed": 0, "failed": 0, "expired": 0, "recovered": 0}
    db = SessionLocal()
    try:
        stats["expired"] = agent_mesh_service.expire_unclaimed_dispatch_messages(db)
        stats["recovered"] = agent_mesh_service.recover_stale_dispatch_messages(db)
        candidates = _candidate_rows(db, limit)
        stats["candidates"] = len(candidates)
        for message_id, user_id, target_address in candidates:
            user = db.query(User).filter(User.id == user_id).first()
            if user is None:
                logger.warning("[agent-mesh-dispatcher] skip inactive user message_id={}", message_id)
                continue
            claimed = agent_mesh_service.claim_dispatch_message(
                db,
                user,
                message_id,
                target_address=target_address,
                lease_seconds=settings.agent_mesh_dispatch_lease_seconds,
            )
            if claimed is None:
                continue
            stats["claimed"] += 1
            lease_token = str(claimed.pop("lease_token"))
            try:
                if int(user.status or 0) != 1:
                    target_name = target_address
                    result = _result("blocked", "账户已停用或删除，Agent 消息未执行")
                else:
                    target_name, result = _handle(db, user, target_address, {**claimed, "user_id": user.id})
                completion = agent_mesh_service.complete_dispatch_message(
                    db,
                    user,
                    message_id,
                    target_address=target_address,
                    target_name=target_name,
                    lease_token=lease_token,
                    success=True,
                    summary=result,
                )
                if completion["status"] == "completed":
                    stats["completed"] += 1
                else:
                    stats["failed"] += 1
            except Exception as exc:  # noqa: BLE001 - 单条消息失败不能中断消费者
                db.rollback()
                try:
                    target_name = CONTRACTS.get(target_address.split(":", 1)[1])
                    agent_mesh_service.complete_dispatch_message(
                        db,
                        user,
                        message_id,
                        target_address=target_address,
                        target_name=getattr(target_name, "name", target_address),
                        lease_token=lease_token,
                        success=False,
                        summary={"status": "failed", "summary": str(exc)},
                        error=str(exc),
                    )
                except Exception as completion_exc:  # noqa: BLE001
                    db.rollback()
                    logger.warning(
                        "[agent-mesh-dispatcher] failure persistence failed message_id={} error={}",
                        message_id,
                        completion_exc,
                    )
                stats["failed"] += 1
                logger.warning("[agent-mesh-dispatcher] message_id={} failed: {}", message_id, exc)
        return stats
    finally:
        db.close()


def start_agent_mesh_dispatcher() -> None:
    """启动独立于治理任务开关的 Agent Mesh 消费器。"""
    global _scheduler
    if not settings.agent_mesh_dispatcher_enabled:
        logger.info("[agent-mesh-dispatcher] disabled by config")
        return
    if _scheduler and getattr(_scheduler, "running", False):
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        dispatch_once,
        "interval",
        id="agent-mesh-dispatch",
        seconds=max(1, int(settings.agent_mesh_dispatch_interval_seconds)),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[agent-mesh-dispatcher] started")


def stop_agent_mesh_dispatcher() -> None:
    """停止 Agent Mesh 消费器。"""
    global _scheduler
    if not _scheduler:
        return
    try:
        if getattr(_scheduler, "running", False):
            _scheduler.shutdown(wait=False)
    finally:
        _scheduler = None
        logger.info("[agent-mesh-dispatcher] stopped")
