"""复用页面桥的团队执行入口；不建立另一套浏览器、身份或动作账本。"""

from __future__ import annotations

import asyncio
import hmac
from time import monotonic
from types import SimpleNamespace

from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.exceptions import AppError, ConflictError
from app.models.agent_response_run import AgentResponseRun, response_run_stop_requested
from app.models.agent_team import AgentTeam, AgentTeamTask
from app.services import agent_browser_service, agent_team_service

_KEYS = {
    "inspect": set(),
    "navigate": {"route"},
    "scroll": {"direction"},
    "click": {"control_handle"},
    "fill": {"control_handle", "value"},
    "select": {"control_handle", "value"},
    "check": {"control_handle", "checked"},
}


def browser_contract(raw):
    allowed = {"operation", "action", "args"}
    if set(key for key in raw if not str(key).startswith("_")) != allowed:
        raise ValueError("页面任务只接受 operation、action、args，不接受身份、URL或审批覆盖")
    action, args = raw.get("action"), raw.get("args")
    if not isinstance(action, str) or action not in _KEYS or not isinstance(args, dict) or set(args) != _KEYS[action]:
        raise ValueError("页面动作参数不符合已登记契约")
    if any(not isinstance(value, str) for key, value in args.items() if key != "checked"):
        raise ValueError("页面动作文本参数类型无效")
    if action == "check" and not isinstance(args["checked"], bool):
        raise ValueError("checked 必须为布尔值")
    if action == "scroll" and args["direction"] not in {"up", "down"}:
        raise ValueError("滚动方向仅支持 up/down")
    if "value" in args and (len(args["value"]) > 20000 or agent_browser_service._SECRET_VALUE.search(args["value"])):
        raise ValueError("秘密或超长输入不可写入团队任务")
    return SimpleNamespace(
        code="browser." + action,
        page="current_page",
        risk=("read" if action in {"inspect", "navigate", "scroll"} else "high"),
    )


def require_browser_action_lease(db, row):
    try:
        _, team_id, task_id, attempt, lease_sha256 = row.call_id.split(":")
        # 与回收器保持 team -> task 锁序；直到 claim/ACK 提交前禁止换租约。
        team = db.query(AgentTeam).filter_by(id=int(team_id)).populate_existing().with_for_update().first()
        task = db.query(AgentTeamTask).filter_by(id=int(task_id)).populate_existing().with_for_update().first()
        raw = agent_team_service._unjson(task.input_json, {}) if task else {}
        if (
            team is None
            or task is None
            or task.team_id != team.id
            or team.user_id != row.user_id
            or team.session_key != row.session_key
            or team.surface != row.surface
            or team.status not in {"running", "verifying"}
            or task.status != "running"
            or task.attempt_count != int(attempt)
            or not task.lease_token
            or task.lease_token.startswith("cleanup_")
            or not hmac.compare_digest(lease_sha256, agent_browser_service._hash(task.lease_token))
            or not task.lease_expires_at
            or task.lease_expires_at <= agent_browser_service._now()
            or raw.get("operation") != "operate_current_page"
            or raw.get("action") != row.action
        ):
            raise ValueError("inactive task")
        parent = (
            db.query(AgentResponseRun)
            .filter_by(id=int(team.root_agent_run_id or 0))
            .populate_existing()
            .with_for_update()
            .first()
        )
        if (
            parent is None
            or parent.run_id != row.run_id
            or parent.user_id != row.user_id
            or parent.session_key != row.session_key
            or parent.surface != row.surface
            or parent.status not in agent_browser_service._ACTIVE
            or response_run_stop_requested(parent)
        ):
            raise ValueError("unbound run")
        # 行锁不能阻止时间流逝；等待父运行锁后必须重新检查同一任务租约。
        if task.lease_expires_at <= agent_browser_service._now():
            raise ValueError("lease expired while waiting for parent run")
    except (TypeError, ValueError, AttributeError) as exc:
        raise ConflictError("页面任务已停止、租约过期或所属运行不一致，禁止认领和回执") from exc


