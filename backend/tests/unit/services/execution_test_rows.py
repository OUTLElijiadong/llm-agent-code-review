"""Persist complete local identities for execution tests that previously used stubs."""

from datetime import datetime, timedelta

from app.models.agent_capability import SandboxEnvironment
from app.models.project import Project
from app.models.user import User


def authorized_sandbox_environment(db, *, owner_id=1, project_id=1, public_id="sbx_local", **values):
    """Administrative unit fixture; permission-denial cases use their own real RBAC rows."""
    if db.get(User, owner_id) is None:
        db.add(User(id=owner_id, username=f"local-sandbox-{owner_id}", password="local", role="admin", status=1))
    if db.get(Project, project_id) is None:
        db.add(Project(id=project_id, user_id=owner_id, project_name="local sandbox fixture", status="active"))
    attributes = dict(
        public_id=public_id,
        project_id=project_id,
        owner_id=owner_id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="whitebox",
        runtime="runsc",
        image_ref="unused",
        source_sha256="0" * 64,
        execution_token="local-fixture-lease",
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    attributes.update(values)
    row = SandboxEnvironment(**attributes)
    db.add(row)
    db.commit()
    return row
