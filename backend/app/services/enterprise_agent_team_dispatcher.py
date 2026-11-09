"""Bounded worker adapter for the enterprise Agent-team durable queue."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any, Callable

from loguru import logger
from sqlalchemy import distinct
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.billing_ledger import BillingReservation, BillingWallet
from app.models.enterprise_agent_team import (
    EnterpriseAgentTeam,
    EnterpriseAgentTeamEvent,
    EnterpriseAgentTeamMember,
    EnterpriseAgentTeamTask,
)
from app.models.enterprise_chat import EnterpriseChatMessage
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import enterprise_agent_team_runtime_service as runtime

_scheduler = None


def dispatch_once(
    *,
    session_factory,
    execute_claimed: Callable[[Any, dict[str, Any]], Any],
    enterprise_id: int,
    batch_size: int = 20,
    concurrency: int = 4,
) -> dict[str, int]:
    """Claim a bounded batch, execute it concurrently, and classify safe failures.

    `execute_claimed` must make each provider request through
    `runtime.metered_team_call` and persist results through `complete_task`.
    Exceptions are safe to retry only when the billing ledger proves this attempt
    never reserved an upstream request; otherwise `fail_task` freezes it for review.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("企业 Agent 团队 dispatcher batch_size 必须为 1 到 100")
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise ValueError("企业 Agent 团队 dispatcher concurrency 必须为 1 到 16")
    if not callable(execute_claimed):
        raise ValueError("企业 Agent 团队 dispatcher 缺少执行器")

    stats = {"claimed": 0, "completed": 0, "retryable": 0, "reconciliation": 0, "failed": 0}
    claims = []
    for _ in range(batch_size):
        claim = runtime.claim_next_task(session_factory, enterprise_id=enterprise_id)
        if claim is None:
            break
        claims.append(claim)
    stats["claimed"] = len(claims)
    for claim in claims:
        claim["enterprise_id"] = int(enterprise_id)
    if not claims:
        return stats

    workers = min(len(claims), concurrency)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="enterprise-agent-team") as pool:
        futures = {pool.submit(execute_claimed, session_factory, claim): claim for claim in claims}
        for future in as_completed(futures):
            claim = futures[future]
            try:
                future.result()
                with session_factory() as db:
                    task = (
                        db.query(EnterpriseAgentTeamTask.status)
                        .filter_by(
                            id=claim["task_id"],
                            team_id=claim["team_id"],
                        )
                        .first()
                    )
                    task_status = str(task.status) if task is not None else "missing"
                if task_status == "completed":
                    stats["completed"] += 1
                    continue
                if task_status == "retryable":
                    stats["retryable"] += 1
                    continue
                if task_status == "awaiting_reconciliation":
                    stats["reconciliation"] += 1
                    continue
                if task_status in {"failed", "cancelled", "suppressed"}:
                    stats["failed"] += 1
                    continue
                result = runtime.fail_task(
                    session_factory,
                    enterprise_id=enterprise_id,
                    task_id=claim["task_id"],
                    lease_token=claim["lease_token"],
                    lease_generation=claim["lease_generation"],
                    error_code="executor_returned_without_completion",
                )
                stats[
                    result["status"] if result["status"] in {"retryable", "awaiting_reconciliation"} else "failed"
                ] += 1
            except Exception as exc:  # noqa: BLE001 - task state records retry/freeze decision
                logger.exception(
                    "[enterprise-agent-team] task executor failed enterprise={} team={} task={}",
                    enterprise_id,
                    claim["team_id"],
                    claim["task_id"],
                )
                try:
                    result = runtime.fail_task(
                        session_factory,
                        enterprise_id=enterprise_id,
                        task_id=claim["task_id"],
                        lease_token=claim["lease_token"],
                        lease_generation=claim["lease_generation"],
                        error_code=type(exc).__name__,
                    )
                    stats[
                        result["status"] if result["status"] in {"retryable", "awaiting_reconciliation"} else "failed"
                    ] += 1
                except Exception:  # noqa: BLE001 - stale workers cannot replace a newer task outcome
                    logger.exception(
                        "[enterprise-agent-team] task failure state could not be persisted enterprise={} task={}",
                        enterprise_id,
                        claim["task_id"],
                    )
                    stats["failed"] += 1
    return stats


