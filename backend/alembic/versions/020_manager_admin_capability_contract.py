"""Allow the protected manager to enter registered admin capabilities.

Revision ID: 020
Revises: 019
"""

from __future__ import annotations

import copy
import json
from typing import Any, Union

import sqlalchemy as sa

from alembic import op

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels = None
depends_on = None

_TOOL = "admin_execute_capability"
_VERSION_KEY = "manager_admin_capability_boundary_version"
_VERSION = "manager_admin_capability_v1"
_STATE_KEY = "_migration_020_manager_admin_capability_state"
_PERMISSION_NOTE = "manager_admin_capability_v1 protected manager admin capability gateway"
_DEFAULT_SCOPE = "管理全部管理员页面并通过真实业务 API 执行已登记能力"


def _decode_config(raw: Any) -> dict:
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("manager config_json is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("manager config_json must be a JSON object")
    return parsed


def _upgrade_config(config: dict) -> dict:
    updated = copy.deepcopy(config)
    if _STATE_KEY not in updated:
        updated[_STATE_KEY] = {
            "boundary_present": "governance_boundary" in updated,
            "boundary": copy.deepcopy(updated.get("governance_boundary")),
            "version_present": _VERSION_KEY in updated,
            "version": copy.deepcopy(updated.get(_VERSION_KEY)),
        }
    raw_boundary = updated.get("governance_boundary")
    boundary = dict(raw_boundary) if isinstance(raw_boundary, dict) else {}

    def _tools(key: str) -> set[str]:
        values = boundary.get(key, [])
        if not isinstance(values, (list, tuple, set)):
            return set()
        return {str(value) for value in values if value}

    allowed = _tools("allowed_tools")
    approvals = _tools("approval_tools")
    blocked = _tools("blocked_tools")
    allowed.add(_TOOL)
    approvals.discard(_TOOL)
    blocked.discard(_TOOL)
    boundary.update(
        {
            "scope": str(boundary.get("scope") or _DEFAULT_SCOPE),
            "allowed_tools": sorted(allowed),
            "approval_tools": sorted(approvals),
            "blocked_tools": sorted(blocked),
        }
    )
    updated["governance_boundary"] = boundary
    updated[_VERSION_KEY] = _VERSION
    return updated


def _downgrade_config(config: dict) -> dict:
    restored = copy.deepcopy(config)
    state = restored.pop(_STATE_KEY, None)
    if not isinstance(state, dict):
        if restored.get(_VERSION_KEY) != _VERSION:
            return restored
        raw_boundary = restored.get("governance_boundary")
        if isinstance(raw_boundary, dict):
            boundary = copy.deepcopy(raw_boundary)
            values = boundary.get("allowed_tools", [])
            allowed = [str(value) for value in values if value and str(value) != _TOOL]
            boundary["allowed_tools"] = sorted(set(allowed))
            restored["governance_boundary"] = boundary
        restored.pop(_VERSION_KEY, None)
        return restored
    if state.get("boundary_present"):
        restored["governance_boundary"] = state.get("boundary")
    else:
        restored.pop("governance_boundary", None)
    if state.get("version_present"):
        restored[_VERSION_KEY] = state.get("version")
    else:
        restored.pop(_VERSION_KEY, None)
    return restored


def _tables() -> tuple[sa.TableClause, sa.TableClause]:
    profile = sa.table(
        "agent_profile",
        sa.column("id", sa.BigInteger()),
        sa.column("code", sa.String()),
        sa.column("config_json", sa.Text()),
        sa.column("update_time", sa.DateTime()),
    )
    permission = sa.table(
        "agent_tool_permission",
        sa.column("id", sa.BigInteger()),
        sa.column("agent_code", sa.String()),
        sa.column("tool_code", sa.String()),
        sa.column("permission", sa.String()),
        sa.column("risk_level", sa.String()),
        sa.column("enabled", sa.SmallInteger()),
        sa.column("note", sa.String()),
    )
    return profile, permission


def upgrade() -> None:
    conn = op.get_bind()
    profile, permission = _tables()
    row = conn.execute(
        sa.select(profile.c.id, profile.c.config_json).where(profile.c.code == "manager")
    ).mappings().first()
    if row is not None:
        config = _upgrade_config(_decode_config(row["config_json"]))
        conn.execute(
            profile.update()
            .where(profile.c.id == row["id"])
            .values(config_json=json.dumps(config, ensure_ascii=False, sort_keys=True), update_time=sa.func.now())
        )

    existing_permission = conn.execute(
        sa.select(permission.c.id).where(
            permission.c.agent_code == "manager",
            permission.c.tool_code == _TOOL,
        )
    ).first()
    if existing_permission is None:
        conn.execute(
            permission.insert().values(
                agent_code="manager",
                tool_code=_TOOL,
                permission="allow",
                risk_level="low",
                enabled=1,
                note=_PERMISSION_NOTE,
            )
        )


def downgrade() -> None:
    conn = op.get_bind()
    profile, permission = _tables()
    row = conn.execute(
        sa.select(profile.c.id, profile.c.config_json).where(profile.c.code == "manager")
    ).mappings().first()
    if row is not None:
        config = _downgrade_config(_decode_config(row["config_json"]))
        conn.execute(
            profile.update()
            .where(profile.c.id == row["id"])
            .values(config_json=json.dumps(config, ensure_ascii=False, sort_keys=True), update_time=sa.func.now())
        )
    conn.execute(
        permission.delete().where(
            permission.c.agent_code == "manager",
            permission.c.tool_code == _TOOL,
            permission.c.note == _PERMISSION_NOTE,
        )
    )
