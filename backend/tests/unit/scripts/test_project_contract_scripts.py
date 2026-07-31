"""项目事实生成与 OpenAPI 兼容性检查脚本测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_openapi_contract import find_breaking_changes  # noqa: E402
from scripts.generate_project_facts import (  # noqa: E402
    build_project_facts,
    render_project_facts_markdown,
)


def _openapi_document(
    *, required_query: bool = False, body_required: bool = False, required_name: bool = False
) -> dict:
    """构造覆盖参数和请求体兼容规则的最小 OpenAPI 文档。

    Args:
        required_query: 是否加入新的必填 query 参数。
        body_required: 请求体是否必填。
        required_name: 请求模型的 name 字段是否必填。

    Returns:
        dict: 可用于兼容性比较的 OpenAPI 文档。
    """
    parameters = []
    if required_query:
        parameters.append(
            {
                "name": "scope",
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
            }
        )
    required = ["name"] if required_name else []
    return {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": "1"},
        "paths": {
            "/items": {
                "post": {
                    "parameters": parameters,
                    "requestBody": {
                        "required": body_required,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ItemInput"}
                            }
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "components": {
            "schemas": {
                "ItemInput": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": required,
                }
            }
        },
    }


def test_openapi_breaking_change_rules_cover_required_inputs() -> None:
    """必填参数、必填请求体和新增必填字段都必须被识别。"""
    baseline = _openapi_document()

    parameter_changes = find_breaking_changes(baseline, _openapi_document(required_query=True))
    body_changes = find_breaking_changes(baseline, _openapi_document(body_required=True))
    schema_changes = find_breaking_changes(baseline, _openapi_document(required_name=True))

    assert any("required parameter" in item for item in parameter_changes)
    assert any("request body became required" in item for item in body_changes)
    assert any("required request field" in item for item in schema_changes)


def test_openapi_breaking_change_rules_cover_removed_operations() -> None:
    """路径删除和 HTTP 方法删除必须被识别。"""
    baseline = _openapi_document()
    missing_path = _openapi_document()
    missing_path["paths"] = {}
    missing_method = _openapi_document()
    missing_method["paths"]["/items"] = {}

    assert any("removed path" in item for item in find_breaking_changes(baseline, missing_path))
    assert any("removed method" in item for item in find_breaking_changes(baseline, missing_method))


def test_openapi_additive_optional_change_is_allowed() -> None:
    """新增可选字段不应被误判为破坏性变更。"""
    baseline = _openapi_document()
    current = _openapi_document()
    current["components"]["schemas"]["ItemInput"]["properties"]["note"] = {"type": "string"}

    assert find_breaking_changes(baseline, current) == []


def test_project_facts_match_discovered_collections() -> None:
    """事实摘要计数必须由实际明细集合推导且 Markdown 可稳定渲染。"""
    facts, _ = build_project_facts(ROOT)

    assert facts["http"]["business_route_count"] == len(facts["http"]["routes"])
    assert facts["http"]["business_route_count"] > 0
    assert facts["http"]["operation_count"] == sum(
        len(route["methods"]) for route in facts["http"]["routes"]
    )
    assert facts["http"]["websocket_route_count"] == len(facts["http"]["websockets"])
    assert facts["orm"]["table_count"] == len(facts["orm"]["tables"])
    assert facts["agents"]["count"] == len(facts["agents"]["items"])
    assert facts["frontend"]["view_count"] == len(facts["frontend"]["views"])
    assert facts["alembic"]["heads"]
    assert "# 自动生成的项目事实" in render_project_facts_markdown(facts)
