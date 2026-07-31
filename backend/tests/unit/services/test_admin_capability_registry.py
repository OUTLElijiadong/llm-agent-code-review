"""管理页面、OpenAPI 与 Responses 能力注册表契约测试。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.main import app
from app.services.admin_capability_registry import (
    ADMIN_CAPABILITIES,
    ADMIN_PAGE_ROUTES,
    CAPABILITY_BY_CODE,
    READ,
    describe_capabilities,
    discovery_tool_schema,
    execution_tool_schema,
    operation_contract,
)
from app.services.admin_capability_service import AdminCapabilityError, prepare_request

REPO_ROOT = Path(__file__).resolve().parents[4]

FRONTEND_API_CAPABILITY = {
    "adminOverview:getSystemStatus": "overview.system",
    "adminOverview:getSecurityPosture": "overview.security",
    "adminOverview:getLoginGeo": "overview.geo",
    "adminOverview:getAgentsActivity": "overview.agent_activity",
    "adminGovernance:getGovernanceOverview": "governance.overview",
    "adminGovernance:listGovernanceAgents": "governance.agents.list",
    "adminGovernance:listAgentMemory": "knowledge.memory.list",
    "adminGovernance:createAgentMemory": "knowledge.memory.create",
    "adminGovernance:listAgentKnowledge": "knowledge.docs.list",
    "adminGovernance:createAgentKnowledgeDoc": "knowledge.docs.create",
    "adminGovernance:activateAgentKnowledgeDoc": "knowledge.docs.activate",
    "adminGovernance:listAgentKnowledgeSources": "knowledge.sources.list",
    "adminGovernance:upsertAgentKnowledgeSource": "knowledge.sources.upsert",
    "adminGovernance:crawlAgentKnowledgeSources": "knowledge.sources.crawl",
    "adminGovernance:listApprovals": "approvals.list",
    "adminGovernance:approveItem": "approvals.approve",
    "adminGovernance:rejectItem": "approvals.reject",
    "adminGovernance:listPolicies": "policies.list",
    "adminGovernance:upsertPolicy": "policies.upsert",
    "adminGovernance:evaluatePolicy": "policies.evaluate",
    "adminGovernance:listPolicyDecisions": "policies.decisions.list",
    "adminGovernance:listToolCalls": "tools.calls.list",
    "adminGovernance:listToolPermissions": "tools.permissions.list",
    "adminGovernance:upsertToolPermission": "tools.permissions.upsert",
    "adminGovernance:listJobs": "jobs.list",
    "adminGovernance:runJob": "jobs.run",
    "adminGovernance:updateJob": "jobs.update",
    "adminGovernance:getObservabilityOverview": "observability.overview",
    "adminGovernance:listAlerts": "observability.alerts.list",
    "adminGovernance:resolveAlert": "observability.alerts.resolve",
    "adminGovernance:listRewardEvents": "rewards.events.list",
    "adminGovernance:createRewardEvent": "rewards.events.create",
    "adminGovernance:listArtifactVersions": "rollback.versions.list",
    "adminGovernance:createArtifactVersion": "rollback.versions.create",
    "adminGovernance:rollbackArtifactVersion": "rollback.versions.rollback",
    "agentStudio:listAgentReleaseApprovals": "agent_releases.approvals.list",
    "agentStudio:listAdminAgentReleases": "agent_releases.agents.list",
    "agentStudio:approveAgentRelease": "agent_releases.approve",
    "agentStudio:rejectAgentRelease": "agent_releases.reject",
    "agentStudio:reviseAgentRelease": "agent_releases.revise",
    "agentStudio:disableCustomAgent": "agent_releases.disable",
    "agentStudio:rollbackCustomAgent": "agent_releases.rollback",
    "betaCode:listBetaCodes": "beta_codes.list",
    "betaCode:generateBetaCodes": "beta_codes.generate",
    "betaCode:revokeBetaCode": "beta_codes.revoke",
    "user:getUsers": "users.list",
    "user:resetPassword": "users.reset_password",
    "user:toggleUserStatus": "users.toggle_status",
    "user:setUserRole": "users.set_legacy_role",
    "user:deleteUser": "users.delete",
    "rbac:listRoles": "rbac.roles.list",
    "rbac:createRole": "rbac.roles.create",
    "rbac:updateRole": "rbac.roles.update",
    "rbac:deleteRole": "rbac.roles.delete",
    "rbac:listPermissions": "rbac.permissions.list",
    "rbac:fetchRolePermissions": "rbac.roles.permissions.get",
    "rbac:assignRolePermissions": "rbac.roles.permissions.assign",
    "rbac:fetchRoleDataScope": "rbac.roles.data_scope.get",
    "rbac:updateRoleDataScope": "rbac.roles.data_scope.update",
    "rbac:fetchUsersByRole": "rbac.roles.users",
    "rbac:fetchUserRoles": "rbac.users.roles.get",
    "rbac:assignUserRoles": "rbac.users.roles.assign",
    "project:getProjects": "rbac.roles.projects.list",
    "aiLog:getAiLogs": "ai_logs.list",
    "aiLog:getAiLogDetail": "ai_logs.get",
    "audit:listAuditLogs": "audit.list",
    "evolution:getFeedback": "evolution.feedback",
    "evolution:listExperiences": "evolution.experiences",
    "evolution:listEvalCases": "evolution.eval_cases",
    "evolution:runEvolution": "evolution.run",
    "evolution:triggerEvolution": "evolution.trigger",
    "evolution:listProposals": "evolution.proposals.list",
    "evolution:evaluateProposal": "evolution.proposals.evaluate",
    "evolution:approveProposal": "evolution.proposals.approve",
    "evolution:rejectProposal": "evolution.proposals.reject",
    "evolution:rollbackProposal": "evolution.proposals.rollback",
    "agent:listRuntimeAgents": "skills.agents.list",
    "agent:listAgentSkills": "skills.list",
    "agent:invokeAgentSkill": "skills.invoke",
    "agent:listSkillRecords": "skills.records.list",
    "knowledge:getEmbeddingConfig": "embedding.config.get",
    "knowledge:updateEmbeddingConfig": "embedding.config.update",
    "llmConfig:getLlmConfig": "llm.config.get",
    "llmConfig:updateLlmConfig": "llm.config.update",
    "llmConfig:testLlmConfig": "llm.config.test",
    "report:listTemplates": "report_templates.list",
    "report:createTemplate": "report_templates.create",
    "report:updateTemplate": "report_templates.update",
    "report:deleteTemplate": "report_templates.delete",
}


def _frontend_admin_routes() -> set[str]:
    source = (REPO_ROOT / "frontend/src/router/index.ts").read_text(encoding="utf-8")
    admin_block = source.split("path: '/admin'", 1)[1].split("path: '/403'", 1)[0]
    children = set(re.findall(r"\bpath:\s*'([^']*)'", admin_block))
    return {f"/admin/{path}" for path in children if path}


def _admin_menu_routes() -> set[str]:
    source = (REPO_ROOT / "frontend/src/components/admin/AdminLayout.vue").read_text(encoding="utf-8")
    return set(re.findall(r"\{\s*path:\s*'(/admin/[^']+)'", source))


def _admin_view_api_imports() -> set[str]:
    views = list((REPO_ROOT / "frontend/src/views/admin").glob("*.vue"))
    views.append(REPO_ROOT / "frontend/src/views/report/ReportTemplateManage.vue")
    imported: set[str] = set()
    pattern = re.compile(r"import\s*\{([^}]+)\}\s*from\s*'@/api/([^']+)'", re.DOTALL)
    for path in views:
        source = path.read_text(encoding="utf-8")
        for names, module in pattern.findall(source):
            for raw_name in names.split(","):
                name = raw_name.strip()
                if not name or name.startswith("type "):
                    continue
                imported.add(f"{module}:{name.split(' as ', 1)[0].strip()}")
    return imported


def test_every_admin_route_and_menu_entry_has_agent_capabilities() -> None:
    expected = set(ADMIN_PAGE_ROUTES)
    assert _frontend_admin_routes() == expected
    assert _admin_menu_routes() == expected

    mapped_pages = {spec.page for spec in ADMIN_CAPABILITIES}
    assert mapped_pages == expected
    for page in expected:
        assert any(spec.page == page for spec in ADMIN_CAPABILITIES), page


def test_all_registered_capabilities_bind_existing_openapi_operations() -> None:
    openapi = app.openapi()
    assert len(ADMIN_CAPABILITIES) == 97
    assert len(CAPABILITY_BY_CODE) == len(ADMIN_CAPABILITIES)
    for spec in ADMIN_CAPABILITIES:
        contract = operation_contract(spec, openapi)
        assert contract.schema["type"] == "object"
        assert contract.schema["additionalProperties"] is False
        if spec.method == "GET":
            assert spec.risk == READ


def test_every_api_function_imported_by_admin_views_maps_to_a_capability() -> None:
    imported = _admin_view_api_imports()
    assert imported == set(FRONTEND_API_CAPABILITY)
    for api_function, capability in FRONTEND_API_CAPABILITY.items():
        assert capability in CAPABILITY_BY_CODE, api_function


def test_discovery_returns_exact_page_contracts() -> None:
    rows = describe_capabilities(app.openapi(), page="/admin/llm")
    assert {row["capability"] for row in rows} == {
        "llm.config.get",
        "llm.config.update",
        "llm.config.test",
    }
    update = next(row for row in rows if row["capability"] == "llm.config.update")
    assert "api_key" in update["parameters"]["properties"]
    assert update["risk"] == "critical"


def test_prepare_request_separates_path_query_and_body() -> None:
    openapi = app.openapi()
    spec = CAPABILITY_BY_CODE["rbac.roles.data_scope.update"]
    path, query, body = prepare_request(
        spec,
        {"role_id": 7, "scope_type": "custom", "project_ids": [11, 12]},
        openapi,
    )
    assert path == "/api/rbac/roles/7/data-scope"
    assert query == {}
    assert body == {"scope_type": "custom", "project_ids": [11, 12]}

    with pytest.raises(AdminCapabilityError, match="不接受参数"):
        prepare_request(spec, {"role_id": 7, "scope_type": "all", "shell": "id"}, openapi)
    with pytest.raises(AdminCapabilityError, match="缺少必填参数"):
        prepare_request(spec, {"role_id": 7}, openapi)


def test_tool_schemas_expose_only_registry_codes_not_http_path_or_method() -> None:
    discovery = discovery_tool_schema()
    execution = execution_tool_schema()
    assert discovery["name"] == "admin_describe_capabilities"
    assert execution["name"] == "admin_execute_capability"
    properties = execution["parameters"]["properties"]
    assert set(properties) == {"capability", "params"}
    assert set(properties["capability"]["enum"]) == set(CAPABILITY_BY_CODE)
