"""Security Sentinel API must preserve usable partial output on failures."""

from types import SimpleNamespace

import pytest

from app.agents.base import AgentResult
from app.ai.exceptions import AiServiceError
from app.api.v1 import security as security_api
from app.schemas.security import SecurityScanTaskIn


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