def _request_call_index(request_key: str) -> int:
    try:
        value = int(str(request_key).rsplit(":", 1)[1])
    except (IndexError, TypeError, ValueError) as exc:
        raise runtime.ValidationError("企业团队模型调用序号无效") from exc
    if value <= 0:
        raise runtime.ValidationError("企业团队模型调用序号无效")
    return value


def _task_billing_scope(
    factory,
    claim: dict[str, Any],
    team: EnterpriseAgentTeam,
    task: EnterpriseAgentTeamTask,
    member: EnterpriseAgentTeamMember,
):
    from app.models.enterprise_chat import EnterpriseAssistantRun
    from app.services import enterprise_assistant_billing_group as billing_group
    from app.services.billing_runtime_gate import billing_scope

    enterprise_id = int(team.enterprise_id)
    user_id = int(team.initiator_user_id)
    team_id, task_id = int(team.id), int(task.id)
    lease_token = str(claim["lease_token"])
    lease_generation = int(claim["lease_generation"])
    prefix = f"ent-team:{team_id}:{task_id}:{int(claim['attempt'])}"
    with factory() as db:
        parent_run = db.query(EnterpriseAssistantRun).filter_by(
            id=int(team.assistant_run_id), enterprise_id=enterprise_id, conversation_id=int(team.conversation_id),
        ).first()
        if parent_run is None:
            raise runtime.ForbiddenError("企业 Agent 团队父运行不存在")
        billing_group_id = billing_group.frozen(parent_run)

    def validate_live(db: Session, request_key: str | None = None) -> dict[str, Any]:
        context = runtime._lock_live_context(db, enterprise_id=enterprise_id, task_id=task_id)
        if billing_group.frozen(context["run"]) != billing_group_id:
            raise runtime.ForbiddenError("企业 Agent 团队计费归属已变化")
        runtime._verify_approval_for_live_context(db, context)
        runtime._check_lease(
            context,
            lease_token=lease_token,
            lease_generation=lease_generation,
            now=runtime._now(),
        )
        live_team, live_task, live_member = context["team"], context["task"], context["member"]
        if (
            int(live_team.id) != team_id
            or int(live_task.id) != task_id
            or int(live_member.id) != int(member.id)
            or live_member.address != member.address
            or live_member.kind != "registered"
        ):
            raise runtime.ForbiddenError("企业团队成员或任务绑定已变化")
        from app.services.enterprise_agent_team_service import _contract_snapshot

        code = str(live_member.address).split(":", 1)[-1]
        _contract, live_capabilities = _contract_snapshot(code)
        if runtime._digest(live_capabilities) != live_member.snapshot_sha256:
            raise runtime.ForbiddenError("注册 Agent 能力版本已变化，拒绝执行旧任务计划")
        if request_key is not None:
            call_index = _request_call_index(request_key)
            if call_index > int(context["plan"].get("max_calls_per_task") or 0):
                raise runtime.PaymentRequiredError("团队任务模型调用次数达到计划上限")
        live_task.lease_expires_at = runtime._now() + timedelta(seconds=300)
        return context

    def attempt_guard(db: Session, request_key: str) -> None:
        validate_live(db, request_key)

    def event(db: Session, event_type: str, request_key: str) -> None:
        db.add(
            EnterpriseAgentTeamEvent(
                team_id=team_id,
                task_id=task_id,
                member_id=int(member.id),
                enterprise_id=enterprise_id,
                conversation_id=int(team.conversation_id),
                actor_address=str(member.address)[:200],
                correlation_id=hashlib.sha256(request_key.encode("utf-8")).hexdigest()[:64],
                event_type=event_type,
                detail_json=runtime._canonical_json(
                    {
                        "attempt": int(claim["attempt"]),
                        "call_index": _request_call_index(request_key),
                    }
                ),
            )
        )

    def on_reserved(db: Session, request_key: str) -> None:
        context = validate_live(db, request_key)
        plan = context["plan"]
        reservations = runtime._team_reservations(db, context["team"])
        reserved_total = sum(
            int(row.actual_milli if row.status == "settled" else row.requested_milli or 0)
            for row in reservations
            if row.status in {"reserved", "unknown", "settled"}
        )
        if reserved_total > int(plan["max_team_credits_milli"]):
            raise runtime.PaymentRequiredError("企業 Agent 團隊預算已用盡，本次模型请求未发送")
        event(db, "billing.reserved", request_key)

    def on_unknown(db: Session, request_key: str) -> None:
        # Provider outcomes are accounting facts. Do not let a subscription,
        # quota or membership change erase the outcome after a request was sent.
        event(db, "billing.unknown", request_key)

    def on_settled(db: Session, request_key: str) -> None:
        event(db, "billing.settled", request_key)

    def send_guard(send: Callable, request_key: str):
        sent = False
        try:
            with factory.begin() as db:
                context = validate_live(db, request_key)
                reservation = (
                    db.query(BillingReservation)
                    .filter_by(
                        request_key=request_key,
                        enterprise_id=enterprise_id,
                        user_id=user_id,
                        group_id=billing_group_id,
                        status="reserved",
                    )
                    .first()
                )
                if reservation is None:
                    raise runtime.ConflictError("企业团队模型预占失效，拒绝发送")
                context["task"].lease_expires_at = runtime._now() + timedelta(seconds=900)
                sent = True
                return send()
        except BaseException as exc:
            if sent:
                raise
            try:
                with factory.begin() as db:
                    ledger.release_not_sent(db, request_key)
                    db.add(
                        EnterpriseAgentTeamEvent(
                            team_id=team_id,
                            task_id=task_id,
                            member_id=int(member.id),
                            enterprise_id=enterprise_id,
                            conversation_id=int(team.conversation_id),
                            actor_address=str(member.address)[:200],
                            correlation_id=hashlib.sha256(request_key.encode("utf-8")).hexdigest()[:64],
                            event_type="billing.not_sent",
                            detail_json=runtime._canonical_json(
                                {
                                    "attempt": int(claim["attempt"]),
                                    "call_index": _request_call_index(request_key),
                                }
                            ),
                        )
                    )
            except Exception as release_error:  # noqa: BLE001 - unresolved holds must remain frozen
                raise ledger.BillingError("模型请求未发送，但预占无法安全释放，账单待核对") from release_error
            from app.services.billing_metered_call import MeteredRequestNotSent

            raise MeteredRequestNotSent("团队已取消或授权变化，模型请求未发送且预占已释放") from exc

    try:
        team_plan = json.loads(team.plan_snapshot_json or "{}")
    except (TypeError, ValueError) as exc:
        raise runtime.ConflictError("企业团队计费计划损坏") from exc
    with factory() as wallet_db:
        wallet = (
            wallet_db.query(BillingWallet)
            .filter_by(
                owner_type="enterprise",
                owner_id=enterprise_id,
            )
            .first()
        )
        if wallet is None:
            raise runtime.PaymentRequiredError("企业积分账本不存在，任务未执行")
        wallet_id = int(wallet.id)

    return billing_scope(
        session_factory=factory,
        user_id=user_id,
        max_input_tokens=int(team_plan["max_input_tokens_per_call"]),
        max_output_tokens=int(team_plan["max_output_tokens_per_call"]),
        enterprise_wallet_id=wallet_id,
        enterprise_id=enterprise_id,
        group_id=billing_group_id,
        personal_wallet_id=None,
        allow_personal=False,
        usage_kind="member",
        request_key_prefix=prefix,
        attempt_guard=attempt_guard,
        send_guard=send_guard,
        on_reserved=on_reserved,
        on_unknown=on_unknown,
        on_settled=on_settled,
        allow_streaming=False,
    )


