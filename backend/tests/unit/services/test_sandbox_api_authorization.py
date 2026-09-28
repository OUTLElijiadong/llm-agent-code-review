"""沙箱公开 API 必须要求服务端远程目标确认，而不是相信布尔参数。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.v1 import sandboxes as sandbox_api
from app.core.exceptions import ForbiddenError
from app.schemas.agent_capability import SandboxCreateIn
from app.services import sandbox_service


def test_create_sandbox_route_rejects_client_asserted_boolean_as_approval(monkeypatch):
    actor = SimpleNamespace(id=7, username="reviewer")
    payload = SandboxCreateIn(
        project_id=91,
        purpose="test",
        language="python",
        test_mode="blackbox",
        remote_target_url="https://target.example/path",
        remote_target_authorized=True,
    )
    captured: dict[str, object] = {}

    def create(_db, _actor, data, *, require_remote_target_approval=False):
        captured["require_server_approval"] = require_remote_target_approval
        sandbox_service._require_remote_target_authorization(
            data,
            server_approval_required=require_remote_target_approval,
        )
        raise AssertionError("缺少服务端票据时不得启动外部请求")

    monkeypatch.setattr(sandbox_service, "create_environment", create)

    with pytest.raises(ForbiddenError, match="服务端逐目标确认"):
        sandbox_api.create_sandbox(payload, SimpleNamespace(), actor)

    assert captured["require_server_approval"] is True
