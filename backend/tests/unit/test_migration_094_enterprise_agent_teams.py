import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def _migration_module():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/094_enterprise_agent_teams.py"
    spec = importlib.util.spec_from_file_location("migration_094_enterprise_agent_teams", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_parent_stubs(connection):
    connection.exec_driver_sql(
        "CREATE TABLE enterprise_conversation (id INTEGER PRIMARY KEY, enterprise_id INTEGER NOT NULL, "
        "group_id INTEGER, UNIQUE (enterprise_id, id), UNIQUE (enterprise_id, id, group_id))"
    )
    connection.exec_driver_sql(
        "CREATE TABLE enterprise_assistant_run (id INTEGER PRIMARY KEY, enterprise_id INTEGER NOT NULL, "
        "conversation_id INTEGER NOT NULL, group_id INTEGER, initiator_user_id INTEGER NOT NULL, "
        "source_message_id INTEGER NOT NULL, "
        "UNIQUE (id, enterprise_id, conversation_id, initiator_user_id, source_message_id), "
        "UNIQUE (id, enterprise_id, conversation_id, group_id, initiator_user_id, source_message_id))"
    )
    connection.exec_driver_sql(
        "CREATE TABLE enterprise_chat_message (id INTEGER PRIMARY KEY, enterprise_id INTEGER NOT NULL, "
        "conversation_id INTEGER NOT NULL, sender_id INTEGER, "
        "UNIQUE (id, enterprise_id, conversation_id, sender_id))"
    )
    connection.exec_driver_sql(
        "INSERT INTO enterprise_conversation (id, enterprise_id, group_id) "
        "VALUES (80, 70, 90), (81, 71, 91), (82, 72, NULL)"
    )
    connection.exec_driver_sql(
        "INSERT INTO enterprise_assistant_run "
        "(id, enterprise_id, conversation_id, group_id, initiator_user_id, source_message_id) "
        "VALUES (10, 70, 80, 90, 110, 20), (11, 71, 81, 91, 111, 21), "
        "(12, 70, 80, 90, 110, 21), (13, 72, 82, NULL, 112, 22), (14, 70, 80, 91, 110, 20)"
    )
    connection.exec_driver_sql(
        "INSERT INTO enterprise_chat_message (id, enterprise_id, conversation_id, sender_id) "
        "VALUES (20, 70, 80, 110), (21, 71, 81, 111), (22, 72, 82, 112)"
    )


def test_094_creates_tenant_scoped_team_tables_and_enforces_payer_and_idempotency():
    module = _migration_module()
    assert module.revision == "094_enterprise_agent_teams"
    assert len(module.revision) <= 32
    assert module.down_revision == "093_billing_provider_identity"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        _create_parent_stubs(connection)
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()

        expected_tables = {
            "enterprise_agent_team", "enterprise_agent_team_member", "enterprise_agent_team_task",
            "enterprise_agent_team_approval", "enterprise_agent_team_event",
        }
        assert expected_tables.issubset(set(inspect(connection).get_table_names()))
        assert {
            item["name"] for item in inspect(connection).get_unique_constraints("enterprise_agent_team")
        } == {
            "uq_ent_agent_team_tool_call", "uq_ent_agent_team_trace", "uq_ent_agent_team_request_prefix",
            "uq_ent_agent_team_scope_ref", "uq_ent_agent_team_scope_group",
        }
        indexes = {item["name"] for item in inspect(connection).get_indexes("enterprise_agent_team")}
        assert {
            "ix_ent_agent_team_scope_status", "ix_ent_agent_team_group_status",
            "ix_ent_agent_team_initiator", "ix_ent_agent_team_lease",
        }.issubset(indexes)

        values = {
            "id": 1, "assistant_run_id": 10, "enterprise_id": 70, "conversation_id": 80,
            "group_id": 90, "initiator_user_id": 110, "source_message_id": 20,
            "source_tool_call_id": "call_enterprise_team_1", "title": "安全审查",
            "objective": "检查项目变更", "payer_owner_type": "enterprise", "payer_owner_id": 70,
            "trace_id": "enterprise_team_1", "request_key_prefix": "ent-team:1",
            "plan_sha256": "a" * 64, "plan_snapshot_json": "{}",
            "permission_snapshot_sha256": "b" * 64, "permission_snapshot_json": "{}",
            "result_json": "{}", "error_json": "{}", "create_time": "2026-11-06 00:00:00",
            "update_time": "2026-11-06 00:00:00",
        }
        columns = ", ".join(values)
        binds = ", ".join(f":{key}" for key in values)
        connection.execute(text(f"INSERT INTO enterprise_agent_team ({columns}) VALUES ({binds})"), values)

        duplicate = {**values, "id": 2, "trace_id": "enterprise_team_2",
                     "request_key_prefix": "ent-team:2"}
        with pytest.raises(IntegrityError):
            connection.execute(text(f"INSERT INTO enterprise_agent_team ({columns}) VALUES ({binds})"), duplicate)

        wrong_payer = {**values, "id": 3, "source_tool_call_id": "call_enterprise_team_3",
                       "trace_id": "enterprise_team_3", "request_key_prefix": "ent-team:3",
                       "payer_owner_id": 999}
        with pytest.raises(IntegrityError):
            connection.execute(text(f"INSERT INTO enterprise_agent_team ({columns}) VALUES ({binds})"), wrong_payer)

        wrong_parent = {
            **values, "id": 4, "source_tool_call_id": "call_enterprise_team_4",
            "trace_id": "enterprise_team_4", "request_key_prefix": "ent-team:4",
            "assistant_run_id": 10, "enterprise_id": 71, "conversation_id": 81,
            "group_id": 91, "initiator_user_id": 111, "source_message_id": 21, "payer_owner_id": 71,
        }
        with pytest.raises(IntegrityError):
            connection.execute(text(f"INSERT INTO enterprise_agent_team ({columns}) VALUES ({binds})"), wrong_parent)

        wrong_message = {
            **values, "id": 5, "source_tool_call_id": "call_enterprise_team_5",
            "trace_id": "enterprise_team_5", "request_key_prefix": "ent-team:5",
            "assistant_run_id": 12, "source_message_id": 21,
        }
        with pytest.raises(IntegrityError):
            connection.execute(text(f"INSERT INTO enterprise_agent_team ({columns}) VALUES ({binds})"), wrong_message)

        wrong_group = {
            **values, "id": 6, "source_tool_call_id": "call_enterprise_team_6",
            "trace_id": "enterprise_team_6", "request_key_prefix": "ent-team:6", "group_id": 91,
        }
        with pytest.raises(IntegrityError):
            connection.execute(text(f"INSERT INTO enterprise_agent_team ({columns}) VALUES ({binds})"), wrong_group)

        missing_company_conversation = {
            **values, "id": 7, "assistant_run_id": 13, "enterprise_id": 72, "conversation_id": 83,
            "group_id": None, "initiator_user_id": 112, "source_message_id": 22, "payer_owner_id": 72,
            "source_tool_call_id": "call_enterprise_team_7", "trace_id": "enterprise_team_7",
            "request_key_prefix": "ent-team:7",
        }
        with pytest.raises(IntegrityError):
            connection.execute(
                text(f"INSERT INTO enterprise_agent_team ({columns}) VALUES ({binds})"),
                missing_company_conversation,
            )

        wrong_parent_group = {
            **values, "id": 8, "assistant_run_id": 14, "source_tool_call_id": "call_enterprise_team_8",
            "trace_id": "enterprise_team_8", "request_key_prefix": "ent-team:8",
        }
        with pytest.raises(IntegrityError):
            connection.execute(
                text(f"INSERT INTO enterprise_agent_team ({columns}) VALUES ({binds})"),
                wrong_parent_group,
            )

        second_team = {
            **values, "id": 2, "assistant_run_id": 11, "enterprise_id": 71, "conversation_id": 81,
            "group_id": 91, "initiator_user_id": 111, "source_message_id": 21,
            "source_tool_call_id": "call_enterprise_team_2", "payer_owner_id": 71,
            "trace_id": "enterprise_team_2", "request_key_prefix": "ent-team:2",
        }
        connection.execute(text(f"INSERT INTO enterprise_agent_team ({columns}) VALUES ({binds})"), second_team)

        connection.execute(text(
            "INSERT INTO enterprise_agent_team_member "
            "(id, team_id, member_key, display_name, address, kind, snapshot_sha256, capabilities_json, "
            "create_time, update_time) VALUES (1, 1, 'reviewer', '审查员', 'agent:code_reviewer', "
            "'registered', :sha, '{}', '2026-11-06 00:00:00', '2026-11-06 00:00:00')"
        ), {"sha": "c" * 64})
        connection.execute(text(
            "INSERT INTO enterprise_agent_team_task "
            "(id, team_id, member_id, task_key, title, instructions, dependency_keys_json, input_json, "
            "result_json, artifacts_json, errors_json, create_time, update_time) "
            "VALUES (1, 1, 1, 'review', '执行审查', '按只读范围审查', '[]', '{}', '{}', '[]', '[]', "
            "'2026-11-06 00:00:00', '2026-11-06 00:00:00')"
        ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO enterprise_agent_team_task "
                "(id, team_id, member_id, task_key, title, instructions, dependency_keys_json, input_json, "
                "result_json, artifacts_json, errors_json, create_time, update_time) "
                "VALUES (2, 2, 1, 'cross-tenant', '跨团队任务', '不允许挂到其他团队成员', '[]', '{}', '{}', '[]', "
                "'[]', "
                "'2026-11-06 00:00:00', '2026-11-06 00:00:00')"
            ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO enterprise_agent_team_approval "
                "(id, team_id, enterprise_id, conversation_id, initiator_user_id, approval_key, plan_sha256, "
                "required_permission_code, plan_json, requested_by_user_id, create_time, update_time) "
                "VALUES (1, 2, 70, 80, 111, 'cross-scope', :sha, 'agent:approve', '{}', 111, "
                "'2026-11-06 00:00:00', '2026-11-06 00:00:00')"
            ), {"sha": "d" * 64})
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO enterprise_agent_team_approval "
                "(id, team_id, task_id, enterprise_id, conversation_id, group_id, initiator_user_id, approval_key, "
                "plan_sha256, required_permission_code, plan_json, requested_by_user_id, create_time, update_time) "
                "VALUES (2, 2, 1, 71, 81, 91, 111, 'wrong-task-team', :sha, 'group_manager', '{}', 111, "
                "'2026-11-06 00:00:00', '2026-11-06 00:00:00')"
            ), {"sha": "e" * 64})
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO enterprise_agent_team_approval "
                "(id, team_id, enterprise_id, conversation_id, group_id, initiator_user_id, approval_key, "
                "plan_sha256, required_permission_code, plan_json, requested_by_user_id, create_time, update_time) "
                "VALUES (3, 1, 70, 80, 91, 110, 'wrong-group', :sha, 'group_manager', '{}', 110, "
                "'2026-11-06 00:00:00', '2026-11-06 00:00:00')"
            ), {"sha": "f" * 64})
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO enterprise_agent_team_event "
                "(id, team_id, enterprise_id, conversation_id, event_type, actor_address, detail_json, "
                "create_time, update_time) VALUES (1, 2, 70, 80, 'team.started', 'system', '{}', "
                "'2026-11-06 00:00:00', '2026-11-06 00:00:00')"
            ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO enterprise_agent_team_event "
                "(id, team_id, task_id, enterprise_id, conversation_id, event_type, actor_address, "
                "detail_json, create_time, update_time) "
                "VALUES (2, 2, 1, 71, 81, 'task.started', 'system', '{}', "
                "'2026-11-06 00:00:00', '2026-11-06 00:00:00')"
            ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO enterprise_agent_team_event "
                "(id, team_id, member_id, enterprise_id, conversation_id, event_type, actor_address, "
                "detail_json, create_time, update_time) "
                "VALUES (3, 2, 1, 71, 81, 'member.started', 'system', '{}', "
                "'2026-11-06 00:00:00', '2026-11-06 00:00:00')"
            ))

    engine.dispose()


def test_094_model_columns_match_migration_and_downgrade_is_forbidden():
    module = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_parent_stubs(connection)
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        from app.models import load_all_models

        load_all_models()
        from app.models.enterprise_agent_team import (
            EnterpriseAgentTeam,
            EnterpriseAgentTeamApproval,
            EnterpriseAgentTeamEvent,
            EnterpriseAgentTeamMember,
            EnterpriseAgentTeamTask,
        )

        models = (
            EnterpriseAgentTeam, EnterpriseAgentTeamMember, EnterpriseAgentTeamTask,
            EnterpriseAgentTeamApproval, EnterpriseAgentTeamEvent,
        )
        for model in models:
            migrated = {item["name"] for item in inspect(connection).get_columns(model.__tablename__)}
            model_columns = set(model.__table__.columns.keys())
            if model is EnterpriseAgentTeam:
                # project_id is added by migration 097, after the 094 snapshot.
                model_columns.remove("project_id")
            assert migrated == model_columns, model.__tablename__

    with pytest.raises(RuntimeError, match="审计依据"):
        module.downgrade()
    engine.dispose()
