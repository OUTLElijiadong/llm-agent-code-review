"""同一主 Responses 调用中的团队结果不能重复触发助手回复。"""

import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import func

from app.models.agent_mesh import AgentMeshMessage
from app.models.agent_response_run import AgentResponseRun, AgentResponseTranscriptMessage, AgentToolExecution
from app.models.agent_team import AgentTeamMember, AgentTeamTask
from app.models.user import User
from app.services import agent_mesh_service, agent_team_service
from app.services.ai_usage_context import usage_context


def _team_with_parent_call(db, *, surface="user", session_key="session-team-dedup", parent_status="completed",
                           owner_id=7401, tool_owner_id=None, tool_session_key=None,
                           parent_surface=None, root_run_id=None, tool_result_team_id=None,
                           tool_status="success", tool_name="create_agent_team", tool_call_id=None,
                           include_primary_answer=True):
    owner = db.get(User, owner_id)
    if owner is None:
        owner = User(
            id=owner_id,
            username=f"team-dedup-{uuid4().hex[:10]}",
            password="x" * 60,
            role="user",
            status=1,
        )
        db.add(owner)
        db.flush()
    run_id = f"run-{uuid4().hex}"
    run = AgentResponseRun(
        run_id=run_id,
        user_id=owner_id,
        surface=parent_surface or surface,
        session_key=tool_session_key or session_key,
        status="running",
        checkpoint_json=json.dumps({"transcript": [], "rounds": 1}),
        version=1,
    )
    db.add(run)
    db.flush()
    if root_run_id is not None:
        run.root_agent_run_id = root_run_id
        db.flush()
    if parent_status == "completed" and include_primary_answer:
        position = db.query(func.max(AgentResponseTranscriptMessage.position)).filter(
            AgentResponseTranscriptMessage.user_id == owner_id,
            AgentResponseTranscriptMessage.surface == surface,
            AgentResponseTranscriptMessage.session_key == session_key,
        ).scalar() or 0
        primary_answer = {"role": "assistant", "content": "论坛只读查询已完成，核验了最新帖子。"}
        primary_answer_json = json.dumps(primary_answer, ensure_ascii=False)
        db.add(AgentResponseTranscriptMessage(
            user_id=owner_id,
            surface=surface,
            session_key=session_key,
            position=int(position) + 1,
            run_id=run_id,
            message_json=primary_answer_json,
            message_sha256=hashlib.sha256(primary_answer_json.encode("utf-8")).hexdigest(),
        ))
        db.flush()
    execution = AgentToolExecution(
        request_id=uuid4().hex * 2,
        run_id=run_id,
        call_id=tool_call_id if tool_call_id is not None else f"call_{uuid4().hex}",
        user_id=tool_owner_id if tool_owner_id is not None else owner_id,
        tool_name=tool_name,
        status="executing",
        arguments_json="{}",
    )
    db.add(execution)
    db.flush()

    from app.schemas.agent_team import AgentTeamCreateIn

    payload = AgentTeamCreateIn.model_validate({
        "surface": surface,
        "session_id": session_key,
        "title": "只读论坛查询",
        "objective": "读取最新帖子并核验结果",
        "trace_id": run_id,
        "members": [
            {"member_key": "reader", "display_name": "读取 Agent", "address": "agent:project_analyzer",
             "role": "worker"},
            {"member_key": "verifier", "display_name": "核验 Agent", "address": "agent:code_reviewer",
             "role": "verifier"},
        ],
        "tasks": [
            {"task_key": "read", "member_key": "reader", "title": "读取帖子", "instructions": "只读读取帖子"},
            {"task_key": "verify", "member_key": "verifier", "title": "核验帖子", "instructions": "核验结果",
             "depends_on": ["read"]},
        ],
    })
    with usage_context(owner_id, {
        "root_agent_run_id": int(run.id),
        "agent_run_id": int(run.id),
        "tool_execution_id": int(execution.id),
    }, db=db):
        created = agent_team_service.create_team_from_xiaoling(db, owner, payload)

    # 真实执行顺序：子团队在父运行活动时创建，父运行之后才结束。
    run.status = parent_status
    execution.status = tool_status
    execution.result_json = json.dumps({
        "status": tool_status,
        "output": {"team_id": int(tool_result_team_id or created["team_id"]), "completed": False},
    })
    db.commit()
    return owner, run, execution, created


