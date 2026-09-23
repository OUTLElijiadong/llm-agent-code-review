"""Route discovery contracts in both flat and included-router FastAPI runtimes."""

import copy
import importlib.util
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

PATH = Path(__file__).resolve().parents[1] / "scripts/verify_permission_acceptance_https.py"
SPEC = importlib.util.spec_from_file_location("permission_route_contexts_runner", PATH)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def test_complete_real_app_including_hidden_endpoints_without_lifespan(monkeypatch):
    from app.main import app

    monkeypatch.setattr(app, "openapi", lambda: pytest.fail("OpenAPI cannot prove authorization"))
    plan = m.build_plan()
    assert len(plan["routes"]) == 332
    assert len({row["endpoint"] for row in plan["routes"]}) == 42
    paths = {(row["method"], row["path"]) for row in plan["routes"]}
    assert {
        ("POST", "/api/auth/login"),
        ("POST", "/v1/responses"),
        ("POST", "/api/discuss/start"),
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("GET", "/metrics"),
        ("GET", "/api/admin/llm/models/registry"),
        ("PUT", "/api/admin/llm/models/assignments"),
        ("GET", "/api/me/profile"),
        ("POST", "/api/me/profile/preference-prompted"),
    } <= paths
    assert sum(row["anonymous"] == "ready" for row in plan["routes"]) == 318
    assert sum(row["no_permission"] == "ready" for row in plan["routes"]) == 251
    private_assets = {
        "/api/agent-responses/runs/{run_id}/assets",
        "/api/agent-responses/assets/{asset_id}/image",
    }
    asset_routes = [row for row in plan["routes"] if row["path"] in private_assets]
    assert {row["path"] for row in asset_routes} == private_assets
    assert all(row["method"] == "GET" and row["anonymous"] == row["no_permission"] == "ready"
               for row in asset_routes)


@pytest.mark.parametrize(
    "kind", ["tiny", "missing_one", "duplicate", "changed_dependency", "changed_ready", "missing_source", "middleware"]
)
def test_plan_rejects_partial_or_modified_runtime_evidence(kind):
    plan = m.build_plan()
    if kind == "tiny":
        plan["routes"] = plan["routes"][-3:]
    if kind == "missing_one":
        plan["routes"].pop()
    if kind == "duplicate":
        plan["routes"].append(copy.deepcopy(plan["routes"][0]))
    if kind == "changed_dependency":
        plan["routes"][0]["dependency_order"] = ["app.core.database.get_db"]
    if kind == "changed_ready":
        next(row for row in plan["routes"] if row["anonymous"] == "ready")["anonymous"] = "blocked"
    if kind == "missing_source":
        plan["source_sha256"].pop("app/core/security.py")
    if kind == "middleware":
        plan["middleware"] = []
    # Recomputing a public digest must not legitimize changed dependency evidence.
    if kind in {"changed_dependency", "changed_ready"} and hasattr(m, "route_inventory"):
        plan["inventory"] = m.route_inventory(plan["routes"])
    with pytest.raises(ValueError):
        m.validate_plan(plan, PATH.parents[1])


def test_nested_context_keeps_prefix_parent_dependencies_and_real_execution_order():
    observed = []

    def app_dep():
        observed.append("app")

    def outer_dep():
        observed.append("outer")

    def include_dep():
        observed.append("include")

    def inner_dep():
        observed.append("inner")

    def route_dep():
        observed.append("route")

    def child_dep():
        observed.append("child")

    def parameter_dep(_value=Depends(child_dep)):
        observed.append("parameter")

    app = FastAPI(dependencies=[Depends(app_dep)])
    outer = APIRouter(prefix="/outer", dependencies=[Depends(outer_dep)])
    inner = APIRouter(prefix="/inner", dependencies=[Depends(inner_dep)])

    @inner.get("/items/{item_id}", dependencies=[Depends(route_dep)], include_in_schema=False)
    def endpoint(item_id: int, _value=Depends(parameter_dep)):
        observed.append("endpoint")
        return {"id": item_id}

    outer.include_router(inner, prefix="/nested", dependencies=[Depends(include_dep)])
    app.include_router(outer, prefix="/api")
    contexts = list(m.iter_api_route_contexts(app.routes))
    assert len(contexts) == 1
    route = contexts[0]
    assert route.path == "/api/outer/nested/inner/items/{item_id}"

    def ordered(dependant):
        for child in dependant.dependencies:
            yield from ordered(child)
        yield dependant.call

    calls = list(ordered(route.dependant))
    assert calls == [app_dep, outer_dep, include_dep, inner_dep, route_dep, child_dep, parameter_dep, endpoint]
    assert observed == []  # Introspection never executes dependencies/endpoints.
    with TestClient(app) as client:
        assert client.get("/api/outer/nested/inner/items/7").json() == {"id": 7}
    assert observed == ["app", "outer", "include", "inner", "route", "child", "parameter", "endpoint"]


def test_repeated_router_inclusion_keeps_distinct_paths():
    router = APIRouter()

    @router.get("/items")
    def endpoint():
        return {}

    app = FastAPI()
    app.include_router(router, prefix="/one")
    app.include_router(router, prefix="/two")
    assert [route.path for route in m.iter_api_route_contexts(app.routes)] == ["/one/items", "/two/items"]


