import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.enterprise_agent_teams import router as enterprise_agent_teams_router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.billing_ledger import (
    BillingOrder,
    BillingPaymentEvent,
    BillingRefund,
    BillingSubscription,
    BillingSubscriptionPeriod,
    BillingWallet,
)
from app.models.enterprise_agent_team import (
    EnterpriseAgentTeam,
    EnterpriseAgentTeamApproval,
    EnterpriseAgentTeamEvent,
    EnterpriseAgentTeamMember,
    EnterpriseAgentTeamTask,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import (
    Enterprise,
    EnterpriseGroup,
    EnterpriseGroupMember,
    EnterpriseMember,
)
from app.models.enterprise_project import EnterpriseProject, EnterpriseProjectGrant
from app.models.enterprise_security_artifact import EnterpriseSecurityArtifact
from app.models.project import Project
from app.models.user import User
from app.services import enterprise_agent_team_runtime_service as team_runtime
from app.services import enterprise_agent_team_service as teams
from app.services import enterprise_assistant_runtime_service as assistant_runtime
from app.services.deepseek_responses_runtime import ToolCall


@pytest.fixture
def teamdb():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Enterprise.__table__,
            EnterpriseMember.__table__,
            EnterpriseGroup.__table__,
            EnterpriseGroupMember.__table__,
            EnterpriseProject.__table__,
            EnterpriseProjectGrant.__table__,
            EnterpriseSecurityArtifact.__table__,
            Project.__table__,
            EnterpriseConversation.__table__,
            EnterpriseAssistantRun.__table__,
            EnterpriseChatMessage.__table__,
            EnterpriseAssistantRuntimeState.__table__,
            EnterpriseAgentTeam.__table__,
            EnterpriseAgentTeamApproval.__table__,
            EnterpriseAgentTeamEvent.__table__,
            EnterpriseAgentTeamMember.__table__,
            EnterpriseAgentTeamTask.__table__,
            BillingOrder.__table__,
            BillingPaymentEvent.__table__,
            BillingRefund.__table__,
            BillingSubscription.__table__,
            BillingSubscriptionPeriod.__table__,
            BillingWallet.__table__,
        ],
    )
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all(
        [
            User(id=1, username="owner", password="x", email="owner@example.test", role="user", status=1),
            User(id=2, username="leader", password="x", email="leader@example.test", role="user", status=1),
            User(id=3, username="member", password="x", email="member@example.test", role="user", status=1),
            User(id=4, username="outsider", password="x", email="outsider@example.test", role="user", status=1),
            User(id=5, username="other-owner", password="x", email="other@example.test", role="user", status=1),
            User(id=6, username="ordinary", password="x", email="ordinary@example.test", role="user", status=1),
            Enterprise(id=10, name="甲公司", owner_id=1, seat_limit=8, status="active", paid_order_id="order-a"),
            Enterprise(id=20, name="乙公司", owner_id=5, seat_limit=3, status="active", paid_order_id="order-b"),
            EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
            EnterpriseMember(enterprise_id=10, user_id=2, role="member", status="active"),
            EnterpriseMember(enterprise_id=10, user_id=3, role="member", status="active"),
            EnterpriseMember(enterprise_id=10, user_id=4, role="admin", status="active"),
            EnterpriseMember(enterprise_id=10, user_id=6, role="member", status="active"),
            EnterpriseMember(enterprise_id=20, user_id=5, role="owner", status="active"),
            EnterpriseGroup(id=100, enterprise_id=10, name="甲组", status="active"),
            EnterpriseGroup(id=200, enterprise_id=20, name="乙组", status="active"),
            EnterpriseGroupMember(enterprise_id=10, group_id=100, user_id=2, role="leader", status="active"),
            EnterpriseGroupMember(enterprise_id=10, group_id=100, user_id=3, role="member", status="active"),
        ]
    )
    session.flush()

    conversations = [
        EnterpriseConversation(id=1000, enterprise_id=10, kind="company", conversation_key="company", created_by=1),
        EnterpriseConversation(
            id=1001, enterprise_id=10, kind="group", conversation_key="group:100", group_id=100, created_by=2
        ),
        EnterpriseConversation(
            id=1002,
            enterprise_id=10,
            kind="direct",
            conversation_key="direct:2:3",
            participant_low_id=2,
            participant_high_id=3,
            created_by=2,
        ),
        EnterpriseConversation(id=2000, enterprise_id=20, kind="company", conversation_key="company", created_by=5),
    ]
    session.add_all(conversations)
    session.flush()

    for run_id, enterprise_id, conversation_id, initiator_id, source_message_id in (
        (1, 10, 1000, 2, 101),
        (2, 10, 1001, 3, 102),
        (3, 10, 1001, 2, 103),
        (4, 10, 1002, 3, 104),
        (5, 20, 2000, 5, 105),
    ):
        session.add(
            EnterpriseChatMessage(
                id=source_message_id,
                enterprise_id=enterprise_id,
                conversation_id=conversation_id,
                sender_id=initiator_id,
                sender_type="human",
                client_message_id=f"source-{source_message_id}",
                content="@小菱 执行团队任务",
            )
        )
        session.add(
            EnterpriseAssistantRun(
                id=run_id,
                public_id=f"run-{run_id}",
                request_key=f"request-{run_id}",
                enterprise_id=enterprise_id,
                conversation_id=conversation_id,
                initiator_user_id=initiator_id,
                source_message_id=source_message_id,
                prompt_sha256=str(run_id) * 64,
            )
        )
    session.flush()

    for team_id, run_id, enterprise_id, conversation_id, group_id, initiator_id, source_id in (
        (1, 1, 10, 1000, None, 2, 101),
        (2, 2, 10, 1001, 100, 3, 102),
        (3, 3, 10, 1001, 100, 2, 103),
        (4, 4, 10, 1002, None, 3, 104),
        (5, 5, 20, 2000, None, 5, 105),
    ):
        session.add(
            EnterpriseAgentTeam(
                id=team_id,
                assistant_run_id=run_id,
                enterprise_id=enterprise_id,
                conversation_id=conversation_id,
                group_id=group_id,
                initiator_user_id=initiator_id,
                source_message_id=source_id,
                source_tool_call_id=f"call-{team_id}",
                title=f"团队{team_id}",
                objective="只读检查",
                payer_owner_type="enterprise",
                payer_owner_id=enterprise_id,
                trace_id=f"trace-{team_id}",
                request_key_prefix=f"request-prefix-{team_id}",
                plan_sha256="a" * 64,
                plan_snapshot_json="{}",
                permission_snapshot_sha256="b" * 64,
                permission_snapshot_json="{}",
                result_json="{}",
                error_json="{}",
            )
        )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _assistant_team_tool(teamdb, monkeypatch, *, conversation_id, actor_id, team_id):
    executor = assistant_runtime.EnterpriseAssistantToolExecutor(
        teamdb, enterprise_id=10, conversation_id=conversation_id,
        run_public_id="test-run", lease_generation=1, actor_user_id=actor_id,
    )
    monkeypatch.setattr(executor, "_current", lambda: (SimpleNamespace(initiator_user_id=actor_id), {}))
    # These unit cases cover team visibility; model-send/tool-conflict clearance
    # has separate tests with persisted conversation snapshots.
    monkeypatch.setattr(executor, "_action_clarification", lambda *_args: None)
    return asyncio.run(executor.execute(ToolCall(
        call_id="read-team", name="get_enterprise_agent_team",
        arguments={"team_id": team_id}, raw_arguments="{}",
    )))