def _complete_team(db, created):
    team_id = int(created["team_id"])
    first = agent_team_service.claim_next_task(db, team_id, lease_seconds=60)
    assert first is not None
    agent_team_service.complete_task(
        db, team_id, first["task_id"], lease_token=first["lease_token"],
        result={"status": "completed", "summary": "读取到帖子 44、43、42", "evidence": [{"title": "帖子列表"}]},
    )
    second = agent_team_service.claim_next_task(db, team_id, lease_seconds=60)
    assert second is not None
    result = agent_team_service.complete_task(
        db, team_id, second["task_id"], lease_token=second["lease_token"],
        result={"status": "completed", "summary": "核验帖子 44、43、42"},
    )
    return result


def _fail_team(db, created):
    first = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert first is not None
    return agent_team_service.complete_task(
        db,
        created["team_id"],
        first["task_id"],
        lease_token=first["lease_token"],
        result={"status": "failed", "summary": "首轮读取失败", "retryable": False},
        success=False,
        error="首轮读取失败",
    )


def _retry_failed_team(db, owner, created):
    return agent_team_service.retry_team(
        db,
        owner,
        created["team_id"],
        task_keys=["read"],
        strategy_changes={"read": "先重新确认帖子列表可读，再执行只读核验"},
    )


@pytest.mark.parametrize("round_no", [1, 2, 3])
def test_team_results_are_delivered_once_as_a_terminal_summary(db, round_no):
    owner, _run, execution, created = _team_with_parent_call(
        db, session_key=f"session-team-dedup-{round_no}",
    )
    final = _complete_team(db, created)

    assert final["status"] == "completed"
    rows = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == f"session:user:session-team-dedup-{round_no}",
    ).all()
    task_results = [
        row for row in rows
        if row.message_type == "task.result" and json.loads(row.context_json).get("task_id")
    ]
    assert len(task_results) == 2
    assert all(row.status == "completed" for row in task_results)
    assert all(row.tool_execution_id == execution.id for row in task_results)
    assert all(row.agent_team_id == created["team_id"] for row in task_results)
    assert all(json.loads(row.context_json)["session_delivery"] is False for row in task_results)
    assert all(
        json.loads(row.context_json)["suppression_reason"] == "team_terminal_aggregate"
        for row in task_results
    )
    assert all("suppression_reason" not in json.loads(row.payload_json) for row in task_results)

    terminal = [row for row in rows if json.loads(row.context_json).get("team_terminal") is True]
    assert len(terminal) == 1
    assert terminal[0].message_type == "task.result"
    assert terminal[0].status == "queued"
    assert terminal[0].agent_team_id == created["team_id"]
    assert terminal[0].tool_execution_id == execution.id
    terminal_payload = json.loads(terminal[0].payload_json)
    assert terminal_payload["team_id"] == created["team_id"]
    assert terminal_payload["status"] == "completed"
    assert terminal_payload["summary"]["final_result"]["summary"] == "核验帖子 44、43、42"
    assert terminal_payload["result_reference"] == {
        "tool": "get_agent_team", "team_id": created["team_id"],
    }
    assert sum(row.status in {"queued", "delivered"} for row in rows) == 1
    if round_no == 1:
        _message, system_input = agent_mesh_service.prepare_message_run(
            db,
            owner,
            terminal[0].message_id,
            surface="user",
            session_key=f"session-team-dedup-{round_no}",
        )
        assert "团队终态汇总回执" in system_input["content"]
        assert f'"team_id":{created["team_id"]}' in system_input["content"]
        assert "服务端已完成真实账本读取" in system_input["content"]
        assert "不要重建团队" in system_input["content"]
        assert "verified_team_snapshot" in system_input["content"]
        assert "核验帖子 44、43、42" in system_input["content"]

    team_detail = agent_team_service.get_team(db, owner, created["team_id"])
    history_results = [message for message in team_detail["messages"] if message["message_type"] == "task.result"]
    individual_results = [message for message in history_results if message["context"].get("task_id")]
    assert len(individual_results) == 2
    assert all(message["status"] == "completed" for message in individual_results)
    assert all(
        message["context"]["suppression_reason"] == "team_terminal_aggregate"
        for message in individual_results
    )
    assert sum(message["context"].get("team_terminal") is True for message in history_results) == 1
    assert [task["status"] for task in team_detail["tasks"]] == ["completed", "completed"]