def test_incomplete_discovery_is_rejected_during_generation(monkeypatch):
    from app.main import app

    rows = list(m.iter_api_route_contexts(app.routes))[-3:]
    monkeypatch.setattr(m, "iter_api_route_contexts", lambda _routes, **_kwargs: iter(rows))
    with pytest.raises(ValueError, match="完整性"):
        m.build_plan()


def test_unknown_tree_node_or_mount_cannot_be_silently_skipped():
    from starlette.routing import Mount

    app = FastAPI()
    with pytest.raises(ValueError, match="尚未支持"):
        list(m.iter_api_route_contexts([Mount("/sub", app=app)]))


def test_legacy_schema_requires_regeneration():
    plan = m.build_plan()
    plan["schema"] = 1
    with pytest.raises(ValueError, match="重新生成"):
        m.validate_plan(plan, PATH.parents[1])


def test_direct_runner_small_plan_never_logs_in_or_sends_requests(monkeypatch, tmp_path):
    runner = m.Runner("https://example.invalid", {"marker": "local"}, {}, tmp_path / "log.json", phase="matrix")
    monkeypatch.setattr(runner, "login", lambda _account: pytest.fail("invalid plan reached login"))
    with pytest.raises(ValueError):
        runner.run()
    assert runner.log["requests"] == [] and runner.log["status"] != "passed"


def test_flat_fallback_retains_direct_route_and_rejects_unknown_node(monkeypatch):
    from fastapi import routing

    def endpoint():
        return {}

    route = routing.APIRoute("/direct", endpoint)
    monkeypatch.delattr(routing, "iter_route_contexts", raising=False)
    assert list(m.iter_api_route_contexts([route])) == [route]
    with pytest.raises(ValueError, match="尚未支持"):
        list(m.iter_api_route_contexts([object()]))


def test_unknown_inherited_dependency_blocks_both_roles_without_execution(monkeypatch):
    import app.main as main_module
    from app.core.dependencies import get_current_user

    observed = []

    def unreviewed_parent():
        observed.append("would_run_before_auth")

    fixture = FastAPI()
    fixture.router.routes.extend(main_module.app.routes)
    fixture.user_middleware = list(main_module.app.user_middleware)
    router = APIRouter(dependencies=[Depends(unreviewed_parent)])

    @router.get("/unreviewed-parent")
    def endpoint(_user=Depends(get_current_user)):
        observed.append("endpoint")

    fixture.include_router(router, prefix="/api")
    monkeypatch.setattr(main_module, "app", fixture)
    plan = m.build_plan()
    row = next(row for row in plan["routes"] if row["path"] == "/api/unreviewed-parent")
    assert row["anonymous"] == row["no_permission"] == "blocked"
    assert row["dependency_order"][0].endswith("unreviewed_parent")
    assert row["unknown_dependencies"] == [row["dependency_order"][0]]
    assert observed == []


@pytest.mark.parametrize("spoof_doc_name", [False, True])
def test_business_starlette_http_route_cannot_disappear_from_complete_plan(monkeypatch, spoof_doc_name):
    from starlette.routing import Route

    import app.main as main_module

    async def business(request):
        pytest.fail("discovery executed business endpoint")

    if spoof_doc_name:
        business.__module__ = "fastapi.applications"
        business.__qualname__ = "FastAPI.setup.<locals>.openapi"
    fixture = FastAPI()
    fixture.router.routes.extend(main_module.app.routes)
    fixture.user_middleware = list(main_module.app.user_middleware)
    fixture.router.routes.append(Route("/api/uncovered-business", business, methods=["GET"]))
    monkeypatch.setattr(main_module, "app", fixture)
    with pytest.raises(ValueError, match="非APIRoute"):
        m.build_plan()


def test_existing_websocket_is_explicitly_unverified_and_source_bound():
    plan = m.build_plan()
    websocket = [row for row in plan["excluded_routes"] if row["kind"] == "websocket"]
    assert websocket == [
        {
            "kind": "websocket",
            "path": "/api/ws/discuss/{session_id}",
            "endpoint": "app.api.v1.ws_discussion.ws_discuss",
            "source": "app/api/v1/ws_discussion.py",
            "status": "not_tested",
            "reason": "HTTP权限矩阵不建立WebSocket连接，需独立验收",
        }
    ]
    assert websocket[0]["source"] in plan["source_sha256"]
    plan["excluded_routes"] = []
    with pytest.raises(ValueError, match="完整路由"):
        m.validate_plan(plan, PATH.parents[1])


def test_studio_guard_is_source_bound_and_all_studio_routes_are_negative_tested():
    plan = m.build_plan()
    studio = [row for row in plan["routes"] if row["endpoint"] == "app/api/v1/agent_studio.py"]
    assert len(studio) == 15
    assert all(row["anonymous"] == row["no_permission"] == "ready" for row in studio)
    assert "app/services/agent_studio_service.py" in plan["source_sha256"]
    assert all(not row["unknown_dependencies"] for row in studio)
