"""迁移 042 的并列分支合并契约。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "042_merge_review_result_and_remote_import_heads.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_042_merge_heads", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_merge_migration_has_both_041_heads_and_noop_downgrade() -> None:
    migration = _load_migration()

    assert migration.revision == "042_merge_review_import_heads"
    assert len(migration.revision) <= 32
    assert set(migration.down_revision) == {
        "041_review_result_pipeline",
        "041a_remote_import_tasks",
    }
    assert migration.upgrade() is None
    assert migration.downgrade() is None


def test_all_revision_ids_fit_default_alembic_version_column() -> None:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.set_main_option("path_separator", "os")

    revisions = ScriptDirectory.from_config(config).walk_revisions()
    oversized = [revision.revision for revision in revisions if len(revision.revision) > 32]

    assert oversized == []