def test_terminal_receipt_from_cancelled_parent_is_acknowledged_without_starting_model(db):
    owner, parent_run, _execution, created = _team_with_parent_call(
        db,
        session_key="session-team-parent-cancelled",
        parent_status="cancelled",
    )
    _complete_team(db, created)
    receipt = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-parent-cancelled",
        AgentMeshMessage.context_json.contains('"team_terminal"'),
    ).one()
    assert json.loads(receipt.context_json)["parent_run_id"] == parent_run.run_id

    with pytest.raises(agent_mesh_service.AgentMeshStateError, match="父级小菱响应已停止"):
        agent_mesh_service.prepare_message_run(
            db,
            owner,
            receipt.message_id,
            surface="user",
            session_key="session-team-parent-cancelled",
        )

    db.refresh(receipt)
    assert receipt.status == "completed"
    assert db.query(AgentResponseRun).filter(
        AgentResponseRun.user_id == owner.id,
        AgentResponseRun.mesh_message_id == receipt.message_id,
    ).count() == 0


@pytest.mark.parametrize("parent_status", ["running", "waiting_approval", "waiting_input", "failed"])
def test_partial_team_does_not_deliver_individual_success_before_terminal_state(db, parent_status):
    owner, run, _execution, created = _team_with_parent_call(
        db, session_key=f"session-team-dedup-{parent_status}", parent_status=parent_status,
    )
    first = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert first is not None

    agent_team_service.complete_task(
        db, created["team_id"], first["task_id"], lease_token=first["lease_token"],
        result={"status": "completed", "summary": "第一阶段结果"},
    )

    row = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == f"session:user:session-team-dedup-{parent_status}",
        AgentMeshMessage.message_type == "task.result",
        AgentMeshMessage.context_json.contains('"task_id"'),
    ).one()
    assert row.status == "completed"
    assert row.tool_execution_id is not None
    assert json.loads(row.context_json)["session_delivery"] is False
    assert db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == f"session:user:session-team-dedup-{parent_status}",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).count() == 0

    second = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert second is not None
    agent_team_service.complete_task(
        db, created["team_id"], second["task_id"], lease_token=second["lease_token"],
        result={"status": "completed", "summary": "汇总完成"},
    )
    terminal = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == f"session:user:session-team-dedup-{parent_status}",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()
    assert terminal.status == "queued"


def test_failed_team_delivers_one_terminal_error_and_keeps_task_history(db):
    owner, _run, _execution, created = _team_with_parent_call(
        db, session_key="session-team-dedup-failure",
    )
    first = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert first is not None

    agent_team_service.complete_task(
        db, created["team_id"], first["task_id"], lease_token=first["lease_token"],
        result={"status": "failed", "summary": "上游读取失败", "retryable": False},
        success=False,
        error="上游读取失败",
    )

    row = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-failure",
        AgentMeshMessage.message_type == "task.error",
        AgentMeshMessage.context_json.contains('"team_terminal"'),
    ).one()
    assert row.status == "queued"
    assert json.loads(row.context_json)["team_terminal"] is True
    assert "上游读取失败" in row.payload_json
    individual_errors = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-failure",
        AgentMeshMessage.message_type == "task.error",
        AgentMeshMessage.context_json.contains('"task_id"'),
    ).all()
    assert len(individual_errors) == 1
    assert individual_errors[0].status == "failed"
    assert json.loads(individual_errors[0].context_json)["session_delivery"] is False