@pytest.mark.parametrize(("current_room", "team_id"), [(1001, 4), (1002, 2)])
def test_assistant_team_tool_cannot_cross_group_and_direct_rooms(teamdb, monkeypatch, current_room, team_id):
    assert teams.get_team_detail(teamdb, enterprise_id=10, user_id=3, team_id=team_id)["team_id"] == team_id
    result = _assistant_team_tool(teamdb, monkeypatch, conversation_id=current_room, actor_id=3, team_id=team_id)
    assert result.status == "error"
    assert result.failure_kind == "authorization"
    assert "只读检查" not in str(result)


def test_assistant_team_tool_cannot_cross_groups(teamdb, monkeypatch):
    teamdb.add_all([
        EnterpriseGroup(id=101, enterprise_id=10, name="另一组", status="active"),
        EnterpriseGroupMember(enterprise_id=10, group_id=101, user_id=3, role="member", status="active"),
        EnterpriseConversation(id=1003, enterprise_id=10, kind="group", conversation_key="group:101",
                               group_id=101, created_by=3),
    ])
    teamdb.commit()
    result = _assistant_team_tool(teamdb, monkeypatch, conversation_id=1003, actor_id=3, team_id=2)
    assert result.status == "error"
    assert result.failure_kind == "authorization"
    assert "只读检查" not in str(result)


@pytest.mark.parametrize(("conversation_id", "team_id"), [(1001, 2), (1002, 4)])
def test_assistant_team_tool_returns_only_protected_reference_for_same_room(
    teamdb, monkeypatch, conversation_id, team_id,
):
    team = teamdb.query(EnterpriseAgentTeam).filter_by(id=team_id).one()
    team.objective = "不应转发到群聊的团队结果"
    teamdb.commit()
    result = _assistant_team_tool(teamdb, monkeypatch, conversation_id=conversation_id, actor_id=3, team_id=team_id)
    assert result.status == "success"
    assert result.output == {"location": "请在当前会话点击「团队进度」查看"}
    assert "不应转发到群聊的团队结果" not in str(result.output)
    assert "objective" not in result.output
    assert "tasks" not in result.output
    assert "team_id" not in result.output