def _message_for_claim(team: EnterpriseAgentTeam, claim: dict[str, Any]) -> dict[str, Any]:
    task_input = claim.get("input") if isinstance(claim.get("input"), dict) else {}
    task_id, team_id = int(claim["task_id"]), int(claim["team_id"])
    try:
        plan_snapshot = json.loads(team.plan_snapshot_json or "{}")
    except (TypeError, ValueError) as exc:
        raise runtime.ConflictError("企业团队执行计划快照损坏") from exc
    if not isinstance(plan_snapshot, dict):
        raise runtime.ConflictError("企业团队执行计划快照格式无效")
    max_calls_per_task = plan_snapshot.get("max_calls_per_task")
    if type(max_calls_per_task) is not int or max_calls_per_task <= 0:
        raise runtime.ConflictError("企业团队任务模型调用上限无效")
    agent_code = str(claim.get("member_address") or "").split(":", 1)[-1]
    if agent_code == "security_sentinel" and (type(team.project_id) is not int or team.project_id <= 0):
        raise runtime.ForbiddenError("企业安全审计任务没有服务端绑定项目")
    context = {
        "team_id": team_id,
        "agent_team_task_id": task_id,
        "lease_token": str(claim["lease_token"]),
        "lease_generation": int(claim["lease_generation"]),
        "enterprise_id": int(team.enterprise_id),
        "conversation_id": int(team.conversation_id),
        "group_id": int(team.group_id) if team.group_id is not None else None,
        "max_calls_per_task": max_calls_per_task,
    }
    if agent_code == "security_sentinel":
        context["project_id"] = int(team.project_id)
    return {
        "message_id": f"enterprise-team-{team_id}-{task_id}-{claim['attempt']}",
        "user_id": int(team.initiator_user_id),
        "trace_id": str(team.trace_id),
        "correlation_id": f"enterprise-team:{team_id}:task:{task_id}",
        "causation_id": "",
        "sent_from": f"enterprise-conversation:{team.conversation_id}",
        "send_to": str(claim["member_address"]),
        "message_type": "task.request",
        "payload": {
            **task_input,
            "instructions": str(claim["instructions"]),
            "title": str(claim["title"]),
            "execution_mode": "read_only",
            "_agent_team": {
                "team_id": team_id,
                "task_id": task_id,
                "attempt": int(claim["attempt"]),
                "lease_token": str(claim["lease_token"]),
            },
        },
        "context": context,
    }