@pytest.mark.parametrize(
    "scope_override",
    [
        {"tool_owner_id": 7402},
        {"tool_session_key": "another-session"},
    ],
    ids=["tool-owner-mismatch", "parent-session-mismatch"],
)
def test_parent_scope_mismatch_does_not_suppress_terminal_team_result(db, scope_override):
    owner, _run, execution, created = _team_with_parent_call(
        db, session_key="session-team-dedup-scope", **scope_override,
    )
    final = _complete_team(db, created)

    assert final["status"] == "completed"
    results = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-scope",
        AgentMeshMessage.message_type == "task.result",
        AgentMeshMessage.agent_team_id == created["team_id"],
        AgentMeshMessage.context_json.contains('"task_id"'),
    ).all()
    assert len(results) == 2
    assert all(row.status == "completed" for row in results)
    assert all(row.tool_execution_id == execution.id for row in results)
    terminal = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-scope",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()
    assert terminal.status == "queued"
    assert terminal.agent_team_id == created["team_id"]


def test_distinct_completed_team_calls_keep_separate_terminal_notifications(db):
    first_owner, _first_run, first_exec, first_team = _team_with_parent_call(
        db, session_key="session-team-dedup-independent",
    )
    second_owner, _second_run, second_exec, second_team = _team_with_parent_call(
        db, owner_id=first_owner.id, session_key="session-team-dedup-independent",
    )
    assert second_owner.id == first_owner.id
    _complete_team(db, first_team)
    _complete_team(db, second_team)

    rows = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == first_owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-independent",
        AgentMeshMessage.message_type == "task.result",
    ).all()
    terminal = [row for row in rows if json.loads(row.context_json).get("team_terminal") is True]
    task_results = [row for row in rows if json.loads(row.context_json).get("task_id")]
    assert len(terminal) == 2
    assert len(task_results) == 4
    assert {row.agent_team_id for row in terminal} == {first_team["team_id"], second_team["team_id"]}
    assert {row.tool_execution_id for row in terminal} == {first_exec.id, second_exec.id}
    assert all(row.status == "queued" for row in terminal)
    assert all(row.status == "completed" for row in task_results)


@pytest.mark.parametrize(
    "scope_override",
    [
        {"parent_surface": "admin"},
        {"root_run_id": 999999},
        {"tool_result_team_id": 999999},
        {"tool_status": "executing"},
        {"tool_name": "other_tool"},
        {"tool_call_id": ""},
    ],
    ids=[
        "surface-mismatch", "root-run-mismatch", "tool-result-other-team", "tool-still-running",
        "wrong-tool", "missing-call-id",
    ],
)
def test_parent_call_identity_does_not_affect_team_result_delivery(db, scope_override):
    owner, _run, execution, created = _team_with_parent_call(
        db, session_key="session-team-dedup-call-scope", **scope_override,
    )
    _complete_team(db, created)

    results = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-call-scope",
        AgentMeshMessage.message_type == "task.result",
        AgentMeshMessage.agent_team_id == created["team_id"],
        AgentMeshMessage.context_json.contains('"task_id"'),
    ).all()
    assert len(results) == 2
    assert all(row.status == "completed" for row in results)
    assert all(row.tool_execution_id == execution.id for row in results)
    terminal = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-call-scope",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()
    assert terminal.status == "queued"


