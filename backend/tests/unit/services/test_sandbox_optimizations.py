"""沙箱优化回归:测试生成缓存、stdlib grounding 白名单与卡死告警。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta

import pytest

from app.models.agent_capability import SandboxEnvironment, SandboxEvent, SandboxWorker
from app.models.agent_governance import AgentAlert
from app.models.project import Project
from app.models.user import User
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
def _clear_agent_test_cache(monkeypatch):
    # 缓存行为测试隔离模型配置，真实配置消费由独立入口回归覆盖。
    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda _db, agent, user_id: agent)
    sandbox_service._AGENT_TEST_CACHE.clear()
    yield
    sandbox_service._AGENT_TEST_CACHE.clear()


def _cache_environment(db) -> SandboxEnvironment:
    existing = db.get(SandboxEnvironment, 1)
    if existing is not None:
        return existing
    db.add(User(id=7, username="cache-local", password="local", role="admin", status=1))
    db.add(Project(id=9, user_id=7, project_name="cache local", status="active"))
    environment = SandboxEnvironment(
        id=1,
        public_id="sbx_cache",
        project_id=9,
        owner_id=7,
        agent_code="test_verifier",
        source_sha256="legacy-field-value",
        agent_config_json='{"db_type":"none"}',
        execution_token="cache-local-lease",
        purpose="test",
        language="python",
        test_mode="whitebox",
        runtime="runsc",
        image_ref="unused",
        resource_policy_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.add(environment)
    db.commit()
    return environment


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
        _cache_environment(db),
        archive,
        "python",
        "whitebox",
    )
    second = _generate_agent_test_cases(
        db,
        _cache_environment(db),
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

    environment = _cache_environment(db)
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
    worker = SandboxWorker(
        code="watchdog-worker",
        name="watchdog worker",
        worker_type="local",
        transport="unix",
        endpoint="/tmp/watchdog.sock",
        supported_languages_json='["python"]',
        supported_modes_json='["whitebox"]',
        runtime="runsc",
        max_concurrency=1,
        priority=10,
        status="healthy",
        enabled=1,
    )
    db.add(worker)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_stuck_1",
        project_id=project.id,
        owner_id=42,
        worker_id=worker.id,
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
    monkeypatch.setattr(
        sandbox_service,
        "_stop_registered_worker_requests",
        lambda _worker, _environment: {"sbx_stuck_1": {"request_id": "sbx_stuck_1", "status": "stopped"}},
    )

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


def test_heartbeat_recovery_keeps_environment_stopping_when_worker_cleanup_fails(db, monkeypatch) -> None:
    monkeypatch.setattr(sandbox_service.settings, "sandbox_stuck_after_seconds", 1)
    now = datetime.utcnow()
    project = Project(user_id=1, project_name="回收失败项目", description="", language="python", status="active")
    db.add(project)
    db.flush()
    worker = SandboxWorker(
        code="watchdog-failing-worker",
        name="watchdog failing worker",
        worker_type="local",
        transport="unix",
        endpoint="/tmp/watchdog-failing.sock",
        supported_languages_json='["python"]',
        supported_modes_json='["whitebox"]',
        runtime="runsc",
        max_concurrency=1,
        priority=10,
        status="healthy",
        enabled=1,
    )
    db.add(worker)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_stuck_cleanup_failure",
        project_id=project.id,
        owner_id=42,
        worker_id=worker.id,
        agent_code="sandbox_deployer",
        purpose="test",
        language="python",
        test_mode="whitebox",
        status="stopping",
        runtime="runsc",
        image_ref="prism-sandbox-python:3.11",
        source_sha256="b" * 64,
        resource_policy_json="{}",
        agent_config_json='{"active_worker_request_id":"sbx_stuck_cleanup_failure"}',
        execution_token="lease",
        expires_at=now + timedelta(hours=1),
        started_at=now - timedelta(seconds=1200),
        create_time=now - timedelta(seconds=1200),
        update_time=now - timedelta(seconds=1200),
    )
    db.add(environment)
    db.commit()
    calls = []

    def fail_cleanup(_worker, _environment):
        calls.append(_environment.public_id)
        raise RuntimeError("worker returned 404")

    monkeypatch.setattr(sandbox_service, "_stop_registered_worker_requests", fail_cleanup)

    first = heartbeat_and_recover_sandboxes(db)
    second = heartbeat_and_recover_sandboxes(db)

    assert first["recovered"] == 0
    assert first["cleanup_pending"] == 1
    assert second["recovered"] == 0
    assert second["cleanup_pending"] == 1
    assert calls == [environment.public_id, environment.public_id]
    assert environment.status == "stopping"
    assert environment.stopped_at is None
    assert "未确认" in (environment.error or "")
    assert db.query(SandboxEvent).filter_by(environment_id=environment.id, event_type="failed").count() == 0
    alert = db.query(AgentAlert).filter_by(fingerprint=environment.public_id).one()
    assert "待重试" in alert.title
    assert "404" in alert.detail_json


def test_worker_http_error_keeps_bounded_redacted_json_detail() -> None:
    import httpx

    response = httpx.Response(
        400,
        json={
            "ok": False,
            "error": "invalid source archive; api_key=sk-test-secret Authorization: Bearer abcdefghijklmnop",
        },
    )

    error = sandbox_service._worker_http_error(response, "/execute")

    assert "HTTP 400" in str(error)
    assert "invalid source archive" in str(error)
    assert "sk-test-secret" not in str(error)
    assert "abcdefghijklmnop" not in str(error)
    assert len(str(error)) < 400


def test_worker_http_error_omits_non_json_response_body() -> None:
    import httpx

    response = httpx.Response(404, text="proxy route does not exist; bearer abcdefghijklmnop")

    error = sandbox_service._worker_http_error(response, "/stop")

    assert str(error) == "Sandbox worker /stop 返回 HTTP 404"


def test_worker_stop_requires_matching_request_id_and_terminal_status(monkeypatch) -> None:
    worker = object()
    request_id = "sbx_request_12345678"
    monkeypatch.setattr(
        sandbox_service,
        "_call_worker",
        lambda *_args, **_kwargs: {
            "result": {
                "request_id": "sbx_other_12345678",
                "status": "stopped",
                "result": {"cleanup_confirmed": True},
            }
        },
    )
    with pytest.raises(RuntimeError, match="request_id_mismatch"):
        sandbox_service._stop_worker_requests(worker, [request_id])

    monkeypatch.setattr(
        sandbox_service,
        "_call_worker",
        lambda *_args, **_kwargs: {
            "result": {
                "request_id": request_id,
                "status": "stopped",
                "result": {"cleanup_confirmed": True},
            }
        },
    )
    assert sandbox_service._stop_worker_requests(worker, [request_id])[request_id]["status"] == "stopped"

    monkeypatch.setattr(
        sandbox_service,
        "_call_worker",
        lambda *_args, **_kwargs: {
            "result": {
                "request_id": request_id,
                "status": "failed",
                "result": {"cleanup_error": "Docker daemon unavailable"},
            }
        },
    )
    with pytest.raises(RuntimeError, match="cleanup_unconfirmed"):
        sandbox_service._stop_worker_requests(worker, [request_id])


def test_watchdog_retries_legacy_failed_row_that_lacks_worker_cleanup_receipt(db, monkeypatch) -> None:
    monkeypatch.setattr(sandbox_service.settings, "sandbox_stuck_after_seconds", 1)
    now = datetime.utcnow()
    project = Project(user_id=1, project_name="旧回收状态", description="", language="python", status="active")
    db.add(project)
    db.flush()
    worker = SandboxWorker(
        code="legacy-cleanup-worker",
        name="legacy cleanup worker",
        worker_type="local",
        transport="unix",
        endpoint="/tmp/legacy-cleanup.sock",
        supported_languages_json='["python"]',
        supported_modes_json='["whitebox"]',
        runtime="runsc",
        max_concurrency=1,
        priority=10,
        status="healthy",
        enabled=1,
    )
    db.add(worker)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_legacy_cleanup_pending",
        project_id=project.id,
        owner_id=42,
        worker_id=worker.id,
        agent_code="sandbox_deployer",
        purpose="test",
        language="python",
        test_mode="whitebox",
        status="failed",
        runtime="runsc",
        image_ref="prism-sandbox-python:3.11",
        source_sha256="c" * 64,
        resource_policy_json="{}",
        agent_config_json='{"active_worker_request_id":"sbx_legacy_cleanup_pending"}',
        execution_token="lease",
        expires_at=now + timedelta(hours=1),
        started_at=now - timedelta(seconds=1200),
        stopped_at=now - timedelta(seconds=60),
        result_json='{"cleanup_error":"HTTP 404"}',
        error="沙箱心跳超时，已自动判定卡死并回收",
        create_time=now - timedelta(seconds=1200),
        update_time=now - timedelta(seconds=60),
    )
    db.add(environment)
    db.commit()
    calls = []

    def fail_cleanup(_worker, row):
        calls.append(row.public_id)
        raise RuntimeError("HTTP 404")

    monkeypatch.setattr(sandbox_service, "_stop_registered_worker_requests", fail_cleanup)

    result = heartbeat_and_recover_sandboxes(db)

    assert calls == [environment.public_id]
    assert result["cleanup_pending"] == 1
    assert result["recovered"] == 0
    assert environment.status == "stopping"
    assert environment.stopped_at is None
    assert "未确认" in (environment.error or "")


def test_watchdog_confirms_cleanup_for_legacy_failed_row_and_clears_stale_error(db, monkeypatch) -> None:
    monkeypatch.setattr(sandbox_service.settings, "sandbox_stuck_after_seconds", 1)
    now = datetime.utcnow()
    project = Project(user_id=1, project_name="旧回收确认", description="", language="python", status="active")
    db.add(project)
    db.flush()
    worker = SandboxWorker(
        code="legacy-cleanup-confirm-worker",
        name="legacy cleanup confirm worker",
        worker_type="local",
        transport="unix",
        endpoint="/tmp/legacy-cleanup-confirm.sock",
        supported_languages_json='["python"]',
        supported_modes_json='["whitebox"]',
        runtime="runsc",
        max_concurrency=1,
        priority=10,
        status="healthy",
        enabled=1,
    )
    db.add(worker)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_legacy_cleanup_confirm",
        project_id=project.id,
        owner_id=42,
        worker_id=worker.id,
        agent_code="sandbox_deployer",
        purpose="test",
        language="python",
        test_mode="whitebox",
        status="failed",
        runtime="runsc",
        image_ref="prism-sandbox-python:3.11",
        source_sha256="d" * 64,
        resource_policy_json="{}",
        agent_config_json='{"active_worker_request_id":"sbx_legacy_cleanup_confirm"}',
        execution_token="lease",
        expires_at=now + timedelta(hours=1),
        started_at=now - timedelta(seconds=1200),
        stopped_at=now - timedelta(seconds=60),
        result_json='{"cleanup_error":"HTTP 404"}',
        error="沙箱心跳超时，已自动判定卡死并回收",
        create_time=now - timedelta(seconds=1200),
        update_time=now - timedelta(seconds=60),
    )
    db.add(environment)
    db.commit()
    monkeypatch.setattr(
        sandbox_service,
        "_stop_registered_worker_requests",
        lambda _worker, row: {row.public_id: {"request_id": row.public_id, "status": "stopped"}},
    )

    result = heartbeat_and_recover_sandboxes(db)

    assert result["recovered"] == 1
    assert result["cleanup_pending"] == 0
    assert environment.status == "failed"
    assert environment.stopped_at is not None
    assert json.loads(environment.result_json) == {"cleanup_confirmed": True}
    assert "Worker 已确认" in (environment.error or "")