def _security_artifact_from_result(
    result: dict[str, Any],
    *,
    project_id: int,
) -> dict[str, Any] | None:
    """Extract a bounded immutable artifact only from the scoped Sentinel result."""
    evidence = result.get("evidence")
    if not isinstance(evidence, list):
        return None
    reports = [
        item.get("data")
        for item in evidence
        if isinstance(item, dict)
        and item.get("source") == "request_scoped_agent"
        and isinstance(item.get("data"), dict)
    ]
    if len(reports) != 1:
        return None
    report = reports[0]
    compliance = report.get("compliance")
    findings = report.get("findings")
    summary_text = report.get("summary")
    if (
        type(report.get("project_id")) is not int
        or int(report["project_id"]) != int(project_id)
        or not isinstance(compliance, dict)
        or not isinstance(findings, list)
        or not isinstance(summary_text, str)
        or not summary_text.strip()
        or not re.fullmatch(r"[0-9a-f]{64}", str(report.get("source_archive_sha256") or ""))
        or type(compliance.get("provider_request_count")) is not int
        or compliance["provider_request_count"] < 0
    ):
        return None

    scan_mode = str(compliance.get("scan_mode") or "")
    complete = (
        str(result.get("status") or "") == "completed"
        and scan_mode in {"full", "static_full", "triage"}
        and compliance.get("audit_inputs_complete") is True
        and compliance.get("semantic_execution_complete") is True
        and compliance.get("audit_request_accounting_complete") is True
        and not compliance.get("findings_truncated")
        and not compliance.get("result_payload_truncated")
        and not (compliance.get("dataflow_attempted") and compliance.get("dataflow_complete") is not True)
        and not (scan_mode == "full" and compliance.get("semantic_complete") is not True)
        and not (scan_mode in {"full", "static_full"} and compliance.get("static_complete") is not True)
    )
    verification = compliance.get("verification")
    if complete:
        complete = (
            isinstance(verification, dict)
            and verification.get("enabled") is True
            and verification.get("complete") is True
        )

    errors = result.get("errors")
    error_code = next(
        (
            str(item.get("code") or "")
            for item in errors
            if isinstance(item, dict) and str(item.get("code") or "")
        ),
        "",
    ) if isinstance(errors, list) else ""
    termination_reason = "" if complete else re.sub(
        r"[^a-z0-9_.-]+", "_",
        str(result.get("failure_kind") or error_code or "coverage_incomplete").lower(),
    ).strip("_.-")[:64] or "coverage_incomplete"
    return {
        "audit_run_id": uuid.uuid4().hex,
        "source_sha256": str(report["source_archive_sha256"]),
        "coverage_status": "complete" if complete else "incomplete",
        "termination_reason": termination_reason,
        "summary": {
            "summary": summary_text.strip()[:12_000],
            "project_id": int(project_id),
            "scan_mode": scan_mode or None,
            "finding_count": len(findings),
            "provider_request_count": int(compliance["provider_request_count"]),
            "coverage_ratio": compliance.get("coverage_ratio"),
        },
        "report": report,
    }