def test_missing_team_tool_attribution_still_delivers_terminal_team_result(db):
    owner, _run, _execution, created = _team_with_parent_call(
        db, session_key="session-team-dedup-missing-attribution",
    )
    team = db.get(agent_team_service.AgentTeam, created["team_id"])
    team.tool_execution_id = None
    db.commit()
    _complete_team(db, created)

    results = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-missing-attribution",
        AgentMeshMessage.message_type == "task.result",
        AgentMeshMessage.agent_team_id == created["team_id"],
        AgentMeshMessage.context_json.contains('"task_id"'),
    ).all()
    assert len(results) == 2
    assert all(row.status == "completed" for row in results)
    assert all(row.tool_execution_id is None for row in results)
    terminal = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-missing-attribution",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()
    assert terminal.status == "queued"


def test_completed_run_without_visible_assistant_answer_still_gets_terminal_result(db):
    owner, _run, _execution, created = _team_with_parent_call(
        db,
        session_key="session-team-dedup-no-visible-answer",
        include_primary_answer=False,
    )
    _complete_team(db, created)

    results = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-no-visible-answer",
        AgentMeshMessage.message_type == "task.result",
        AgentMeshMessage.agent_team_id == created["team_id"],
        AgentMeshMessage.context_json.contains('"task_id"'),
    ).all()
    assert len(results) == 2
    assert all(row.status == "completed" for row in results)
    assert all(json.loads(row.context_json)["session_delivery"] is False for row in results)
    terminal = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-no-visible-answer",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()
    assert terminal.status == "queued"


def test_expired_team_emits_one_terminal_error_and_preserves_completed_task_results(db):
    owner, _run, _execution, created = _team_with_parent_call(
        db, session_key="session-team-dedup-expired",
    )
    first = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert first is not None
    agent_team_service.complete_task(
        db, created["team_id"], first["task_id"], lease_token=first["lease_token"],
        result={"status": "completed", "summary": "已完成第一阶段"},
    )
    team = db.get(agent_team_service.AgentTeam, created["team_id"])
    agent_team_service._expire_team(db, team, reason="test_deadline")
    db.commit()

    terminal = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-expired",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()
    assert terminal.message_type == "task.error"
    assert terminal.status == "queued"
    completed_task_result = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-expired",
        AgentMeshMessage.message_type == "task.result",
        AgentMeshMessage.context_json.contains('"task_id"'),
    ).one()
    assert completed_task_result.status == "completed"
    agent_team_service._persist_terminal_team_notification(db, team, now=team.completed_at)
    db.commit()
    assert db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-expired",
        AgentMeshMessage.context_json.contains('"team_terminal"'),
    ).count() == 1


def test_cancelled_team_emits_one_verified_terminal_notice_with_reason(db):
    owner, _run, _execution, created = _team_with_parent_call(
        db, session_key="session-team-dedup-cancelled",
    )
    agent_team_service.cancel_team(db, owner, created["team_id"], reason="用户主动停止")
    agent_team_service.cancel_team(db, owner, created["team_id"], reason="重复取消")

    terminal = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-dedup-cancelled",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()
    assert terminal.message_type == "task.error"
    assert terminal.status == "queued"
    assert json.loads(terminal.payload_json)["status"] == "cancelled"

    _row, system_input = agent_mesh_service.prepare_message_run(
        db, owner, terminal.message_id, surface="user",
        session_key="session-team-dedup-cancelled",
    )
    assert '"status":"cancelled"' in system_input["content"]
    assert '"error_reason":"user_cancelled"' in system_input["content"]


def test_terminal_team_error_reason_is_preserved_in_compact_model_context(db):
    owner, _run, _execution, created = _team_with_parent_call(
        db, session_key="session-team-terminal-error-reason",
    )
    team = db.get(agent_team_service.AgentTeam, created["team_id"])
    agent_team_service._expire_team(db, team, reason="deadline_reached")
    db.commit()
    terminal = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-terminal-error-reason",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()

    _row, system_input = agent_mesh_service.prepare_message_run(
        db, owner, terminal.message_id, surface="user",
        session_key="session-team-terminal-error-reason",
    )

    assert '"error_reason":"deadline_reached"' in system_input["content"]


