#!/usr/bin/env python3
"""Generate deterministic project facts and the current FastAPI OpenAPI document."""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}


def _configure_deterministic_environment() -> None:
    """Force safe deterministic settings before importing the application.

    The generator only inspects metadata and never needs a production database or
    secret. Environment variables intentionally take precedence over any local
    untracked ``.env`` file so generated artifacts do not depend on a workstation.
    """
    os.environ["APP_ENV"] = "dev"
    os.environ["OPENAPI_ENABLED"] = "true"
    os.environ["DB_HOST"] = "sqlite"
    os.environ["DB_NAME"] = "project_facts"
    os.environ["AGENT_GOVERNANCE_SCHEDULER_ENABLED"] = "false"
    os.environ["SKILL_SCHEDULER_ENABLED"] = "false"
    os.environ["SKILL_EVENT_TRIGGER_ENABLED"] = "false"


def _ensure_backend_import_path(root: Path) -> None:
    """Add the repository backend directory to ``sys.path`` exactly once.

    Args:
        root: Repository root directory.
    """
    backend = str(root / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)


def _canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON-compatible value with stable ordering and a final newline.

    Args:
        value: JSON-compatible object.

    Returns:
        bytes: UTF-8 encoded canonical representation.
    """
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _relative_posix(path: Path, root: Path) -> str:
    """Return a repository-relative POSIX path.

    Args:
        path: Absolute or root-relative path.
        root: Repository root directory.

    Returns:
        str: Stable forward-slash path.
    """
    return path.resolve().relative_to(root.resolve()).as_posix()


def _load_application(root: Path) -> Any:
    """Import and return the FastAPI application without running its lifespan.

    Args:
        root: Repository root directory.

    Returns:
        Any: FastAPI application instance.
    """
    _ensure_backend_import_path(root)
    from app.main import app

    return app


def _operation_name(path: str, method: str, operation: Mapping[str, Any]) -> str:
    """Recover a readable route name from an OpenAPI operation.

    FastAPI's default operation ID is ``<route-name><path>_<method>`` after
    non-word characters are replaced by underscores. Custom operation IDs are
    preserved unchanged.

    Args:
        path: OpenAPI path template.
        method: Uppercase HTTP method.
        operation: OpenAPI operation mapping.

    Returns:
        str: Route function name when recoverable, otherwise the operation ID.
    """
    operation_id = str(operation.get("operationId", ""))
    path_token = re.sub(r"\W", "_", path)
    suffix = f"{path_token}_{method.lower()}"
    if operation_id.endswith(suffix):
        return operation_id[: -len(suffix)]
    return operation_id or f"{method.lower()} {path}"


def _discover_http_facts(app: Any) -> Dict[str, Any]:
    """Collect business HTTP operations and WebSocket routes.

    HTTP operations are derived from the public OpenAPI document instead of
    directly iterating ``app.routes``. FastAPI 0.139 introduced lazy included
    routers, so direct route iteration no longer exposes nested ``APIRoute``
    instances while OpenAPI remains the stable flattened contract.

    Args:
        app: FastAPI application instance.

    Returns:
        dict[str, Any]: Sorted route facts and derived counts.
    """
    from fastapi.routing import APIWebSocketRoute

    routes: List[Dict[str, Any]] = []
    websockets: List[Dict[str, str]] = []
    openapi = app.openapi()
    for path, path_item in openapi.get("paths", {}).items():
        if not path.startswith("/api") or not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            normalized_method = method.upper()
            if normalized_method not in HTTP_METHODS or not isinstance(operation, Mapping):
                continue
            routes.append(
                {
                    "path": path,
                    "methods": [normalized_method],
                    "name": _operation_name(path, normalized_method, operation),
                }
            )

    for route in app.routes:
        if isinstance(route, APIWebSocketRoute):
            websockets.append({"path": route.path, "name": route.name})
    routes.sort(key=lambda item: (item["path"], item["methods"], item["name"]))
    websockets.sort(key=lambda item: (item["path"], item["name"]))
    return {
        "business_route_count": len(routes),
        "operation_count": sum(len(item["methods"]) for item in routes),
        "websocket_route_count": len(websockets),
        "routes": routes,
        "websockets": websockets,
    }


def _import_model_modules(root: Path) -> None:
    """Import every ORM model module so all tables populate shared metadata.

    Args:
        root: Repository root directory.
    """
    _ensure_backend_import_path(root)
    model_dir = root / "backend" / "app" / "models"
    for path in sorted(model_dir.glob("*.py")):
        if path.stem in {"__init__", "base"}:
            continue
        importlib.import_module(f"app.models.{path.stem}")


def _discover_orm_facts(root: Path) -> Dict[str, Any]:
    """Collect SQLAlchemy table and column facts from ``Base.metadata``.

    Args:
        root: Repository root directory.

    Returns:
        dict[str, Any]: Sorted ORM table metadata.
    """
    _import_model_modules(root)
    from app.core.database import Base

    tables: List[Dict[str, Any]] = []
    for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name):
        columns: List[Dict[str, Any]] = []
        for column in table.columns:
            columns.append(
                {
                    "name": column.name,
                    "type": str(column.type),
                    "nullable": bool(column.nullable),
                    "primary_key": bool(column.primary_key),
                    "foreign_keys": sorted(key.target_fullname for key in column.foreign_keys),
                }
            )
        tables.append({"name": table.name, "column_count": len(columns), "columns": columns})
    return {"table_count": len(tables), "tables": tables}


def _discover_agent_facts(root: Path) -> Dict[str, Any]:
    """Collect live Agent registry metadata after deterministic registration.

    Args:
        root: Repository root directory.

    Returns:
        dict[str, Any]: Sorted Agent metadata and count.
    """
    _ensure_backend_import_path(root)
    from loguru import logger

    from app.agents.orchestrator import get_orchestrator
    from app.agents.registry import AgentRegistry

    logger.disable("app.agents")
    try:
        get_orchestrator()
    finally:
        logger.enable("app.agents")
    items: List[Dict[str, Any]] = []
    for agent in AgentRegistry.instance().list_runtime():
        skills = sorted(
            {
                str(skill.get("name", "")) if isinstance(skill, dict) else str(skill)
                for skill in agent.get("skills", [])
                if skill
            }
        )
        items.append(
            {
                "code": agent["code"],
                "name": agent.get("name", agent["code"]),
                "category": agent.get("category", "general"),
                "description": agent.get("description", ""),
                "skills": skills,
            }
        )
    items.sort(key=lambda item: item["code"])
    return {"count": len(items), "items": items}


def _assignment_literal(module: ast.Module, name: str) -> Any:
    """Read a top-level assignment or annotated assignment literal from an AST.

    Args:
        module: Parsed Python module AST.
        name: Variable name to locate.

    Returns:
        Any: Literal value, or ``None`` when no supported assignment exists.
    """
    for node in module.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            if node.value is not None:
                return ast.literal_eval(node.value)
    return None


def _normalize_down_revisions(value: Any) -> List[str]:
    """Normalize Alembic ``down_revision`` values into a sorted string list.

    Args:
        value: None, a revision string, or an iterable of revision strings.

    Returns:
        list[str]: Normalized predecessor revisions.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return sorted(str(item) for item in value if item is not None)
    return [str(value)]