async def execute_browser_task(db, actor, team, task, member, raw, lease_token):
    from app.services.site_agent_service import _confirmed, _live_task, _result

    browser_contract(raw)
    parent = db.get(AgentResponseRun, int(team.root_agent_run_id or 0), populate_existing=True)
    if (
        parent is None
        or parent.user_id != actor.id
        or parent.session_key != team.session_key
        or parent.surface != team.surface
        or parent.status not in agent_browser_service._ACTIVE
        or response_run_stop_requested(parent)
    ):
        return _result("blocked", "页面任务未绑定当前账号的真实小菱运行")
    if raw["action"] in agent_browser_service._MUTATIONS and not _confirmed(db, team, task, member, raw):
        return _result("blocked", "页面控件交互缺少当前账号对同一任务的确认")
    run_id = parent.run_id
    call_id = f"team-browser:{team.id}:{task.id}:{task.attempt_count}:{agent_browser_service._hash(lease_token)}"
    try:
        _live_task(db, actor, team.id, task.id, lease_token)
        output = await agent_browser_service.request_action(
            db,
            actor,
            run_id=run_id,
            session_key=team.session_key,
            surface=team.surface,
            call_id=call_id,
            action=raw["action"],
            args=raw["args"],
            event_sink=None,
            approved=raw["action"] not in agent_browser_service._MUTATIONS or _confirmed(db, team, task, member, raw),
        )
        _live_task(db, actor, team.id, task.id, lease_token)
        agent_browser_service._owner_run(db, actor, run_id=run_id, session_key=team.session_key, surface=team.surface)
        status = "completed" if output.get("completed") is True else "blocked"
        return _result(
            status,
            (
                "页面动作已返回客户端观察；业务写入需独立回读"
                if status == "completed"
                else str(output.get("error") or "页面动作未确认完成")
            ),
            evidence=[{"source": "browser_action_ledger", "data": output}],
        )
    except (AppError, ValueError) as exc:
        db.rollback()
        return _result("blocked", str(exc))


def _expire_browser_team(db, user, team_id, reason):
    team = agent_team_service._team_or_raise(db, user, team_id, lock=True)
    agent_team_service._expire_team(db, team, reason=reason)
    db.commit()
    agent_team_service._cleanup_team_runtime_resources(db, team, reason=reason)
    return agent_team_service.get_team(db, user, team_id)


async def wait_for_browser_team(db, user, team_id):
    """父运行保持存活，返回最新终态；调度积压和断流均有界收敛。"""
    deadline = monotonic() + int(settings.agent_full_validation_wait_seconds)
    try:
        while True:
            # 结束 MySQL REPEATABLE READ 快照，读取其他 worker 的最新提交。
            db.commit()
            db.expire_all()
            state = agent_team_service.get_team(db, user, int(team_id))
            team = db.get(AgentTeam, int(team_id))
            parent = db.get(AgentResponseRun, int(team.root_agent_run_id or 0), populate_existing=True)
            if (
                parent is None
                or parent.user_id != user.id
                or parent.session_key != team.session_key
                or parent.surface != team.surface
                or response_run_stop_requested(parent)
                or parent.status not in agent_browser_service._ACTIVE
            ):
                if state["status"] in agent_team_service._TERMINAL_TEAM:
                    raise ConflictError(f"团队 {team_id} 所属运行已停止或失效，保留团队原终态，不再回传工具成功")
                return agent_team_service.cancel_team(
                    db,
                    user,
                    int(team_id),
                    reason="页面任务所属运行已停止或失效",
                    reason_code="browser_parent_inactive",
                    deliver_terminal_notification=False,
                )
            if state["status"] in agent_team_service._TERMINAL_TEAM:
                return state
            team_deadline = team.deadline_at
            if team_deadline is not None and team_deadline.replace(tzinfo=None) <= agent_browser_service._now():
                return _expire_browser_team(db, user, int(team_id), "deadline_reached")
            if monotonic() >= deadline:
                return _expire_browser_team(db, user, int(team_id), "browser_team_wait_timeout")
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        db.rollback()
        agent_team_service.cancel_team(
            db,
            user,
            int(team_id),
            reason="页面团队等待已中断",
            reason_code="browser_wait_interrupted",
            deliver_terminal_notification=False,
        )
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        try:
            _expire_browser_team(db, user, int(team_id), "browser_team_wait_read_failed")
        except SQLAlchemyError:
            db.rollback()
        raise ConflictError(f"团队 {team_id} 状态读取失败，页面执行未确认完成，请查看团队账本") from exc