def test_assistant_team_output_stays_safe_for_future_group_members(teamdb, monkeypatch):
    team = teamdb.query(EnterpriseAgentTeam).filter_by(id=2).one()
    team.objective = "仅发起人和有权管理者可见"
    teamdb.commit()
    result = _assistant_team_tool(teamdb, monkeypatch, conversation_id=1001, actor_id=3, team_id=2)
    teamdb.add(EnterpriseGroupMember(enterprise_id=10, group_id=100, user_id=6, role="member", status="active"))
    teamdb.commit()
    with pytest.raises(NotFoundError):
        teams.get_team_detail(teamdb, enterprise_id=10, user_id=6, team_id=2)
    assert result.status == "success"
    assert "仅发起人和有权管理者可见" not in str(result.output)


def test_assistant_team_tool_denies_member_after_group_revocation(teamdb, monkeypatch):
    teamdb.query(EnterpriseGroupMember).filter_by(enterprise_id=10, group_id=100, user_id=3).one().status = "removed"
    teamdb.commit()
    result = _assistant_team_tool(teamdb, monkeypatch, conversation_id=1001, actor_id=3, team_id=2)
    assert result.status == "error"
    assert result.failure_kind == "authorization"


def test_assistant_team_tool_rechecks_project_grant(teamdb, monkeypatch):
    teamdb.add_all([
        EnterpriseGroup(id=300, enterprise_id=10, name="项目组", status="active"),
        Project(id=9001, user_id=1, project_name="受限项目", status="active"),
        EnterpriseProject(enterprise_id=10, project_id=9001, group_id=300, status="active"),
        EnterpriseProjectGrant(enterprise_id=10, project_id=9001, user_id=3, role="viewer", status="active"),
    ])
    teamdb.query(EnterpriseAgentTeam).filter_by(id=2).one().project_id = 9001
    teamdb.commit()
    allowed = _assistant_team_tool(teamdb, monkeypatch, conversation_id=1001, actor_id=3, team_id=2)
    assert allowed.status == "success"
    grant = teamdb.query(EnterpriseProjectGrant).filter_by(enterprise_id=10, project_id=9001, user_id=3).one()
    grant.status = "revoked"
    teamdb.commit()
    denied = _assistant_team_tool(teamdb, monkeypatch, conversation_id=1001, actor_id=3, team_id=2)
    assert denied.status == "error"
    assert denied.failure_kind == "authorization"


def test_group_leader_and_member_lists_stay_in_their_current_scope(teamdb):
    leader_items = teams.list_team_summaries(teamdb, enterprise_id=10, user_id=2)["items"]
    assert {item["team_id"] for item in leader_items} == {1, 2, 3}
    assert all(item["title"] is not None for item in leader_items)

    member_items = teams.list_team_summaries(teamdb, enterprise_id=10, user_id=3)["items"]
    assert {item["team_id"] for item in member_items} == {2, 4}

    with pytest.raises(ForbiddenError):
        teams.list_team_summaries(teamdb, enterprise_id=10, user_id=6, group_id=100)


def test_company_admin_gets_cross_group_summary_without_private_chat_details(teamdb):
    items = teams.list_team_summaries(teamdb, enterprise_id=10, user_id=1)["items"]
    by_id = {item["team_id"]: item for item in items}
    assert set(by_id) == {1, 2, 3, 4}
    assert by_id[1]["can_read_conversation"] is True
    assert by_id[1]["title"] == "团队1"
    assert by_id[2]["can_read_conversation"] is False
    assert by_id[2]["title"] is None
    assert by_id[4]["can_read_conversation"] is False
    assert by_id[4]["title"] is None
    with pytest.raises(ForbiddenError):
        teams.require_team_conversation_access(teamdb, enterprise_id=10, user_id=1, team_id=2)

    admin_items = teams.list_team_summaries(teamdb, enterprise_id=10, user_id=4)["items"]
    admin_by_id = {item["team_id"]: item for item in admin_items}
    assert set(admin_by_id) == {1, 2, 3, 4}
    assert admin_by_id[2]["title"] is None
    with pytest.raises(ForbiddenError):
        teams.require_team_conversation_access(teamdb, enterprise_id=10, user_id=4, team_id=2)


def test_team_id_cannot_cross_enterprise_or_chat_visibility(teamdb):
    with pytest.raises(ForbiddenError):
        teams.list_team_summaries(teamdb, enterprise_id=10, user_id=5)
    with pytest.raises(NotFoundError):
        teams.require_team_conversation_access(teamdb, enterprise_id=10, user_id=2, team_id=5)
    with pytest.raises(ForbiddenError):
        teams.require_team_conversation_access(teamdb, enterprise_id=10, user_id=1, team_id=4)


