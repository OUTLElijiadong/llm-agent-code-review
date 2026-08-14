"""沙箱优化回归:测试生成缓存、stdlib grounding 白名单与卡死告警。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.agent_capability import SandboxEnvironment, SandboxEvent
from app.models.agent_governance import AgentAlert
from app.models.project import Project
from app.services import sandbox_service
from app.services.sandbox_service import (
    _agent_test_cache_key,
    _generate_agent_test_cases,
    heartbeat_and_recover_sandboxes,
)


def _zip_with(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.fixture(autouse=True)
def _clear_agent_test_cache():
    sandbox_service._AGENT_TEST_CACHE.clear()
    yield
    sandbox_service._AGENT_TEST_CACHE.clear()


def _cache_environment() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        public_id="sbx_cache",
        project_id=9,
        owner_id=7,
        source_sha256="legacy-field-value",
        agent_config_json='{"db_type":"none"}',
    )


def _fake_generator(calls: list[list[dict[str, str]]]) -> type:
    class FakeGenerator:
        _api_key = "configured"

        def generate(self, **kwargs):
            calls.append(kwargs["source_summary"])
            return {
                "files": [
                    {"path": "test_ai_one.py", "content": "assert 1 == 1\n"},
                    {"path": "test_ai_two.py", "content": "assert 2 > 1\n"},
                ]
            }

    return FakeGenerator


def test_generate_agent_tests_cache_hits_and_uses_archive_sha256(db, monkeypatch) -> None:
    calls: list[dict] = []
    archive = _zip_with({"main.py": "VALUE = 1\n"})
    monkeypatch.setattr(
        "app.agents.test_case_generator_agent.TestCaseGeneratorAgent",
        _fake_generator(calls),
    )
    monkeypatch.setattr("app.services.sandbox_service._append_event", lambda *_args, **_kwargs: None)

    first = _generate_agent_test_cases(
        db,
        _cache_environment(),
        archive,
        "python",
        "whitebox",
    )
    second = _generate_agent_test_cases(
        db,
        _cache_environment(),
        archive,
        "python",
        "whitebox",
    )

    assert first is not None
    assert first == second
    assert len(calls) == 1
    key = _agent_test_cache_key(archive, "python", "whitebox")
    assert key == (hashlib.sha256(base64.b64decode(archive)).hexdigest(), "python", "whitebox")
    assert key in sandbox_service._AGENT_TEST_CACHE


def test_generate_agent_tests_cache_expires_and_regenerates(db, monkeypatch) -> None:
    calls: list[dict] = []
    archive = _zip_with({"main.py": "VALUE = 2\n"})
    monkeypatch.setattr(
        "app.agents.test_case_generator_agent.TestCaseGeneratorAgent",
        _fake_generator(calls),
    )
    monkeypatch.setattr("app.services.sandbox_service._append_event", lambda *_args, **_kwargs: None)

    environment = _cache_environment()
    first = _generate_agent_test_cases(db, environment, archive, "python", "whitebox")
    assert first is not None
    assert len(calls) == 1

    key = _agent_test_cache_key(archive, "python", "whitebox")
    expires_at, cached_files = sandbox_service._AGENT_TEST_CACHE[key]
    sandbox_service._AGENT_TEST_CACHE[key] = (0.0, cached_files)

    second = _generate_agent_test_cases(db, environment, archive, "python", "whitebox")

    assert second is not None
    assert first == second
    assert len(calls) == 2
    assert expires_at > 0.0


def test_grounding_allows_stdlib_from_imports_but_flags_source_absent_attribute() -> None:
    from app.agents import test_case_generator_agent as generator

    summary = {
        "language": "python",
        "snippets": {"main.py": "RESULT = 1\n"},
    }
    files = [
        {
            "path": "test_ai_flow.py",
            "content": (
                "from urllib.parse import urlencode\n"
                "from json import JSONDecodeError\n"
                "query = urlencode({'a': 'b'})\n"
                "assert query == 'a=b'\n"
                "response.absent_attribute\n"
            ),
        }
    ]

    unsupported = generator._grounding_feedback(files, summary)

    assert unsupported == ["absent_attribute"]


def test_heartbeat_recovery_creates_sandbox_stuck_alert(db, monkeypatch) -> None:
    monkeypatch.setattr(sandbox_service.settings, "sandbox_stuck_after_seconds", 1)
    now = datetime.utcnow()
    project = Project(
        user_id=1,
        project_name="缓存项目",
        description="",
        language="python",
        status="active",
    )
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_stuck_1",
        project_id=project.id,
        owner_id=42,
        worker_id=None,
        agent_code="sandbox_deployer",
        purpose="test",
        language="python",
        test_mode="whitebox",
        status="running",
        runtime="runsc",
        image_ref="prism-sandbox-python:3.11",
        source_sha256="a" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=now + timedelta(hours=1),
        started_at=now - timedelta(seconds=1200),
        create_time=now - timedelta(seconds=1200),
        update_time=now - timedelta(seconds=1200),
    )
    db.add(environment)
    db.commit()

    result = heartbeat_and_recover_sandboxes(db)

    assert result["recovered"] == 1
    assert environment.status == "failed"
    assert "心跳超时" in (environment.error or "")

    alerts = db.query(AgentAlert).all()
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.alert_type == "sandbox_stuck"
    assert alert.category == "sandbox_stuck"
    assert alert.source == "sandbox_watchdog"
    assert alert.severity == "high"
    assert alert.user_id == environment.owner_id
    assert alert.fingerprint == environment.public_id
    assert environment.public_id in alert.title
    assert project.project_name in alert.title
    assert "心跳超时" in alert.title

    detail = json.loads(alert.detail_json)
    assert detail["public_id"] == environment.public_id
    assert detail["project_id"] == project.id
    assert detail["project_name"] == project.project_name
    assert "心跳超时" in detail["reason"]

    failed_event = (
        db.query(SandboxEvent)
        .filter(
            SandboxEvent.environment_id == environment.id,
            SandboxEvent.event_type == "failed",
        )
        .one()
    )
    assert failed_event.stage == "watchdog"