@pytest.mark.parametrize("consume_previous_notification", [False, True], ids=["pending", "consumed"])
def test_failed_team_retry_success_emits_new_success_notification(db, consume_previous_notification):
    owner, _run, _execution, created = _team_with_parent_call(
        db, session_key=f"session-team-retry-success-{consume_previous_notification}",
    )
    _fail_team(db, created)
    previous = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == f"session:user:session-team-retry-success-{consume_previous_notification}",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()
    assert previous.message_type == "task.error"
    if consume_previous_notification:
        agent_mesh_service.prepare_message_run(
            db, owner, previous.message_id, surface="user",
            session_key=f"session-team-retry-success-{consume_previous_notification}",
        )
        agent_mesh_service.finish_message_run(
            db, owner, previous.message_id, surface="user",
            session_key=f"session-team-retry-success-{consume_previous_notification}",
            success=True, summary="首轮失败已汇报",
        )

    _retry_failed_team(db, owner, created)
    if not consume_previous_notification:
        db.refresh(previous)
        assert previous.status == "completed"
        assert json.loads(previous.context_json)["superseded_by_generation"] == 1

    first = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert first is not None and first["task_key"] == "read"
    agent_team_service.complete_task(
        db, created["team_id"], first["task_id"], lease_token=first["lease_token"],
        result={"status": "completed", "summary": "重试读取成功"},
    )
    second = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert second is not None and second["task_key"] == "verify"
    agent_team_service.complete_task(
        db, created["team_id"], second["task_id"], lease_token=second["lease_token"],
        result={"status": "completed", "summary": "重试核验通过"},
    )

    rows = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == f"session:user:session-team-retry-success-{consume_previous_notification}",
    ).all()
    terminal = [row for row in rows if json.loads(row.context_json).get("team_terminal") is True]
    assert len(terminal) == 2
    assert previous.message_type == "task.error"
    success = next(row for row in terminal if json.loads(row.context_json)["execution_generation"] == 1)
    assert success.message_type == "task.result"
    assert success.status == "queued"
    assert json.loads(success.payload_json)["status"] == "completed"
    assert len({row.message_id for row in terminal}) == 2


def test_failed_team_retry_failure_keeps_each_generation_separate(db):
    owner, _run, _execution, created = _team_with_parent_call(
        db, session_key="session-team-retry-failure",
    )
    _fail_team(db, created)
    _retry_failed_team(db, owner, created)
    retried = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert retried is not None and retried["task_key"] == "read"
    agent_team_service.complete_task(
        db, created["team_id"], retried["task_id"], lease_token=retried["lease_token"],
        result={"status": "failed", "summary": "重试仍失败", "retryable": False},
        success=False, error="重试仍失败",
    )

    terminal = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-retry-failure",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).all()
    assert len(terminal) == 2
    assert {json.loads(row.context_json)["execution_generation"] for row in terminal} == {0, 1}
    assert all(row.message_type == "task.error" for row in terminal)
    assert sum(row.status == "queued" for row in terminal) == 1


def test_retry_is_rejected_while_previous_terminal_reply_is_processing(db):
    owner, _run, _execution, created = _team_with_parent_call(
        db, session_key="session-team-retry-processing",
    )
    _fail_team(db, created)
    previous = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-retry-processing",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()
    agent_mesh_service.prepare_message_run(
        db, owner, previous.message_id, surface="user", session_key="session-team-retry-processing",
    )
    with pytest.raises(agent_team_service.AgentTeamStateError, match="正在由小菱处理"):
        _retry_failed_team(db, owner, created)
    assert db.get(agent_team_service.AgentTeam, created["team_id"]).status == "failed"


