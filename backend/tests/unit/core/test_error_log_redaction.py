"""Unexpected request failures must not put supplied credentials in logs."""

import json
import subprocess
import sys

from app.core import logger as logger_module


def test_logger_disables_diagnostic_variables_for_both_sinks(monkeypatch):
    calls = []

    class Recorder:
        def remove(self):
            pass

        def configure(self, **_kwargs):
            pass

        def add(self, sink, **kwargs):
            calls.append((sink, kwargs))

    monkeypatch.setattr(logger_module, "logger", Recorder())
    logger_module.setup_logger()
    assert len(calls) == 2
    assert all(options.get("diagnose") is False for _, options in calls)


def test_real_console_and_file_sinks_omit_traceback_locals():
    program = r'''
import io, json, os, secrets, tempfile
from contextlib import redirect_stderr
from pathlib import Path
from loguru import logger
from app.core.logger import setup_logger

with tempfile.TemporaryDirectory(prefix="prism-sinks-test-") as directory:
    os.chdir(directory)
    Path("logs").mkdir()
    stream = io.StringIO()
    secret = secrets.token_hex(24)
    with redirect_stderr(stream):
        setup_logger()
        def fail_with_local():
            if secret:
                raise RuntimeError("safe failure")
        try:
            fail_with_local()
        except RuntimeError:
            logger.exception("safe diagnostic")
        logger.complete()
    console = stream.getvalue()
    file_log = next(Path("logs").glob("app_*.log")).read_text()
    print(json.dumps({
        "console_secret": secret in console,
        "file_secret": secret in file_log,
        "console_diagnostic": "safe diagnostic" in console and "RuntimeError" in console,
        "file_diagnostic": "safe diagnostic" in file_log and "RuntimeError" in file_log,
    }))
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=30, check=True)
    evidence = json.loads(result.stdout)
    assert not evidence["console_secret"] and not evidence["file_secret"]
    assert evidence["console_diagnostic"] and evidence["file_diagnostic"]


def test_api_config_unexpected_failure_omits_payload_and_exception_secrets():
    # Isolate Loguru's global handlers and test secrets in a child process.
    # Only boolean outcomes and non-sensitive HTTP metadata return to pytest.
    program = r'''
import io, json, secrets, tempfile
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.api.v1.api_config import router
from app.core.database import Base, get_db
from app.core.error_handlers import register_handlers
from app.core.security import create_access_token
from app.models.api_config import UserApiConfig
from app.models.user import User
from app.utils import api_resolver

with tempfile.TemporaryDirectory(prefix="prism-log-test-") as directory:
    engine = create_engine("sqlite:///" + directory + "/test.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[User.__table__, UserApiConfig.__table__])
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions.begin() as db:
        db.add(User(id=701, username="test", password="test", role="user", status=1))
    api_resolver._resolve_host = lambda _host: {"8.8.8.8"}
    payload_secret, error_secret = secrets.token_hex(24), secrets.token_hex(24)
    @event.listens_for(engine, "before_cursor_execute")
    def fail_config_read(_conn, _cursor, statement, _parameters, _context, _many):
        if "user_api_config" in statement.lower():
            raise RuntimeError(error_secret)
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api")
    def isolated_db():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = isolated_db
    token = create_access_token(701, "user")
    sink = io.StringIO()
    logger.remove()
    logger.configure(extra={"request_id": "-"})
    logger.add(sink, format="{extra[request_id]} | {message}", diagnose=True, backtrace=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.put(
            "/api/api-config",
            headers={"Authorization": "Bearer " + token, "X-Request-Id": "audit-request-123"},
            json={"provider": "openai", "api_key": payload_secret,
                  "base_url": "https://api.deepseek.com", "model": "test"},
        )
    log_text = sink.getvalue()
    print(json.dumps({
        "http_status": response.status_code,
        "code": response.json().get("code"),
        "payload_exposed": payload_secret in log_text,
        "exception_exposed": error_secret in log_text,
        "token_exposed": token in log_text,
        "has_error_type": "RuntimeError" in log_text,
        "has_location": "save_config" in log_text,
        "has_multiple_frames": "app.api.v1.api_config:save_config:" in log_text
        and "app.services.api_config_service:save_config:" in log_text,
        "has_request_id": "audit-request-123" in log_text,
    }))
    engine.dispose()
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=30, check=True)
    evidence = json.loads(result.stdout)
    assert evidence["http_status"] == 500 and evidence["code"] == 50000
    assert not evidence["payload_exposed"]
    assert not evidence["exception_exposed"]
    assert not evidence["token_exposed"]
    assert evidence["has_error_type"] and evidence["has_location"] and evidence["has_request_id"]
    assert evidence["has_multiple_frames"]