def _discover_alembic_facts(root: Path) -> Dict[str, Any]:
    """Parse Alembic revision files and derive current graph heads without a DB.

    Args:
        root: Repository root directory.

    Returns:
        dict[str, Any]: Revision list, migration count, and graph heads.
    """
    revisions: List[Dict[str, Any]] = []
    version_dir = root / "backend" / "alembic" / "versions"
    for path in sorted(version_dir.glob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        revision = _assignment_literal(module, "revision")
        if revision is None:
            continue
        down_revisions = _normalize_down_revisions(_assignment_literal(module, "down_revision"))
        revisions.append(
            {
                "revision": str(revision),
                "down_revisions": down_revisions,
                "file": _relative_posix(path, root),
            }
        )
    revisions.sort(key=lambda item: item["revision"])
    predecessors = {down for item in revisions for down in item["down_revisions"]}
    heads = sorted(item["revision"] for item in revisions if item["revision"] not in predecessors)
    return {"migration_count": len(revisions), "heads": heads, "revisions": revisions}


def _discover_source_facts(root: Path) -> Dict[str, Any]:
    """Collect deterministic source module, page, and test file inventories.

    Args:
        root: Repository root directory.

    Returns:
        dict[str, Any]: Frontend and code/test inventories.
    """
    views = sorted(_relative_posix(path, root) for path in (root / "frontend" / "src" / "views").rglob("*.vue"))
    backend_modules = sorted(
        _relative_posix(path, root) for path in (root / "backend" / "app").rglob("*.py")
    )
    backend_tests = sorted(
        _relative_posix(path, root) for path in (root / "backend" / "tests").rglob("test_*.py")
    )
    frontend_tests = sorted(
        _relative_posix(path, root)
        for pattern in ("*.test.ts", "*.spec.ts", "*.test.tsx", "*.spec.tsx")
        for path in (root / "frontend" / "src").rglob(pattern)
    )
    return {
        "frontend": {"view_count": len(views), "views": views},
        "code": {
            "backend_python_module_count": len(backend_modules),
            "backend_python_modules": backend_modules,
            "backend_test_file_count": len(backend_tests),
            "backend_test_files": backend_tests,
            "frontend_test_file_count": len(frontend_tests),
            "frontend_test_files": frontend_tests,
        },
    }


def build_project_facts(root: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build all deterministic facts plus the current OpenAPI document.

    Args:
        root: Repository root directory.

    Returns:
        tuple[dict[str, Any], dict[str, Any]]: Project facts and OpenAPI data.
    """
    _configure_deterministic_environment()
    app = _load_application(root)
    source_facts = _discover_source_facts(root)
    facts: Dict[str, Any] = {
        "schema_version": 1,
        "http": _discover_http_facts(app),
        "orm": _discover_orm_facts(root),
        "agents": _discover_agent_facts(root),
        "alembic": _discover_alembic_facts(root),
        "frontend": source_facts["frontend"],
        "code": source_facts["code"],
    }
    return facts, app.openapi()


def _markdown_cell(value: Any) -> str:
    """Escape a value for a single Markdown table cell.

    Args:
        value: Cell value.

    Returns:
        str: Escaped cell text.
    """
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_project_facts_markdown(facts: Mapping[str, Any]) -> str:
    """Render human-readable Markdown from generated project facts.

    Args:
        facts: Project facts mapping returned by :func:`build_project_facts`.

    Returns:
        str: Deterministic Markdown document.
    """
    http = facts["http"]
    orm = facts["orm"]
    agents = facts["agents"]
    frontend = facts["frontend"]
    code = facts["code"]
    alembic = facts["alembic"]
    lines = [
        "# 自动生成的项目事实",
        "",
        "> 本文件由 `scripts/generate_project_facts.py` 生成，请勿手工修改。",
        "",
        "## 摘要",
        "",
        "| 事实 | 数量/值 |",
        "| --- | ---: |",
        f"| 业务 HTTP 路由 | {http['business_route_count']} |",
        f"| HTTP 操作 | {http['operation_count']} |",
        f"| WebSocket 路由 | {http['websocket_route_count']} |",
        f"| ORM 表 | {orm['table_count']} |",
        f"| Agent | {agents['count']} |",
        f"| Vue 页面 | {frontend['view_count']} |",
        f"| 后端 Python 模块 | {code['backend_python_module_count']} |",
        f"| 后端测试文件 | {code['backend_test_file_count']} |",
        f"| 前端测试文件 | {code['frontend_test_file_count']} |",
        f"| Alembic 迁移 | {alembic['migration_count']} |",
        f"| Alembic head | {', '.join(alembic['heads'])} |",
        "",
        "## HTTP 路由",
        "",
        "| 方法 | 路径 | 名称 |",
        "| --- | --- | --- |",
    ]
    for route in http["routes"]:
        lines.append(
            f"| {_markdown_cell(', '.join(route['methods']))} | "
            f"`{_markdown_cell(route['path'])}` | {_markdown_cell(route['name'])} |"
        )
    lines.extend(["", "## WebSocket", "", "| 路径 | 名称 |", "| --- | --- |"])
    for route in http["websockets"]:
        lines.append(f"| `{_markdown_cell(route['path'])}` | {_markdown_cell(route['name'])} |")
    lines.extend(["", "## ORM 表", "", "| 表 | 列数 |", "| --- | ---: |"])
    for table in orm["tables"]:
        lines.append(f"| `{_markdown_cell(table['name'])}` | {table['column_count']} |")
    lines.extend(["", "## Agent", "", "| Code | 分类 | Skill 数 | 描述 |", "| --- | --- | ---: | --- |"])
    for agent in agents["items"]:
        lines.append(
            f"| `{_markdown_cell(agent['code'])}` | {_markdown_cell(agent['category'])} | "
            f"{len(agent['skills'])} | {_markdown_cell(agent['description'])} |"
        )
    lines.extend(["", "## Vue 页面", ""])
    lines.extend(f"- `{_markdown_cell(path)}`" for path in frontend["views"])
    lines.extend(["", "## Alembic 迁移", "", "| Revision | Down revision | 文件 |", "| --- | --- | --- |"])
    for revision in alembic["revisions"]:
        down = ", ".join(revision["down_revisions"]) or "-"
        lines.append(
            f"| `{_markdown_cell(revision['revision'])}` | `{_markdown_cell(down)}` | "
            f"`{_markdown_cell(revision['file'])}` |"
        )
    return "\n".join(lines) + "\n"


def _artifact_payloads(root: Path) -> Dict[Path, bytes]:
    """Build all generated artifact paths and exact byte payloads.

    Args:
        root: Repository root directory.

    Returns:
        dict[Path, bytes]: Output path to canonical content mapping.
    """
    facts, openapi = build_project_facts(root)
    generated = root / "docs" / "generated"
    return {
        generated / "project-facts.json": _canonical_json_bytes(facts),
        generated / "PROJECT_FACTS.md": render_project_facts_markdown(facts).encode("utf-8"),
        generated / "openapi.json": _canonical_json_bytes(openapi),
    }


def _write_or_check(payloads: Mapping[Path, bytes], check: bool) -> int:
    """Write generated artifacts or verify existing files byte-for-byte.

    Args:
        payloads: Output path and expected bytes mapping.
        check: When true, do not write and fail on drift.

    Returns:
        int: Process-style status code.
    """
    drift: List[str] = []
    for path, expected in payloads.items():
        if check:
            if not path.exists():
                drift.append(f"missing: {path}")
            elif path.read_bytes() != expected:
                drift.append(f"drift: {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)
        print(f"generated: {path}")
    if drift:
        for message in drift:
            print(message, file=sys.stderr)
        print("run scripts/generate_project_facts.py to refresh generated artifacts", file=sys.stderr)
        return 1
    if check:
        print("project facts check: PASS")
    return 0


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line options.

    Args:
        argv: Optional explicit argument sequence.

    Returns:
        argparse.Namespace: Parsed options.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when generated files are missing or stale")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the script parent)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Generate or verify repository fact artifacts.

    Args:
        argv: Optional explicit argument sequence.

    Returns:
        int: Process exit code.
    """
    args = _parse_args(argv)
    root = args.root.resolve()
    return _write_or_check(_artifact_payloads(root), args.check)


if __name__ == "__main__":
    raise SystemExit(main())
