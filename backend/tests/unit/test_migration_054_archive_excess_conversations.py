"""054 迁移：存量会话归档与精确降级验证。"""

from datetime import datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, inspect, text


def _migration():
    path = Path(__file__).parents[2] / "alembic/versions/054_archive_excess_agent_conversations.py"
    spec = spec_from_file_location("migration_054", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_archives_only_oldest_idle_conversations_and_downgrade_restores() -> None:
    migration = _migration()
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table(
        "agent_mesh_conversation",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, nullable=False),
        Column("surface", String(24), nullable=False),
        Column("session_key", String(128), nullable=False),
        Column("status", String(24), nullable=False),
        Column("active_run_id", String(80)),
        Column("active_run_status", String(32)),
        Column("last_seen_at", DateTime, nullable=False),
        Column("last_message_at", DateTime),
    )
    response_run = Table(
        "agent_response_run",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, nullable=False),
        Column("surface", String(24), nullable=False),
        Column("session_key", String(128), nullable=False),
        Column("status", String(32), nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO agent_mesh_conversation
              (id,user_id,surface,session_key,status,active_run_id,active_run_status,last_seen_at,last_message_at)
            VALUES
              (1,1,'user','s01','active','r1','running','2026-01-01 00:00:01',NULL),
              (2,1,'admin','s02','active','r2','waiting_approval','2026-01-01 00:00:02',NULL),
              (3,1,'user','s03','active',NULL,NULL,'2026-01-01 00:00:03',NULL),
              (4,1,'user','s04','active',NULL,NULL,'2026-01-01 00:00:04',NULL),
              (5,1,'user','s05','active',NULL,NULL,'2026-01-01 00:00:05',NULL),
              (6,1,'user','s06','active',NULL,NULL,'2026-01-01 00:00:06',NULL),
              (7,1,'user','s07','active',NULL,NULL,'2026-01-01 00:00:07',NULL),
              (8,1,'user','s08','active',NULL,NULL,'2026-01-01 00:00:08',NULL),
              (9,1,'user','s09','active',NULL,NULL,'2026-01-01 00:00:09',NULL),
              (10,1,'user','s10','active',NULL,NULL,'2026-01-01 00:00:10',NULL),
              (11,1,'user','s11','active',NULL,NULL,'2026-01-01 00:00:11',NULL),
              (12,1,'user','s12','active',NULL,NULL,'2026-01-01 00:00:12',NULL),
              (13,1,'user','s13','active',NULL,NULL,'2026-01-01 00:00:13',NULL),
              (20,2,'user','other-1','active',NULL,NULL,'2026-01-01 00:00:01',NULL),
              (21,2,'user','other-2','archived',NULL,NULL,'2026-01-01 00:00:02',NULL)
        """))
        connection.execute(response_run.insert(), [
            {"id": 1, "user_id": 1, "surface": "user", "session_key": "s01", "status": "running"},
            {"id": 2, "user_id": 1, "surface": "admin", "session_key": "s02", "status": "waiting_approval"},
        ])

        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        active_ids = set(connection.execute(text(
            "SELECT id FROM agent_mesh_conversation WHERE user_id=1 AND status='active'"
        )).scalars())
        archived_ids = set(connection.execute(text(
            "SELECT id FROM agent_mesh_conversation WHERE user_id=1 AND status='archived'"
        )).scalars())
        marker_ids = set(connection.execute(text(
            "SELECT conversation_id FROM migration_054_archived_conversation"
        )).scalars())
        assert active_ids == {1, 2, 6, 7, 8, 9, 10, 11, 12, 13}
        assert archived_ids == {3, 4, 5}
        assert marker_ids == {3, 4, 5}
        assert connection.execute(text(
            "SELECT status FROM agent_mesh_conversation WHERE id=21"
        )).scalar_one() == "archived"

        migration.downgrade()
        assert connection.execute(text(
            "SELECT COUNT(*) FROM agent_mesh_conversation WHERE user_id=1 AND status='active'"
        )).scalar_one() == 13
        assert connection.execute(text(
            "SELECT status FROM agent_mesh_conversation WHERE id=21"
        )).scalar_one() == "archived"
        assert migration.ARCHIVE_STATE_TABLE not in inspect(connection).get_table_names()

    engine.dispose()


def test_upgrade_preserves_all_occupied_states_and_never_deletes_messages() -> None:
    migration = _migration()
    engine = create_engine("sqlite://")
    metadata = MetaData()
    conversation = Table(
        "agent_mesh_conversation",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, nullable=False),
        Column("surface", String(24), nullable=False),
        Column("session_key", String(128), nullable=False),
        Column("status", String(24), nullable=False),
        Column("active_run_id", String(80)),
        Column("active_run_status", String(32)),
        Column("last_seen_at", DateTime, nullable=False),
        Column("last_message_at", DateTime),
    )
    response_run = Table(
        "agent_response_run",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, nullable=False),
        Column("surface", String(24), nullable=False),
        Column("session_key", String(128), nullable=False),
        Column("status", String(32), nullable=False),
    )
    message = Table(
        "agent_mesh_message",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, nullable=False),
        Column("payload_json", String(255), nullable=False),
    )
    metadata.create_all(engine)

    base = datetime(2026, 1, 1)
    protected_statuses = migration.OCCUPIED_RUN_STATUSES
    conversations = []
    messages = []
    for conversation_id in range(1, 94):
        last_message_at = None
        if conversation_id == 91:
            last_message_at = base + timedelta(minutes=5)
        elif conversation_id in {92, 93}:
            last_message_at = base + timedelta(minutes=200)
        conversations.append({
            "id": conversation_id,
            "user_id": 1,
            "surface": "user" if conversation_id % 2 else "admin",
            "session_key": f"session-{conversation_id:03d}",
            "status": "active",
            "active_run_id": f"run-{conversation_id}" if conversation_id <= 6 else None,
            "active_run_status": protected_statuses[conversation_id - 1] if conversation_id <= 6 else None,
            "last_seen_at": base + timedelta(minutes=conversation_id),
            "last_message_at": last_message_at,
        })
        messages.append({"id": conversation_id, "user_id": 1, "payload_json": f'{{"n":{conversation_id}}}'})
    conversations.append({
        "id": 100,
        "user_id": 1,
        "surface": "user",
        "session_key": "already-archived",
        "status": "archived",
        "active_run_id": None,
        "active_run_status": None,
        "last_seen_at": base,
        "last_message_at": None,
    })

    with engine.begin() as connection:
        connection.execute(conversation.insert(), conversations)
        connection.execute(message.insert(), messages)
        connection.execute(response_run.insert(), [
            {
                "id": run_id,
                "user_id": 1,
                "surface": "user" if run_id % 2 else "admin",
                "session_key": f"session-{run_id:03d}",
                "status": status,
            }
            for run_id, status in enumerate(protected_statuses, start=1)
        ])

        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        active_ids = set(connection.execute(text(
            "SELECT id FROM agent_mesh_conversation WHERE user_id=1 AND status='active'"
        )).scalars())
        marker_ids = set(connection.execute(text(
            "SELECT conversation_id FROM migration_054_archived_conversation"
        )).scalars())
        assert active_ids == {1, 2, 3, 4, 5, 6, 89, 90, 92, 93}
        assert len(marker_ids) == 83
        assert not active_ids.intersection(marker_ids)
        assert connection.execute(text("SELECT COUNT(*) FROM agent_mesh_message")).scalar_one() == 93
        assert connection.execute(text(
            "SELECT status FROM agent_mesh_conversation WHERE id=100"
        )).scalar_one() == "archived"

        migration.downgrade()
        assert connection.execute(text(
            "SELECT COUNT(*) FROM agent_mesh_conversation WHERE user_id=1 AND status='active'"
        )).scalar_one() == 93
        assert connection.execute(text("SELECT COUNT(*) FROM agent_mesh_message")).scalar_one() == 93
        assert connection.execute(text(
            "SELECT status FROM agent_mesh_conversation WHERE id=100"
        )).scalar_one() == "archived"

    engine.dispose()