def test_team_detail_uses_the_same_initiator_and_group_leader_scope_as_listing(teamdb):
    leader_detail = teams.get_team_detail(teamdb, enterprise_id=10, user_id=2, team_id=2)
    assert leader_detail["title"] == "团队2"

    own_detail = teams.get_team_detail(teamdb, enterprise_id=10, user_id=3, team_id=2)
    assert own_detail["title"] == "团队2"

    # The member can read the group chat, but cannot fetch another member's result by guessing its ID.
    with pytest.raises(NotFoundError, match="团队不存在"):
        teams.get_team_detail(teamdb, enterprise_id=10, user_id=3, team_id=3)

    # A direct-chat participant likewise sees only their own Xiaoling team result.
    with pytest.raises(NotFoundError, match="团队不存在"):
        teams.get_team_detail(teamdb, enterprise_id=10, user_id=2, team_id=4)


def test_team_detail_rechecks_current_group_membership_and_role(teamdb):
    member = teamdb.query(EnterpriseGroupMember).filter_by(
        enterprise_id=10, group_id=100, user_id=3,
    ).one()
    member.status = "inactive"
    teamdb.commit()
    with pytest.raises(ForbiddenError, match="该小组的访问权限"):
        teams.get_team_detail(teamdb, enterprise_id=10, user_id=3, team_id=2)

    leader = teamdb.query(EnterpriseGroupMember).filter_by(
        enterprise_id=10, group_id=100, user_id=2,
    ).one()
    leader.role = "member"
    teamdb.commit()
    with pytest.raises(NotFoundError, match="团队不存在"):
        teams.get_team_detail(teamdb, enterprise_id=10, user_id=2, team_id=2)


def test_project_scoped_team_results_intersect_chat_and_current_project_view(teamdb, monkeypatch):
    team = teamdb.get(EnterpriseAgentTeam, 2)
    team.project_id = 9001
    plan = {"project_id": 9001}
    team.plan_snapshot_json = team_runtime._canonical_json(plan)
    team.plan_sha256 = team_runtime._digest(plan)
    readable_team = teamdb.get(EnterpriseAgentTeam, 3)
    readable_team.project_id = 9002
    readable_plan = {"project_id": 9002}
    readable_team.plan_snapshot_json = team_runtime._canonical_json(readable_plan)
    readable_team.plan_sha256 = team_runtime._digest(readable_plan)
    teamdb.add(EnterpriseGroup(id=300, enterprise_id=10, name="项目责任组", status="active"))
    teamdb.add(Project(id=9001, user_id=1, project_name="组外项目", status="active"))
    teamdb.add(Project(id=9002, user_id=1, project_name="本组项目", status="active"))
    teamdb.add(
        EnterpriseProject(id=9001, enterprise_id=10, project_id=9001, group_id=300, status="active")
    )
    teamdb.add(
        EnterpriseProject(id=9002, enterprise_id=10, project_id=9002, group_id=100, status="active")
    )
    teamdb.commit()

    def unexpected_per_project_lookup(*_args, **_kwargs):
        raise AssertionError("team listing must filter project visibility in SQL, not query each row")

    monkeypatch.setattr(
        "app.services.enterprise_project_policy.require_enterprise_project_access",
        unexpected_per_project_lookup,
    )
    visible = teams.list_team_summaries(
        teamdb,
        enterprise_id=10,
        user_id=2,
        conversation_id=1001,
    )["items"]
    assert {item["team_id"] for item in visible} == {3}
    monkeypatch.undo()
    assert teams.get_team_detail(teamdb, enterprise_id=10, user_id=2, team_id=3)["project_id"] == 9002
    with pytest.raises(NotFoundError, match="团队不存在"):
        teams.get_team_detail(teamdb, enterprise_id=10, user_id=2, team_id=2)


