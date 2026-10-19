"""Security Sentinel API must preserve usable partial output on failures."""

import io
from types import SimpleNamespace

import pytest
from loguru import logger

from app.agents.base import AgentResult
from app.ai.exceptions import AiServiceError
from app.api.v1 import security as security_api
from app.core.exceptions import PermissionError as AppPermissionError
from app.schemas.security import SecurityScanFileIn, SecurityScanTaskIn


@pytest.mark.parametrize("endpoint", [security_api.scan_task, security_api.list_findings])
@pytest.mark.parametrize(
    ("findings", "scanned_file_count"),
    [
        ([{"title": "未授权访问", "severity": "高"}], 2),
        ([], 2),
    ],
)
def test_security_task_endpoints_return_partial_findings_and_coverage(
    monkeypatch, endpoint, findings, scanned_file_count,
):
    partial = AgentResult(
        success=False,
        error="第二阶段复核失败，以下是已完成部分",
        failure_kind="upstream_error",
        data={
            "findings": findings,
            "compliance": {
                "scan_complete": False,
                "scanned_file_count": scanned_file_count,
                "total_file_count": 5,
                "unscanned": ["src/remaining.py"],
            },
            "summary": "已完成部分扫描，发现一项高危问题。",
        },
    )
    sentinel = SimpleNamespace(scan_task=lambda **_kwargs: partial)
    monkeypatch.setattr(
        security_api,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(security_sentinel=sentinel),
    )

    if endpoint is security_api.scan_task:
        response = endpoint(SecurityScanTaskIn(task_id=73), db=object(), user=SimpleNamespace(id=5))
    else:
        response = endpoint(task_id=73, db=object(), user=SimpleNamespace(id=5))

    assert response.data.execution_status == "partial"
    assert response.data.error_message == partial.error
    assert [finding.title for finding in response.data.findings] == [
        finding["title"] for finding in findings
    ]
    assert response.data.compliance["scanned_file_count"] == scanned_file_count
    assert response.data.compliance["unscanned"] == ["src/remaining.py"]


@pytest.mark.parametrize("endpoint", [security_api.scan_task, security_api.list_findings])
def test_security_task_endpoints_keep_explicit_failure_when_no_partial_evidence(
    monkeypatch, endpoint,
):
    failed = AgentResult(
        success=False,
        error="扫描启动失败",
        failure_kind="upstream_error",
        data={"findings": [], "compliance": {"scan_complete": False}, "file_count": 0},
    )
    sentinel = SimpleNamespace(scan_task=lambda **_kwargs: failed)
    monkeypatch.setattr(
        security_api,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(security_sentinel=sentinel),
    )

    with pytest.raises(AiServiceError, match="扫描启动失败") as exc_info:
        if endpoint is security_api.scan_task:
            endpoint(SecurityScanTaskIn(task_id=73), db=object(), user=SimpleNamespace(id=5))
        else:
            endpoint(task_id=73, db=object(), user=SimpleNamespace(id=5))

    assert exc_info.value.code == 50220


def test_security_task_endpoint_does_not_expose_partial_data_after_authorization_revoked(
    monkeypatch,
):
    denied = AgentResult(
        success=False,
        error="无权执行该任务",
        failure_kind="authorization_revoked",
        data={
            "findings": [{"title": "不可泄露", "severity": "严重"}],
            "compliance": {"scanned_file_count": 1},
        },
    )
    sentinel = SimpleNamespace(scan_task=lambda **_kwargs: denied)
    monkeypatch.setattr(
        security_api,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(security_sentinel=sentinel),
    )

    with pytest.raises(AiServiceError, match="无权执行该任务") as exc_info:
        security_api.scan_task(
            SecurityScanTaskIn(task_id=73), db=object(), user=SimpleNamespace(id=5),
        )

    assert exc_info.value.code == 40320


def test_reportable_security_scan_exception_becomes_safe_failed_result():
    provider_secret = "provider=https://private.invalid key=do-not-expose"
    log_output = io.StringIO()
    sink_id = logger.add(log_output, level="ERROR", format="{message}")

    def crash():
        raise RuntimeError(provider_secret)

    try:
        result = security_api._execute_reportable_scan(crash)
    finally:
        logger.remove(sink_id)

    assert isinstance(result, AgentResult)
    assert result.success is False
    assert result.failure_kind == "execution_exception"
    assert "本次结果无法给出" in result.error
    assert provider_secret not in result.error
    assert provider_secret not in log_output.getvalue()
    assert result.data["compliance"]["coverage_status"] == "failed"
    assert result.data["compliance"]["unscanned"]


def test_reportable_scan_preserves_permission_exceptions():
    denied = AppPermissionError("项目访问权已撤销")

    def reject():
        raise denied

    with pytest.raises(AppPermissionError) as exc_info:
        security_api._execute_reportable_scan(reject)

    assert exc_info.value is denied


def test_scan_file_does_not_convert_permission_denial_into_a_report(monkeypatch):
    denied = AppPermissionError("项目访问权已撤销")
    code_file = SimpleNamespace(
        status="active", project_id=13, file_path="src/private.py", file_name="private.py",
    )
    db = SimpleNamespace(get=lambda *_args: code_file)
    user = SimpleNamespace(id=5)

    def reject_scan(**_kwargs):
        raise denied

    sentinel = SimpleNamespace(scan_file=reject_scan)
    monkeypatch.setattr(security_api, "require_project_execution", lambda *_args: None)
    monkeypatch.setattr(
        security_api,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(security_sentinel=sentinel),
    )
    monkeypatch.setattr(
        security_api,
        "_persist_scan_result",
        lambda *_args, **_kwargs: pytest.fail("permission failures must not create reports"),
    )
    with pytest.raises(AppPermissionError) as exc_info:
        security_api.scan_file(SecurityScanFileIn(file_id=3), db=db, user=user)

    assert exc_info.value is denied


def test_scan_file_persists_safe_report_after_unexpected_runtime_error(monkeypatch):
    provider_secret = "provider=https://private.invalid key=do-not-expose"
    code_file = SimpleNamespace(
        status="active", project_id=13, file_path="src/a.py", file_name="a.py",
    )
    db = SimpleNamespace(get=lambda *_args: code_file)
    user = SimpleNamespace(id=5)

    def crash_scan(**_kwargs):
        raise RuntimeError(provider_secret)

    sentinel = SimpleNamespace(scan_file=crash_scan)
    persist_calls = []
    monkeypatch.setattr(security_api, "require_project_execution", lambda *_args: None)
    monkeypatch.setattr(
        security_api,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(security_sentinel=sentinel),
    )

    def persist(_db, _user, **kwargs):
        persist_calls.append(kwargs)
        return {
            "execution_status": "failed",
            "error_message": kwargs["result"].error,
            "report_task_id": 91,
        }

    monkeypatch.setattr(security_api, "_persist_scan_result", persist)
    response = security_api.scan_file(SecurityScanFileIn(file_id=3), db=db, user=user)

    assert len(persist_calls) == 1
    assert persist_calls[0]["result"].failure_kind == "execution_exception"
    assert response.data.report_task_id == 91
    assert response.data.execution_status == "failed"
    assert provider_secret not in response.data.error_message
