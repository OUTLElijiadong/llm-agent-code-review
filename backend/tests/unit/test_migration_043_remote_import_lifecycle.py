"""远程导入生命周期迁移结构回归。"""

import ast
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "043_remote_import_lifecycle.py"
)


def test_remote_import_lifecycle_migration_has_short_linear_revision_and_fields() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignments = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            value = node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
            value = node.value
        else:
            continue
        if name in {"revision", "down_revision"} and value is not None:
            assignments[name] = ast.literal_eval(value)

    assert assignments == {
        "revision": "043_remote_import_lifecycle",
        "down_revision": "042_merge_review_import_heads",
    }
    assert len(assignments["revision"]) <= 32
    for field in ("cancel_reason", "cancel_requested_at", "heartbeat_at"):
        assert field in source