@pytest.mark.parametrize("tamper", ["correlation", "context_generation", "team_status"])
def test_forged_or_stale_terminal_message_is_completed_without_model_dispatch(db, tamper):
    owner, _run, _execution, created = _team_with_parent_call(
        db, session_key=f"session-team-forged-{tamper}",
    )
    _complete_team(db, created)
    row = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == f"session:user:session-team-forged-{tamper}",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()
    if tamper == "correlation":
        row.correlation_id = "team:999999"
    elif tamper == "context_generation":
        context = json.loads(row.context_json)
        context["execution_generation"] = 77
        row.context_json = json.dumps(context)
    else:
        team = db.get(agent_team_service.AgentTeam, created["team_id"])
        team.status = "queued"
    db.commit()
    with pytest.raises(agent_mesh_service.AgentMeshStateError, match="过期或无效"):
        agent_mesh_service.prepare_message_run(
            db, owner, row.message_id, surface="user", session_key=f"session-team-forged-{tamper}",
        )
    assert db.get(AgentMeshMessage, row.id).status == "completed"


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_terminal_notification_payload_stays_bounded_for_maximum_task_count(db, terminal_status):
    owner, _run, _execution, created = _team_with_parent_call(
        db, session_key="session-team-terminal-budget",
    )
    team = db.get(agent_team_service.AgentTeam, created["team_id"])
    verifier = db.query(AgentTeamMember).filter_by(team_id=team.id, role="verifier").one()
    now = agent_team_service._now()
    task_status = "completed" if terminal_status == "completed" else "failed"
    task_errors_json = "[]" if task_status == "completed" else json.dumps(
        [{"code": "worker_error", "message": "请求失败" * 120}], ensure_ascii=False,
    )
    for task in db.query(AgentTeamTask).filter_by(team_id=team.id).all():
        task.status = task_status
        task.completed_at = now
        task.errors_json = task_errors_json
        task.result_json = json.dumps({"summary": "任务完成" * 400}, ensure_ascii=False)
    for index in range(98):
        db.add(AgentTeamTask(
            team_id=team.id,
            member_id=verifier.id,
            task_key=f"extra-{index:03d}",
            title=f"附加任务 {index}",
            instructions="只读验证",
            dependency_keys_json="[]",
            input_json="{}",
            status=task_status,
            attempt_count=1,
            max_attempts=1,
            result_json=json.dumps({"summary": "任务完成" * 400}, ensure_ascii=False),
            artifacts_json="[]",
            errors_json=task_errors_json,
            completed_at=now,
        ))
    team.status = terminal_status
    team.completed_at = now
    team.summary_json = json.dumps({"verification_results": [{"summary": "核验完成" * 500}]}, ensure_ascii=False)
    db.flush()
    agent_team_service._persist_terminal_team_notification(db, team, now=now)
    db.commit()

    row = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == owner.id,
        AgentMeshMessage.send_to == "session:user:session-team-terminal-budget",
    ).filter(AgentMeshMessage.context_json.contains('"team_terminal"')).one()
    payload = json.loads(row.payload_json)
    assert len(row.payload_json.encode("utf-8")) <= 24 * 1024
    assert payload["task_counts"]["total"] == 100
    assert payload["status"] == terminal_status
    assert len(payload["task_statuses"]) == 20
    assert payload["task_statuses"][-1]["omitted_task_count"] == 81
    if terminal_status == "failed":
        assert len(payload["failed_task_samples"]) == 12
        assert payload["omitted_failed_task_count"] == 88
    assert payload["result_reference"] == {"tool": "get_agent_team", "team_id": created["team_id"]}
    _message, system_input = agent_mesh_service.prepare_message_run(
        db, owner, row.message_id, surface="user", session_key="session-team-terminal-budget",
    )
    assert len(system_input["content"].encode("utf-8")) <= 24 * 1024
    assert system_input["content"].count('"attempt_count"') == 100
    if terminal_status == "failed":
        assert system_input["content"].count('"error_previews"') == 6
        assert "请求失败" in system_input["content"]