def _add_second_group_team(teamdb):
    group = EnterpriseGroup(id=300, enterprise_id=10, name="乙组", status="active")
    room = EnterpriseConversation(
        id=1003,
        enterprise_id=10,
        kind="group",
        conversation_key="group:300",
        group_id=300,
        created_by=3,
    )
    message = EnterpriseChatMessage(
        id=106,
        enterprise_id=10,
        conversation_id=1003,
        sender_id=3,
        sender_type="human",
        client_message_id="source-106",
        content="@小菱 执行只读任务",
    )
    run = EnterpriseAssistantRun(
        id=6,
        public_id="run-6",
        request_key="request-6",
        enterprise_id=10,
        conversation_id=1003,
        group_id=300,
        initiator_user_id=3,
        source_message_id=106,
        prompt_sha256="6" * 64,
        status="running",
        attempt_count=1,
    )
    plan = {"risk_level": "low", "requires_approval": False}
    permission = {"messages": []}
    capabilities = {"execution_mode": "model_only", "tools": [], "external_effects": False}
    state = EnterpriseAssistantRuntimeState(
        assistant_run_id=6,
        enterprise_id=10,
        conversation_id=1003,
        group_id=300,
        initiator_user_id=3,
        source_message_id=106,
        status="running",
        checkpoint_json="{}",
        permission_snapshot_json=team_runtime._canonical_json(permission),
        permission_snapshot_sha256=team_runtime._digest(permission),
        lease_generation=1,
        lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).replace(tzinfo=None),
        billing_invocation_count=1,
        version=1,
    )
    team = EnterpriseAgentTeam(
        id=6,
        assistant_run_id=6,
        enterprise_id=10,
        conversation_id=1003,
        group_id=300,
        initiator_user_id=3,
        source_message_id=106,
        source_tool_call_id="call-6",
        title="乙组任务",
        objective="仅乙组查看",
        status="running",
        payer_owner_type="enterprise",
        payer_owner_id=10,
        trace_id="trace-6",
        request_key_prefix="request-prefix-6",
        parent_lease_generation=1,
        lease_generation=1,
        plan_sha256=team_runtime._digest(plan),
        plan_snapshot_json=team_runtime._canonical_json(plan),
        permission_snapshot_sha256=team_runtime._digest(permission),
        permission_snapshot_json=team_runtime._canonical_json(permission),
        result_json="{}",
        error_json="{}",
    )
    member = EnterpriseAgentTeamMember(
        id=60,
        team_id=6,
        member_key="worker",
        display_name="代码审查员",
        address="agent:code_reviewer",
        kind="registered",
        role="worker",
        registered_agent_version="v1",
        snapshot_sha256=team_runtime._digest(capabilities),
        capabilities_json=team_runtime._canonical_json(capabilities),
    )
    task = EnterpriseAgentTeamTask(
        id=600,
        team_id=6,
        member_id=60,
        task_key="review",
        title="审查任务",
        instructions="仅用于权限测试",
        dependency_keys_json="[]",
        input_json="{}",
        status="retryable",
        attempt_count=1,
        max_attempts=3,
        result_json="{}",
        artifacts_json="[]",
        errors_json="[]",
    )
    teamdb.add_all(
        [
            group,
            room,
            message,
            run,
            state,
            EnterpriseGroupMember(enterprise_id=10, group_id=300, user_id=2, role="member", status="active"),
            EnterpriseGroupMember(enterprise_id=10, group_id=300, user_id=3, role="member", status="active"),
            EnterpriseGroupMember(enterprise_id=10, group_id=300, user_id=6, role="leader", status="active"),
            team,
            member,
            task,
        ]
    )
    teamdb.commit()


def test_leader_permissions_do_not_carry_between_groups_in_the_same_enterprise(teamdb):
    _add_second_group_team(teamdb)

    # User 2 leads group 100 but is an ordinary member of group 300.
    assert teams.list_team_summaries(
        teamdb,
        enterprise_id=10,
        user_id=2,
        conversation_id=1003,
        group_id=300,
    )["items"] == []
    with pytest.raises(NotFoundError, match="团队不存在"):
        teams.get_team_detail(teamdb, enterprise_id=10, user_id=2, team_id=6)

    # User 6 leads group 300 and may see another member's result in that group.
    assert {item["team_id"] for item in teams.list_team_summaries(
        teamdb,
        enterprise_id=10,
        user_id=6,
        conversation_id=1003,
        group_id=300,
    )["items"]} == {6}
    assert teams.get_team_detail(teamdb, enterprise_id=10, user_id=6, team_id=6)["title"] == "乙组任务"