def execute_registered_agent(session_factory, claim: dict[str, Any]) -> dict[str, Any]:
    """Execute one allowlisted read-only registered Agent under enterprise billing and a fenced lease."""
    from app.services import agent_governance_service, enterprise_agent_team_service
    from app.services.agent_mesh_dispatcher import _handle, dispatch_state
    from app.services.agent_scheduler_runtime import agent_execution_scope, check_agent_execution
    from app.services.ai_usage_context import usage_context

    with session_factory() as db:
        context = runtime._lock_live_context(
            db,
            enterprise_id=int(claim["enterprise_id"]),
            task_id=int(claim["task_id"]),
        )
        team, task, member = context["team"], context["task"], context["member"]
        code = str(member.address).split(":", 1)[-1]
        if (
            member.kind != "registered"
            or member.address != f"agent:{code}"
            or code not in enterprise_agent_team_service._EXECUTABLE_AGENT_CODES
            or dispatch_state(member.address) != "executable"
            or not agent_governance_service.is_runtime_enabled(db, code)
        ):
            raise runtime.ForbiddenError("企業團隊 Agent 未注册、已停用或超出当前只读白名单")
        _contract, capabilities = enterprise_agent_team_service._contract_snapshot(code)
        if runtime._digest(capabilities) != member.snapshot_sha256:
            raise runtime.ForbiddenError("企业团队 Agent 运行能力与创建时快照不一致")
        user = db.query(User).filter_by(id=int(team.initiator_user_id), status=1).first()
        if user is None:
            raise runtime.ForbiddenError("企业团队发起账号已停用")
        bind = db.get_bind()
        engine = getattr(bind, "engine", bind)
        factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
        team_id, task_id = int(team.id), int(task.id)
        lease_token = str(claim["lease_token"])
        lease_generation = int(claim["lease_generation"])

    def check_lease() -> None:
        with session_factory() as check_db:
            live = runtime._lock_live_context(
                check_db,
                enterprise_id=int(claim["enterprise_id"]),
                task_id=int(claim["task_id"]),
                # This guard fences identity, approval and the lease between
                # already-issued requests. Billing is enforced again by the
                # metered scope before each provider request, while an issued
                # request must still be allowed to settle after subscription expiry.
                require_active_billing=False,
            )
            runtime._verify_approval_for_live_context(check_db, live)
            runtime._check_lease(
                live,
                lease_token=lease_token,
                lease_generation=lease_generation,
                now=runtime._now(),
            )

    with session_factory() as db:
        context = runtime._lock_live_context(
            db,
            enterprise_id=int(claim["enterprise_id"]),
            task_id=int(claim["task_id"]),
        )
        team, task, member = context["team"], context["task"], context["member"]
        team_project_id = int(team.project_id) if team.project_id is not None else None
        user = db.get(User, int(team.initiator_user_id))
        scope = _task_billing_scope(factory, claim, team, task, member)
        attribution = {
            "agent_team_id": team_id,
            "agent_team_task_id": task_id,
            "root_agent_run_id": int(team.assistant_run_id),
        }
        try:
            with agent_execution_scope(check_lease), usage_context(int(user.id), attribution, db=db), scope:
                check_agent_execution()
                _name, result = _handle(
                    db,
                    user,
                    str(claim["member_address"]),
                    _message_for_claim(team, claim),
                    trusted_team_execution=True,
                )
                check_agent_execution()
        finally:
            db.rollback()
    if not isinstance(result, dict):
        raise runtime.ValidationError("注册 Agent 没有返回结构化结果")
    if code == "security_sentinel":
        if team_project_id is None:
            raise runtime.ForbiddenError("企业安全审计任务缺少项目范围")
        artifact = _security_artifact_from_result(result, project_id=team_project_id)
        if (
            str(result.get("status") or "") == "completed"
            and artifact is not None
            and artifact["coverage_status"] == "complete"
        ):
            saved = runtime.complete_task(
                session_factory,
                enterprise_id=int(claim["enterprise_id"]),
                task_id=int(claim["task_id"]),
                lease_token=lease_token,
                lease_generation=lease_generation,
                result={"status": "completed"},
                security_artifact=artifact,
            )
        else:
            failure_kind = str(result.get("failure_kind") or "security_scan_incomplete")[:64]
            retryable = result.get("retryable") if type(result.get("retryable")) is bool else False
            failure_result = {
                "status": "blocked",
                "summary": str(result.get("summary") or "企业项目安全扫描未形成完整、可校验的报告")[:2000],
                "agent_status": str(result.get("status") or "unknown"),
                "errors": result.get("errors") if isinstance(result.get("errors"), list) else [],
            }
            if artifact is not None and retryable is not True:
                failure_result["coverage_status"] = artifact["coverage_status"]
                failure_result["termination_reason"] = artifact["termination_reason"]
            saved = runtime.fail_task(
                session_factory,
                enterprise_id=int(claim["enterprise_id"]),
                task_id=int(claim["task_id"]),
                lease_token=lease_token,
                lease_generation=lease_generation,
                error_code=failure_kind,
                result=failure_result,
                retryable=retryable,
                security_artifact=artifact if artifact is not None and retryable is False else None,
            )
    elif str(result.get("status") or "") == "completed":
        saved = runtime.complete_task(
            session_factory,
            enterprise_id=int(claim["enterprise_id"]),
            task_id=int(claim["task_id"]),
            lease_token=lease_token,
            lease_generation=lease_generation,
            result=result,
        )
    else:
        failure_kind = str(result.get("failure_kind") or "")[:64]
        retryable = result.get("retryable") if type(result.get("retryable")) is bool else None
        failure_result = {
            "status": "blocked",
            "summary": str(result.get("summary") or "Agent 任务未完成")[:2000],
            "agent_status": str(result.get("status") or "unknown"),
            "errors": result.get("errors") if isinstance(result.get("errors"), list) else [],
        }
        if failure_kind:
            failure_result["failure_kind"] = failure_kind
        if failure_kind in {
            "budget_exhausted", "coverage_incomplete", "incomplete_response", "output_truncated",
        }:
            failure_result["coverage_status"] = "incomplete"
            failure_result["termination_reason"] = failure_kind
            coverage = _incomplete_coverage_summary(result)
            if coverage is not None:
                failure_result["coverage"] = coverage
        saved = runtime.fail_task(
            session_factory,
            enterprise_id=int(claim["enterprise_id"]),
            task_id=int(claim["task_id"]),
            lease_token=lease_token,
            lease_generation=lease_generation,
            error_code=failure_kind or str(result.get("status") or "agent_task_failed"),
            result=failure_result,
            retryable=retryable,
        )
    if saved.get("team_status") in {"completed", "failed", "cancelled"}:
        _publish_terminal_receipt(
            session_factory,
            enterprise_id=int(claim["enterprise_id"]),
            team_id=int(claim["team_id"]),
        )
    return {"status": str(saved.get("status") or "unknown"), "team_status": str(saved.get("team_status") or "unknown")}


