"""本地黑盒请求白名单的 API schema 边界。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.agent_capability import SandboxCreateIn


def _payload(**overrides):
    value = {
        "project_id": 1,
        "purpose": "test",
        "language": "php",
        "test_mode": "blackbox",
        "http_scope": [
            {"method": "GET", "path": "/", "query": {}, "expected_statuses": [200]},
        ],
    }
    value.update(overrides)
    return value


def test_local_blackbox_requires_rooted_exact_get_scope() -> None:
    with pytest.raises(ValidationError, match="必须提供精确 HTTP 路径白名单"):
        SandboxCreateIn.model_validate(_payload(http_scope=None))

    with pytest.raises(ValidationError, match="必须包含不带查询参数的 GET /"):
        SandboxCreateIn.model_validate(_payload(http_scope=[
            {"method": "GET", "path": "/about.php", "query": {}, "expected_statuses": [200]},
        ]))


@pytest.mark.parametrize(
    "scope",
    [
        [{"method": "POST", "path": "/", "query": {}, "expected_statuses": [200]}],
        [{"method": "GET", "path": "/", "query": {}, "expected_statuses": [99]}],
        [{"method": "GET", "path": "/../etc/passwd", "query": {}, "expected_statuses": [200]}],
        [{"method": "GET", "path": "/%2e%2e/etc/passwd", "query": {}, "expected_statuses": [200]}],
        [{"method": "GET", "path": "/api/*", "query": {}, "expected_statuses": [200]}],
        [{"method": "GET", "path": "/", "query": {"file": "php://filter"}, "expected_statuses": [200]}],
        [{"method": "GET", "path": "/", "query": {"file": "%252e%252e%252fetc"}, "expected_statuses": [200]}],
        [{
            "method": "GET",
            "path": "/",
            "query": {"file": "%2525252e%2525252e%2525252fetc"},
            "expected_statuses": [200],
        }],
        [{"method": "GET", "path": "/", "query": {"file": "../etc/passwd"}, "expected_statuses": [200]}],
    ],
)
def test_scope_rejects_methods_paths_and_file_or_traversal_parameters(scope) -> None:
    with pytest.raises(ValidationError):
        SandboxCreateIn.model_validate(_payload(http_scope=scope))


def test_scope_rejects_duplicate_targets_more_than_twenty_and_mysql_network() -> None:
    root = {"method": "GET", "path": "/", "query": {}, "expected_statuses": [200]}
    with pytest.raises(ValidationError, match="重复请求目标"):
        SandboxCreateIn.model_validate(_payload(http_scope=[root, root]))
    with pytest.raises(ValidationError):
        SandboxCreateIn.model_validate(_payload(http_scope=[root] * 21))
    with pytest.raises(ValidationError, match="mysql"):
        SandboxCreateIn.model_validate(_payload(db_type="mysql"))


def test_scope_is_only_available_for_local_blackbox_not_combined_tests() -> None:
    with pytest.raises(ValidationError, match="仅适用于"):
        SandboxCreateIn.model_validate(_payload(test_mode="combined"))


def test_scope_is_forbidden_on_remote_whitebox_and_deploy_requests() -> None:
    with pytest.raises(ValidationError, match="仅适用于"):
        SandboxCreateIn.model_validate(_payload(remote_target_url="https://example.test"))
    with pytest.raises(ValidationError, match="仅适用于"):
        SandboxCreateIn.model_validate(_payload(test_mode="whitebox"))
    with pytest.raises(ValidationError, match="仅适用于"):
        SandboxCreateIn.model_validate(_payload(purpose="deploy", test_mode="deploy"))


def test_scope_accepts_exact_paths_query_values_and_expected_status_assertions() -> None:
    value = SandboxCreateIn.model_validate(_payload(http_scope=[
        {"method": "GET", "path": "/", "query": {}, "expected_statuses": [200]},
        {
            "method": "GET",
            "path": "/about.php",
            "query": {"page": "1", "name": "Ada-Lovelace"},
            "expected_statuses": [200],
        },
        {"method": "GET", "path": "/missing.php", "query": {}, "expected_statuses": [404]},
    ]))

    assert [item.path for item in value.http_scope or []] == ["/", "/about.php", "/missing.php"]
    assert (value.http_scope or [])[1].query == {"page": "1", "name": "Ada-Lovelace"}


def test_scope_limits_encoded_query_string_size() -> None:
    oversized_query = {f"key{index}": "a" * 512 for index in range(5)}
    with pytest.raises(ValidationError, match="编码后的查询参数"):
        SandboxCreateIn.model_validate(_payload(http_scope=[
            {"method": "GET", "path": "/", "query": {}, "expected_statuses": [200]},
            {"method": "GET", "path": "/search", "query": oversized_query, "expected_statuses": [200]},
        ]))