def test_second_group_agent_team_http_visibility_retry_and_admin_cancel(teamdb, monkeypatch):
    from app.core.error_handlers import register_handlers
    from app.services import enterprise_agent_team_dispatcher
    from app.services import enterprise_assistant_runtime_service as parent_runtime

    _add_second_group_team(teamdb)
    app = FastAPI()
    app.include_router(enterprise_agent_teams_router)
    actor_id = {"value": 2}
    app.dependency_overrides[get_db] = lambda: teamdb
    app.dependency_overrides[get_current_user] = lambda: teamdb.get(User, actor_id["value"])
    register_handlers(app)
    monkeypatch.setattr(parent_runtime, "_live_role_snapshot", lambda *_args: {})
    monkeypatch.setattr("app.services.enterprise_assistant_service._require_subscription", lambda *_args: None)
    monkeypatch.setattr(
        "app.services.enterprise_assistant_service._require_active_limit", lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(team_runtime, "_attempt_reservations", lambda *_args: [])
    monkeypatch.setattr(
        enterprise_agent_team_dispatcher, "_publish_terminal_receipt", lambda *_args, **_kwargs: None,
    )

    with TestClient(app) as client:
        # The group A leader is only an ordinary member of group B.
        assert client.get("/enterprises/10/agent-teams/6").status_code == 404
        retry_a = client.post("/enterprises/10/agent-teams/6/retry", json={"task_id": 600})
        assert retry_a.status_code == 403

        # Enterprise admins see tenant-wide minimum metadata, but not the private group result.
        actor_id["value"] = 4
        listed = client.get("/enterprises/10/agent-teams")
        item = next(row for row in listed.json()["data"]["items"] if row["team_id"] == 6)
        assert item["title"] is None and item["can_read_conversation"] is False
        assert client.get("/enterprises/10/agent-teams/6").status_code == 403
        admin_retry = client.post("/enterprises/10/agent-teams/6/retry", json={"task_id": 600})
        assert admin_retry.status_code == 403

        # The current group B leader can retry; a tenant admin can stop the run without reading it.
        actor_id["value"] = 6
        mismatched_team = client.post("/enterprises/10/agent-teams/1/retry", json={"task_id": 600})
        assert mismatched_team.status_code == 404, mismatched_team.text
        assert teamdb.get(EnterpriseAgentTeamTask, 600).status == "retryable"
        retry_b = client.post("/enterprises/10/agent-teams/6/retry", json={"task_id": 600})
        assert retry_b.status_code == 200
        assert retry_b.json()["data"]["status"] == "queued"
        actor_id["value"] = 4
        cancelled = client.post(
            "/enterprises/10/agent-teams/6/cancel", json={"reason": "企业管理员停止异常团队"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["data"]["status"] == "cancelled"


def test_conversation_filter_requires_live_room_access_and_does_not_inherit_admin_rights(teamdb):
    leader_items = teams.list_team_summaries(
        teamdb,
        enterprise_id=10,
        user_id=2,
        conversation_id=1001,
    )["items"]
    assert {item["team_id"] for item in leader_items} == {2, 3}
    member_group_items = teams.list_team_summaries(
        teamdb,
        enterprise_id=10,
        user_id=3,
        conversation_id=1001,
    )["items"]
    assert {item["team_id"] for item in member_group_items} == {2}
    assert (
        teams.list_team_summaries(
            teamdb,
            enterprise_id=10,
            user_id=3,
            conversation_id=1000,
        )["items"]
        == []
    )
    member_direct_items = teams.list_team_summaries(
        teamdb,
        enterprise_id=10,
        user_id=3,
        conversation_id=1002,
    )["items"]
    assert {item["team_id"] for item in member_direct_items} == {4}
    with pytest.raises(ForbiddenError):
        teams.list_team_summaries(teamdb, enterprise_id=10, user_id=1, conversation_id=1001)
    with pytest.raises(ValidationError):
        teams.list_team_summaries(
            teamdb,
            enterprise_id=10,
            user_id=2,
            group_id=100,
            conversation_id=1000,
        )


def test_assistant_team_creation_is_idempotent_and_rejects_out_of_scope_input(teamdb, monkeypatch):
    from datetime import datetime, timedelta, timezone

    permission = {"messages": []}
    state = EnterpriseAssistantRuntimeState(
        assistant_run_id=1,
        enterprise_id=10,
        conversation_id=1000,
        group_id=None,
        initiator_user_id=2,
        source_message_id=101,
        status="running",
        checkpoint_json="{}",
        permission_snapshot_json=team_runtime._canonical_json(permission),
        permission_snapshot_sha256=team_runtime._digest(permission),
        stop_requested=False,
        lease_generation=1,
        lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).replace(
            tzinfo=None,
        ),
        billing_invocation_count=1,
        version=1,
    )
    run = teamdb.get(EnterpriseAssistantRun, 1)
    run.status = "running"
    teamdb.add(state)
    teamdb.flush()

    def contract_snapshot(code):
        return None, {
            "agent_code": code,
            "execution_mode": "model_only",
            "tools": [],
            "external_effects": False,
        }

    monkeypatch.setattr(teams, "_contract_snapshot", contract_snapshot)
    monkeypatch.setattr(teams, "_team_budget", lambda *_args, **_kwargs: 5000)
    payload = {
        "title": "代码双检",
        "objective": "检查给定片段并汇总风险",
        "members": [
            {"member_key": "review", "agent_code": "code_reviewer", "display_name": "代码审查"},
            {"member_key": "language", "agent_code": "language_detector", "display_name": "语言识别"},
        ],
        "tasks": [
            {
                "task_key": "review-task",
                "member_key": "review",
                "title": "质量检查",
                "instructions": "只分析输入代码",
                "input": {"code": "print(1)", "language": "python"},
            },
            {
                "task_key": "language-task",
                "member_key": "language",
                "title": "语言识别",
                "instructions": "根据用户提供的项目名称和描述判断主要语言",
                "input": {"project_name": "demo", "description": "Python 服务"},
            },
        ],
    }
    first = teams.create_team_from_assistant(
        teamdb,
        enterprise_id=10,
        user_id=2,
        state=state,
        call_id="call-team-idempotent",
        payload=payload,
    )
    replay = teams.create_team_from_assistant(
        teamdb,
        enterprise_id=10,
        user_id=2,
        state=state,
        call_id="call-team-idempotent",
        payload=payload,
    )
    assert first["status"] == "queued" and first["budget_cap_milli"] == 5000
    assert replay["team_id"] == first["team_id"] and replay["replayed"] is True
    assert teamdb.query(EnterpriseAgentTeam).filter_by(source_tool_call_id="call-team-idempotent").count() == 1
    assert teamdb.query(EnterpriseAgentTeamMember).filter_by(team_id=first["team_id"]).count() == 2
    assert teamdb.query(EnterpriseAgentTeamTask).filter_by(team_id=first["team_id"]).count() == 2

    changed_payload = {**payload, "title": "不同计划"}
    with pytest.raises(ConflictError, match="不能重放不同计划"):
        teams.create_team_from_assistant(
            teamdb,
            enterprise_id=10,
            user_id=2,
            state=state,
            call_id="call-team-idempotent",
            payload=changed_payload,
        )

    security_without_project = {
        "title": "安全扫描",
        "objective": "只扫描当前企业项目",
        "members": [
            {"member_key": "security", "agent_code": "security_sentinel", "display_name": "安全审计"},
            {"member_key": "language", "agent_code": "language_detector", "display_name": "语言识别"},
        ],
        "tasks": [
            {
                "task_key": "security-task",
                "member_key": "security",
                "title": "项目安全审计",
                "instructions": "只读审计服务端绑定的项目",
                "input": {"scan_mode": "static_full"},
            },
            {
                "task_key": "language-task",
                "member_key": "language",
                "title": "语言识别",
                "instructions": "识别项目语言",
                "input": {"project_name": "demo"},
            },
        ],
    }
    with pytest.raises(ValidationError, match="必须绑定已授权的企业项目范围"):
        teams.create_team_from_assistant(
            teamdb,
            enterprise_id=10,
            user_id=2,
            state=state,
            call_id="call-security-without-project",
            payload=security_without_project,
        )

    unsafe_payload = {
        **payload,
        "tasks": [
            {**payload["tasks"][0], "input": {"code": "print(1)", "project_id": 999}},
            payload["tasks"][1],
        ],
    }
    with pytest.raises(ValidationError, match="不接受项目 ID"):
        teams.create_team_from_assistant(
            teamdb,
            enterprise_id=10,
            user_id=2,
            state=state,
            call_id="call-team-unsafe",
            payload=unsafe_payload,
        )


def test_enterprise_team_security_sentinel_is_project_scoped_and_accepts_only_scan_options():
    _contract, snapshot = teams._contract_snapshot("security_sentinel")
    assert snapshot["agent_code"] == "security_sentinel"
    assert snapshot["tools"] == []
    assert snapshot["external_effects"] is False

    assert teams._validate_agent_input("security_sentinel", {}) == {}
    assert teams._validate_agent_input(
        "security_sentinel",
        {"scan_mode": "static_full", "top_n": 80, "trace_dataflow": False},
    ) == {"scan_mode": "static_full", "top_n": 80, "trace_dataflow": False}
    with pytest.raises(ValidationError, match="项目范围"):
        teams._require_project_scope_for_agents(None, {"security_sentinel"})
    with pytest.raises(ValidationError, match="不接受"):
        teams._validate_agent_input("security_sentinel", {"project_id": 9001})
    with pytest.raises(ValidationError, match="scan_mode"):
        teams._validate_agent_input("security_sentinel", {"scan_mode": "external"})
    with pytest.raises(ValidationError, match="scan_mode"):
        teams._validate_agent_input("security_sentinel", {"scan_mode": []})


def test_read_only_api_returns_only_role_scoped_team_summaries(teamdb):
    app = FastAPI()
    app.include_router(enterprise_agent_teams_router)
    actor_id = {"value": 2}
    app.dependency_overrides[get_db] = lambda: teamdb
    app.dependency_overrides[get_current_user] = lambda: teamdb.get(User, actor_id["value"])

    with TestClient(app) as client:
        response = client.get("/enterprises/10/agent-teams")
        assert response.status_code == 200
        body = response.json()["data"]
        assert {item["team_id"] for item in body["items"]} == {1, 2, 3}
        assert all("result_json" not in item and "permission_snapshot_json" not in item for item in body["items"])

        actor_id["value"] = 4
        admin_response = client.get("/enterprises/10/agent-teams?group_id=100")
        assert admin_response.status_code == 200
        admin_items = admin_response.json()["data"]["items"]
        assert {item["team_id"] for item in admin_items} == {2, 3}
        assert all(item["title"] is None and not item["can_read_conversation"] for item in admin_items)

        invalid_limit = client.get("/enterprises/10/agent-teams?limit=101")
        assert invalid_limit.status_code == 422


def test_team_detail_api_scopes_results_to_current_room_and_enterprise_membership(teamdb):
    from app.core.error_handlers import register_handlers

    app = FastAPI()
    app.include_router(enterprise_agent_teams_router)
    actor_id = {"value": 2}
    app.dependency_overrides[get_db] = lambda: teamdb
    app.dependency_overrides[get_current_user] = lambda: teamdb.get(User, actor_id["value"])
    register_handlers(app)

    with TestClient(app) as client:
        # A group leader can inspect every result in the leader's current room,
        # but private-chat participation alone does not reveal the other actor's result.
        assert client.get("/enterprises/10/agent-teams/2").status_code == 200
        assert client.get("/enterprises/10/agent-teams/4").status_code == 404
        assert client.get("/enterprises/10/agent-teams/5").status_code == 404

        actor_id["value"] = 3
        assert client.get("/enterprises/10/agent-teams/2").status_code == 200
        assert client.get("/enterprises/10/agent-teams/3").status_code == 404

        # Company-level roles may read the company room but do not bypass the
        # group-membership or direct-chat participant checks for private rooms.
        for company_admin_id in (1, 4):
            actor_id["value"] = company_admin_id
            assert client.get("/enterprises/10/agent-teams/1").status_code == 200
            assert client.get("/enterprises/10/agent-teams/2").status_code == 403
            assert client.get("/enterprises/10/agent-teams/4").status_code == 403

        actor_id["value"] = 5
        assert client.get("/enterprises/10/agent-teams/1").status_code == 403


def test_security_artifact_api_requires_team_result_and_project_view_scope(teamdb):
    import hashlib
    import json

    from sqlalchemy import event

    from app.core.error_handlers import register_handlers

    team = teamdb.get(EnterpriseAgentTeam, 2)
    team.project_id = 9001
    report = {"findings": [{"title": "授权后可读"}]}
    report_json = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    summary = {"summary": "项目报告摘要"}
    teamdb.add(Project(id=9001, user_id=1, project_name="团队报告项目", status="active"))
    teamdb.add(EnterpriseProject(id=9001, enterprise_id=10, project_id=9001, group_id=100, status="active"))
    teamdb.add(
        EnterpriseAgentTeamMember(
            id=801,
            team_id=2,
            member_key="security",
            display_name="安全审计",
            address="agent:security_sentinel",
            kind="registered",
            role="worker",
            snapshot_sha256="c" * 64,
            capabilities_json="{}",
            status="completed",
        )
    )
    teamdb.flush()
    task = EnterpriseAgentTeamTask(
        id=802,
        team_id=2,
        member_id=801,
        task_key="scan",
        title="项目扫描",
        instructions="只读扫描",
        dependency_keys_json="[]",
        input_json="{}",
        status="completed",
        priority=0,
        attempt_count=1,
        max_attempts=1,
        lease_generation=4,
        result_json=json.dumps(
            {"status": "completed", "security_artifact_id": 9001},
            separators=(",", ":"),
        ),
        artifacts_json="[]",
        errors_json="[]",
    )
    teamdb.add(task)
    teamdb.add(
        EnterpriseSecurityArtifact(
            id=9001,
            team_id=2,
            task_id=802,
            enterprise_id=10,
            project_id=9001,
            conversation_id=1001,
            attempt_no=1,
            lease_generation=4,
            audit_run_id="audit-9001",
            source_sha256="a" * 64,
            report_sha256=hashlib.sha256(report_json.encode("utf-8")).hexdigest(),
            coverage_status="complete",
            termination_reason="",
            summary_json=json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            report_json=report_json,
        )
    )
    teamdb.commit()

    app = FastAPI()
    app.include_router(enterprise_agent_teams_router)
    actor_id = {"value": 2}
    app.dependency_overrides[get_db] = lambda: teamdb
    app.dependency_overrides[get_current_user] = lambda: teamdb.get(User, actor_id["value"])
    register_handlers(app)
    path = "/enterprises/10/agent-teams/2/security-artifacts/9001"

    with TestClient(app) as client:
        statements = []

        def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            if "enterprise_security_artifact" in statement.lower():
                statements.append(statement)

        engine = teamdb.get_bind()
        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            head_response = client.head(path)
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)
        assert head_response.status_code == 204
        assert head_response.content == b""
        assert statements and all("report_json" not in statement.lower() for statement in statements)
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["data"]["report"] == report
        assert response.json()["data"]["summary"] == summary

        # A readable group conversation does not grant access to an artifact
        # whose project is assigned to a different group.
        binding = teamdb.query(EnterpriseProject).filter_by(enterprise_id=10, project_id=9001).one()
        binding.group_id = 300
        teamdb.add(EnterpriseGroup(id=300, enterprise_id=10, name="其他项目组", status="active"))
        teamdb.commit()
        assert client.head(path).status_code == 404
        assert client.get(path).status_code == 404

        binding.group_id = 100
        teamdb.commit()
        assert client.get("/enterprises/10/agent-teams/3/security-artifacts/9001").status_code == 404

        actor_id["value"] = 6
        assert client.head(path).status_code == 403
        assert client.get(path).status_code == 403
