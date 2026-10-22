"""小菱团队内的受限站点执行器：固定业务契约、持久回执及独立读回。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.permission_codes import PermissionCode
from app.models.agent_response_run import AgentToolExecution
from app.models.agent_team import AgentTeamEvent, AgentTeamTask
from app.models.user import User
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import admin_capability_service, agent_supervisor_service, agent_team_service, rbac_service
from app.services.admin_capability_registry import CAPABILITY_BY_CODE as ADMIN_CAPABILITIES
from app.services.admin_capability_registry import operation_contract
from app.services.user_capability_registry import CAPABILITY_BY_CODE as USER_CAPABILITIES

# 凭据输入和一次性明文输出保留在主会话的安全交互，不写入团队任务/结果。
_MAIN_SESSION_ONLY = frozenset(
    {
        "users.reset_password",
        "beta_codes.generate",
        "llm.config.update",
        "embedding.config.update",
        "mcp.servers.create",
        "mcp.servers.update",
        "sandbox.workers.create",
        "sandbox.workers.update",
    }
)
_PENDING = frozenset({"queued", "pending", "running", "starting", "downloading", "scanning", "importing"})


def capability_requires_main_session(scope: str, capability: str) -> bool:
    return scope == "admin" and capability in _MAIN_SESSION_ONLY


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def resolve_site_input(value: Mapping[str, Any]):
    """指令文本和模型的 approved/URL/身份覆盖均不能改变执行契约。"""
    if not isinstance(value, Mapping):
        raise ValueError("站点任务 input 必须是 JSON object")
    operation = value.get("operation")
    if operation == "operate_current_page":
        from app.services.team_browser_service import browser_contract

        return browser_contract(value), dict(value["args"])
    allowed = {"operation"} if operation == "verify_results" else {"operation", "scope", "capability", "params"}
    unknown = {str(key) for key in value if not str(key).startswith("_")} - allowed
    if unknown:
        raise ValueError("站点任务不接受参数：" + ", ".join(sorted(unknown)))
    if operation == "verify_results":
        return None, {}
    if operation != "execute_capability" or value.get("scope") not in {"user", "admin"}:
        raise ValueError("站点任务必须显式指定 execute_capability 和 user/admin scope")
    code = str(value.get("capability") or "")
    if code in _MAIN_SESSION_ONLY:
        raise ValueError("凭据相关能力须由小菱主会话安全交互执行")
    registry = ADMIN_CAPABILITIES if value["scope"] == "admin" else USER_CAPABILITIES
    spec = registry.get(code)
    if spec is None:
        raise ValueError("站点能力不在当前范围的注册表中")
    params = value.get("params", {})
    if not isinstance(params, Mapping):
        raise ValueError("站点能力 params 必须是 JSON object")

    def sensitive(item):
        if isinstance(item, Mapping):
            return any(
                str(key).lower() in {"api_key", "password", "token", "secret", "authorization", "private_key"}
                or sensitive(child)
                for key, child in item.items()
            )
        return isinstance(item, list) and any(sensitive(child) for child in item)

    if sensitive(params):
        raise ValueError("凭据参数须由小菱主会话安全交互执行")
    from app.main import app

    admin_capability_service.prepare_request(spec, params, app.openapi())
    return spec, dict(params)


def build_capability_team_payload(
    *, scope: str, capability: str, params: Mapping[str, Any], surface: str, session_key: str, trace_id: str = ""
) -> AgentTeamCreateIn:
    task_input = {"operation": "execute_capability", "scope": scope, "capability": capability, "params": dict(params)}
    spec, _ = resolve_site_input(task_input)
    if scope == "admin" and surface != "admin":
        raise ValueError("管理能力必须由当前管理会话发起")
    title = spec.description
    return AgentTeamCreateIn.model_validate(
        {
            "surface": surface,
            "session_id": session_key,
            "trace_id": trace_id,
            "title": title,
            "objective": title + "，按真实业务回执独立核验",
            "members": [
                {
                    "member_key": "operator",
                    "display_name": "站点执行子 Agent",
                    "address": "agent:site_operator",
                    "role": "worker",
                },
                {
                    "member_key": "verifier",
                    "display_name": "站点结果核验子 Agent",
                    "address": "agent:site_operator",
                    "role": "verifier",
                },
            ],
            "tasks": [
                {
                    "task_key": "execute",
                    "member_key": "operator",
                    "title": title,
                    "instructions": "按已绑定的固定契约执行；不得猜测参数",
                    "input": task_input,
                    "max_attempts": 1,
                },
                {
                    "task_key": "verify",
                    "member_key": "verifier",
                    "title": "核验：" + title,
                    "instructions": "只核验本团队持久执行回执并读回目标，不再执行写操作",
                    "input": {"operation": "verify_results"},
                    "depends_on": ["execute"],
                    "max_attempts": 1,
                },
            ],
            "max_attempts": 1,
            "max_active_children": 2,
        }
    )


def capability_page_action(
    scope: str, capability: str, phase: str, account_id: int, result: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    registry = ADMIN_CAPABILITIES if scope == "admin" else USER_CAPABILITIES
    spec = registry[capability]
    route = spec.page
    section = getattr(spec, "section", "")
    if section:
        route += "?section=" + section
    data = result.get("data") if isinstance(result, Mapping) else None
    if capability.startswith("forum.posts.") and isinstance(data, Mapping):
        ident = data.get("id")
        if isinstance(ident, int) and not isinstance(ident, bool) and ident > 0:
            route = f"/forum/{ident}"
    return {
        "page": spec.page,
        "capability": capability,
        "phase": phase,
        "mutation": spec.risk != "read",
        "result_route": route,
        "account_id": int(account_id),
    }


def _result(status: str, summary: str, *, receipt=None, evidence=None):
    return {
        "status": status,
        "summary": summary,
        "evidence": evidence or [],
        "artifacts": [],
        "errors": [] if status == "completed" else [{"code": "site_execution_" + status}],
        "next_action": None,
        "retryable": False,
        **({"result_receipt": receipt} if receipt else {}),
    }


def _live_task(db: Session, user: User, team_id: int, task_id: int, lease_token: str):
    # 真实 API 与其它 Worker 可能提交/撤权；每个阶段都重新开启当前快照。
    db.rollback()
    db.expire_all()
    actor = db.get(User, int(user.id), populate_existing=True)
    if (
        actor is None
        or int(actor.status or 0) != 1
        or not rbac_service.check_permission(db, actor.id, PermissionCode.AGENT_CHAT)
    ):
        raise ValueError("当前账号已停用或小菱权限已撤销")
    team, task, member = agent_team_service.require_active_task_lease(
        db,
        team_id=team_id,
        task_id=task_id,
        owner_user_id=actor.id,
        lease_token=lease_token,
    )
    if member.address != "agent:site_operator" and member.kind != "temporary":
        raise ValueError("该成员不具备站点执行能力")
    if team.surface == "admin" and not rbac_service.is_admin_user(db, actor.id):
        raise ValueError("管理会话权限已撤销")
    raw = agent_team_service._unjson(task.input_json, {})
    return actor, team, task, member, raw


def _authorize_actor(db, actor, team, raw, spec):
    if raw["scope"] == "admin":
        if team.surface != "admin" or not rbac_service.is_admin_user(db, actor.id):
            raise ValueError("当前账号无权通过该会话执行管理能力")
    if spec.permission == "super_admin" or str(spec.permission or "").startswith("server_ops:"):
        if not rbac_service.is_super_admin_user(db, actor.id):
            raise ValueError("该站点能力仅唯一超级管理员可执行")
    elif spec.permission and not rbac_service.check_permission(db, actor.id, spec.permission):
        raise ValueError("当前账号缺少权限：" + spec.permission)


def _confirmed(db, team, task, member, raw):
    fingerprint = agent_supervisor_service.task_fingerprint(
        {
            "task_key": task.task_key,
            "member_key": member.member_key,
            "title": task.title,
            "instructions": task.instructions,
            "input": raw,
        },
        member.address,
    )
    for event in (
        db.query(AgentTeamEvent)
        .filter(
            AgentTeamEvent.team_id == team.id,
            AgentTeamEvent.event_type.in_(("supervisor.plan_reviewed", "supervisor.retry_reauthorized")),
        )
        .all()
    ):
        detail = agent_team_service._unjson(event.detail_json, {})
        if detail.get("confirmed_by_user_id") == team.user_id and fingerprint in detail.get(
            "authorized_high_risk_fingerprints", []
        ):
            return True
    return False


def _trusted_team_origin(db, team):
    row = (
        db.query(AgentTeamEvent)
        .filter(
            AgentTeamEvent.team_id == team.id,
            AgentTeamEvent.event_type == "supervisor.plan_reviewed",
            AgentTeamEvent.actor_address == "agent:supervisor",
        )
        .order_by(AgentTeamEvent.id.asc())
        .first()
    )
    return agent_team_service._unjson(row.detail_json, {}).get("site_request_origin") if row else None


def _readback_spec(scope, spec, params, output):
    """只选同注册表内、同目标资源的 GET；不允许模型指定回读 URL。"""
    registry = {**USER_CAPABILITIES, **ADMIN_CAPABILITIES} if scope == "admin" else USER_CAPABILITIES
    data = output.get("data") if isinstance(output, Mapping) else None
    data = data if isinstance(data, Mapping) else {}
    from app.main import app

    if spec.method == "GET":
        # 同一查询的独立读回必须保留排序、筛选和分页，不能退回另一 GET 的默认页。
        return spec, dict(params)
    candidates = sorted((item for item in registry.values() if item.method == "GET"), key=lambda item: item.code)
    for read in candidates:
        if (
            read.path != spec.path
            and not spec.path.startswith(read.path + "/")
            and not read.path.startswith(spec.path + "/{")
        ):
            continue
        contract = operation_contract(read, app.openapi())
        values = {name: params[name] for name in (*contract.path_names, *contract.query_names) if name in params}
        missing = set(contract.path_names) - set(values)
        if len(missing) == 1 and "id" in data:
            values[next(iter(missing))] = data["id"]
        if set(contract.path_names) - set(values):
            continue
        if spec.method != "GET" and not contract.path_names and spec.path != read.path:
            continue
        if not set(contract.schema.get("required", [])) - set(values):
            return read, values
    return None


async def execute_site_task(db: Session, user: User, *, team_id: int, task_id: int, lease_token: str):
    """所有身份、input、确认与回执都从持久任务取值；调用参数只有租约。"""
    try:
        actor, team, task, member, raw = _live_task(db, user, team_id, task_id, lease_token)
        if raw.get("operation") == "operate_current_page":
            from app.services.team_browser_service import execute_browser_task

            return await execute_browser_task(db, actor, team, task, member, raw, lease_token)
        spec, params = resolve_site_input(raw)
        if spec is None:
            return await _verify_dependencies(db, actor, team, task, lease_token)
        _authorize_actor(db, actor, team, raw, spec)
        review = agent_supervisor_service.review_team_task(
            address=member.address,
            task_key=task.task_key,
            title=task.title,
            instructions=task.instructions,
            task_input=raw,
        )
        from app.services import policy_engine, tool_gateway

        policy = tool_gateway.authorize(
            db,
            agent_code="manager" if raw["scope"] == "admin" else "chat_assistant",
            tool_code=raw["scope"] + "_execute_capability",
            action=raw["scope"] + "." + spec.code,
            resource=spec.page,
            actor=actor,
            declared_risk=agent_supervisor_service.declared_capability_risk(spec.risk),
            context={"surface": team.surface, "team_id": team.id, "task_id": task.id, "executor": member.address},
        )
        if policy.decision == policy_engine.DENY:
            raise ValueError("站点执行已被策略阻断：" + policy.reason)
        if (
            review.needs_confirmation
            or policy.decision == policy_engine.ESCALATE
            or policy.risk_level in {policy_engine.HIGH, policy_engine.CRITICAL}
        ) and not _confirmed(db, team, task, member, raw):
            raise ValueError("站点高风险任务缺少当前账号对同一任务的确认")
        request_id = hashlib.sha256(f"site:{actor.id}:{team.id}:{task.id}".encode()).hexdigest()
        arguments = _json(raw)
        existing = db.query(AgentToolExecution).filter_by(request_id=request_id).first()
        if existing:
            if existing.user_id != actor.id or existing.arguments_json != arguments:
                raise ValueError("持久执行回执与当前任务不匹配")
            if existing.status == "executing":
                return _result("blocked", "调用已发出但结果未确认，禁止再次执行写操作")
            return json.loads(existing.result_json)
        row = AgentToolExecution(
            request_id=request_id,
            run_id=f"site-team-{team.id}",
            call_id=str(task.id),
            user_id=actor.id,
            tool_name="site_execute_capability",
            arguments_json=arguments,
            status="executing",
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return _result("blocked", "同一任务正在执行，禁止重复业务调用")
        actor, team, task, member, current = _live_task(db, actor, team_id, task_id, lease_token)
        if _json(current) != arguments:
            raise ValueError("执行前持久任务参数已变化")
        _authorize_actor(db, actor, team, current, spec)
        try:
            from app.core.site_request_context import bind_request_origin

            with bind_request_origin(_trusted_team_origin(db, team)):
                output = await admin_capability_service.execute_api(actor, spec, params, request_id=request_id)
            from app.services.agent_responses_service import redact_agent_model_context_value

            safe = redact_agent_model_context_value(output)
            receipt = {
                "scope": raw["scope"],
                "capability": spec.code,
                "request_id": request_id,
                "request_params": redact_agent_model_context_value(params),
                "status_code": output.get("status_code"),
                "output": safe,
                "page_action": capability_page_action(raw["scope"], spec.code, "pending", actor.id, output),
            }
            data = output.get("data")
            data = data if isinstance(data, Mapping) else {}
            agent = data.get("agent")
            agent = agent if isinstance(agent, Mapping) else {}
            # 只解释显式业务状态；畸形值保留在脱敏回执中，不能参与集合或视为成功。
            values = [obj[key] for obj, key in (
                (data, "status"), (data, "execution_status"), (agent, "status"),
            ) if key in obj]
            malformed = any(not isinstance(value, str) or not value.strip() for value in values)
            states = {value.strip().lower() for value in values if isinstance(value, str) and value.strip()}
            pending = output.get("status_code") == 202 or bool(states & _PENDING)
            failed = (
                malformed or int(output.get("status_code") or 200) >= 400
                or bool(states & {"failed", "blocked", "error", "cancelled", "partial"})
                or data.get("success") is False or data.get("ok") is False
                or bool(agent.get("error")) or agent.get("success") is False or agent.get("ok") is False
            )
            result = _result(
                "failed" if failed else "blocked" if pending else "completed",
                "业务回执报告失败或仅部分完成，不能标记为完成"
                if failed
                else "业务已受理，仍需等待真实业务终态"
                if pending
                else "业务调用已执行，等待独立结果核验",
                receipt=receipt,
                evidence=[{"source": "site_api_receipt", "request_id": request_id}],
            )
        except Exception as exc:
            result = _result("failed", f"站点调用未确认成功，禁止盲目重试：{type(exc).__name__}: {exc}")
        row = db.query(AgentToolExecution).filter_by(request_id=request_id).one()
        row.status = "success" if result["status"] == "completed" else "failed"
        row.result_json = _json(result)
        row.error = None if result["status"] == "completed" else result["summary"]
        db.commit()
        return result
    except (ValueError, TypeError) as exc:
        db.rollback()
        return _result("blocked", str(exc))


async def _verify_dependencies(db, actor, team, task, lease_token):
    wanted = agent_team_service._unjson(task.dependency_keys_json, [])
    if not wanted:
        return _result("blocked", "站点核验必须依赖本团队的真实业务执行任务")
    rows = db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id, AgentTeamTask.task_key.in_(wanted)).all()
    if {row.task_key for row in rows} != set(wanted):
        return _result("blocked", "站点核验依赖任务不存在")
    receipts, evidence = [], []
    for row in rows:
        result = agent_team_service._unjson(row.result_json, {})
        receipt = result.get("result_receipt")
        if row.status != "completed" or not isinstance(receipt, dict):
            return _result("blocked", "依赖业务任务未完成或缺少真实执行回执")
        ledger = db.query(AgentToolExecution).filter_by(request_id=receipt.get("request_id"), user_id=actor.id).first()
        if (
            ledger is None
            or ledger.status != "success"
            or ledger.run_id != f"site-team-{team.id}"
            or ledger.call_id != str(row.id)
            or json.loads(ledger.result_json).get("result_receipt") != receipt
        ):
            return _result("blocked", "依赖回执与执行账本不一致")
        raw = agent_team_service._unjson(row.input_json, {})
        spec, params = resolve_site_input(raw)
        _authorize_actor(db, actor, team, raw, spec)
        pair = _readback_spec(raw["scope"], spec, params, receipt["output"])
        verified = dict(receipt)
        if pair:
            read, read_params = pair
            from app.services.agent_responses_service import redact_agent_model_context_value

            # 在实际调用处记录身份，包含 DELETE 的 404；汇总不得从预期目标补造。
            readback_identity = {"capability": read.code,
                                 "request_params": redact_agent_model_context_value(read_params)}
            try:
                readback = await admin_capability_service.execute_api(
                    actor, read, read_params, request_id=receipt["request_id"] + "-verify"
                )
            except admin_capability_service.AdminCapabilityError as exc:
                if spec.method != "DELETE" or getattr(exc, "status_code", None) != 404:
                    return _result("blocked", "目标业务结果读回失败：" + str(exc), receipt=receipt)
                readback = {"status_code": 404, "data": None}
            data = readback.get("data")
            original = receipt["output"].get("data")
            if isinstance(data, Mapping):
                for key, expected in params.items():
                    if key in data and data[key] != expected:
                        return _result("blocked", "目标业务结果与请求字段不一致：" + key, receipt=receipt)
                if isinstance(original, Mapping) and "id" in original and data.get("id") != original["id"]:
                    return _result("blocked", "目标业务结果 ID 与创建回执不一致", receipt=receipt)
            if spec.method == "GET" and isinstance(original, Mapping) and isinstance(data, Mapping):
                from app.services.agent_responses_service import redact_agent_model_context_value

                # 总数相等不能证明条目相等。快照有变化时保留原回执，明确要求重新读取。
                for key in ("items", "page", "page_size", "total"):
                    if key in original and (
                        key not in data
                        or redact_agent_model_context_value(data[key])
                        != redact_agent_model_context_value(original[key])
                    ):
                        return _result("blocked", "查询条目快照已变化，不能确认一致：" + key, receipt=receipt)
            if spec.method == "DELETE" and readback.get("status_code") != 404:
                return _result("blocked", "删除目标仍可读，业务结果尚未确认", receipt=receipt)
            verified["readback"] = {**redact_agent_model_context_value(readback), **readback_identity}
            evidence.append(
                {
                    "source": "site_same_target_readback",
                    "capability": read.code,
                    "status_code": readback.get("status_code"),
                }
            )
        elif spec.risk != "read":
            # 明确披露只有同步 API 回执，不能把无对应 GET 的命令伪装成页面状态验收。
            verified["verification_scope"] = "synchronous_api_receipt_only"
        _live_task(db, actor, team.id, task.id, lease_token)
        verified["page_action"] = capability_page_action(
            raw["scope"], spec.code, "completed", actor.id, receipt["output"]
        )
        receipts.append(verified)
        evidence.append({"source": "site_persisted_execution", "task_id": row.id, "request_id": receipt["request_id"]})
    final = _result("completed", "站点业务结果已独立核验", evidence=evidence)
    final["result_receipts"] = receipts
    if len(receipts) == 1:
        final["result_receipt"] = receipts[0]
    return final
