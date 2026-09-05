from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, text


def test_review_snapshot_upgrade_and_downgrade_preserve_legacy_rows():
    location = Path(__file__).parents[2] / "alembic/versions/047_review_input_snapshot.py"
    spec = spec_from_file_location("review_snapshot_migration", location)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for name in ("review_task", "review_task_file"):
        Table(name, metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO review_task (id) VALUES (1)"))
        connection.execute(text("INSERT INTO review_task_file (id) VALUES (1)"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert connection.execute(text("SELECT coverage FROM review_task WHERE id=1")).scalar() is None
        legacy_columns = connection.execute(text(
            "SELECT version_no, content_sha256, file_snapshot FROM review_task_file"
        )).one()
        assert legacy_columns == (None, None, None)
        assert {column["name"] for column in inspect(connection).get_columns("review_task_file")} == {
            "id", "version_no", "content_sha256", "file_snapshot",
        }
        migration.downgrade()
        assert connection.execute(text("SELECT COUNT(*) FROM review_task")).scalar() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM review_task_file")).scalar() == 1