def _incomplete_coverage_summary(result: dict[str, Any]) -> dict[str, Any] | None:
    """Keep bounded review coverage evidence when a team task stops before full coverage."""
    evidence = result.get("evidence")
    if not isinstance(evidence, list):
        return None
    for item in evidence:
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        coverage = data.get("coverage") if isinstance(data, dict) else None
        if not isinstance(coverage, dict):
            continue
        try:
            encoded = json.dumps(coverage, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        if len(encoded.encode("utf-8")) <= 48_000:
            return coverage

        summary: dict[str, Any] = {}
        for key in (
            "stage",
            "calls",
            "rule_batches_completed",
            "rule_batches_total",
            "failed_batch",
            "input_rejected_before_http",
            "total_lines",
            "recovered_from_output_truncation",
            "source_sha256",
        ):
            value = coverage.get(key)
            if type(value) in {bool, int} or (isinstance(value, str) and len(value) <= 256):
                summary[key] = value
        for key in ("completed_ranges", "failed_batch_completed_ranges"):
            value = coverage.get(key)
            if isinstance(value, list):
                sample = []
                for row in value[:50]:
                    try:
                        if len(json.dumps(row, ensure_ascii=False).encode("utf-8")) <= 256:
                            sample.append(row)
                    except (TypeError, ValueError):
                        continue
                summary[key] = sample
                summary[f"{key}_total"] = len(value)
                summary[f"{key}_truncated"] = len(sample) < len(value)
        return summary
    return None


def _publish_terminal_receipt(session_factory, *, enterprise_id: int, team_id: int) -> bool:
    """Write one minimal conversation receipt after the whole durable team is terminal."""
    with session_factory.begin() as db:
        team = (
            db.query(EnterpriseAgentTeam)
            .filter_by(
                id=int(team_id),
                enterprise_id=int(enterprise_id),
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        if team is None or team.status not in {"completed", "failed", "cancelled"}:
            return False
        prior = (
            db.query(EnterpriseAgentTeamEvent.id)
            .filter_by(
                team_id=int(team.id),
                event_type="team.receipt_published",
            )
            .first()
        )
        if prior is not None:
            return False
        if team.status == "completed":
            content = f"企业小菱协作团队「{team.title}」已完成。可在本会话打开团队进度查看各 Agent 的结果。"
        elif team.status == "cancelled":
            content = f"企业小菱协作团队「{team.title}」已停止。"
        else:
            content = f"企业小菱协作团队「{team.title}」未能全部完成。可在本会话打开团队进度查看失败任务。"
        db.add(
            EnterpriseChatMessage(
                enterprise_id=int(team.enterprise_id),
                conversation_id=int(team.conversation_id),
                sender_id=None,
                sender_type="assistant",
                assistant_run_id=None,
                client_message_id=f"ent-team-{int(team.id)}-receipt",
                content=content[:10000],
            )
        )
        db.add(
            EnterpriseAgentTeamEvent(
                team_id=int(team.id),
                enterprise_id=int(team.enterprise_id),
                conversation_id=int(team.conversation_id),
                actor_user_id=None,
                correlation_id=f"receipt:{int(team.id)}",
                event_type="team.receipt_published",
                from_status=team.status,
                to_status=team.status,
                actor_address="system",
                detail_json=runtime._canonical_json({"message_kind": "terminal_team_receipt"}),
            )
        )
        return True


def dispatch_due_once() -> dict[str, int]:
    """Scan bounded active tenants and execute only persisted queued enterprise teams."""
    stats = {
        "enterprises": 0,
        "claimed": 0,
        "completed": 0,
        "retryable": 0,
        "reconciliation": 0,
        "failed": 0,
        "receipts": 0,
    }
    if not settings.enterprise_assistant_worker_enabled:
        return stats
    with SessionLocal() as db:
        enterprise_ids = [
            int(row[0])
            for row in db.query(distinct(EnterpriseAgentTeam.enterprise_id))
            .filter(
                EnterpriseAgentTeam.status.in_({"queued", "running"}),
                EnterpriseAgentTeam.stop_requested.is_(False),
            )
            .order_by(EnterpriseAgentTeam.enterprise_id.asc())
            .limit(100)
            .all()
        ]
    stats["enterprises"] = len(enterprise_ids)
    for enterprise_id in enterprise_ids:
        result = dispatch_once(
            session_factory=SessionLocal,
            execute_claimed=execute_registered_agent,
            enterprise_id=enterprise_id,
            batch_size=int(settings.enterprise_assistant_worker_batch_size),
            concurrency=int(settings.enterprise_assistant_worker_concurrency),
        )
        for key in ("claimed", "completed", "retryable", "reconciliation", "failed"):
            stats[key] += int(result.get(key, 0))
        with SessionLocal() as db:
            terminal_ids = [
                int(row[0])
                for row in db.query(EnterpriseAgentTeam.id)
                .filter(
                    EnterpriseAgentTeam.enterprise_id == enterprise_id,
                    EnterpriseAgentTeam.status.in_({"completed", "failed", "cancelled"}),
                )
                .order_by(EnterpriseAgentTeam.id.asc())
                .limit(100)
                .all()
            ]
        for team_id in terminal_ids:
            try:
                stats["receipts"] += int(
                    _publish_terminal_receipt(
                        SessionLocal,
                        enterprise_id=enterprise_id,
                        team_id=team_id,
                    )
                )
            except Exception:  # noqa: BLE001 - receipt persistence is retried on the next queue scan
                logger.exception("[enterprise-agent-team] terminal receipt failed team={}", team_id)
    return stats


def start_enterprise_agent_team_dispatcher() -> None:
    global _scheduler
    if not settings.enterprise_assistant_worker_enabled:
        logger.info("[enterprise-agent-team] dispatcher disabled by config")
        return
    if _scheduler and getattr(_scheduler, "running", False):
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        dispatch_due_once,
        "interval",
        id="enterprise-agent-team-dispatch",
        seconds=max(1, int(settings.enterprise_assistant_worker_interval_seconds)),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[enterprise-agent-team] dispatcher started")


def stop_enterprise_agent_team_dispatcher() -> None:
    global _scheduler
    if not _scheduler:
        return
    try:
        if getattr(_scheduler, "running", False):
            _scheduler.shutdown(wait=False)
    finally:
        _scheduler = None
        logger.info("[enterprise-agent-team] dispatcher stopped")
