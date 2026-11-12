"""企业草稿只属于当前企业、当前作者，绝不自动进入旧全局目录。"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.enterprise_assets import platform_publication_router, publication_router, router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AppError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.billing_ledger import (
    BillingOrder,
    BillingPaymentEvent,
    BillingRefund,
    BillingSubscription,
    BillingSubscriptionPeriod,
)
from app.models.custom_agent import CustomAgent, CustomAgentRelease, CustomAgentVersion, CustomSkill
from app.models.enterprise_asset import (
    EnterpriseAssetDraft,
    EnterpriseAssetDraftVersion,
    EnterpriseAssetPublication,
    EnterpriseAssistantAssetInvocation,
)
from app.models.enterprise_org import Enterprise, EnterpriseAuditEvent, EnterpriseMember
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import agent_studio_service
from app.services import enterprise_asset_service as assets


@pytest.fixture
def assetdb():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [User, Enterprise, EnterpriseMember, EnterpriseAuditEvent, EnterpriseAssetDraft,
              EnterpriseAssetDraftVersion, EnterpriseAssetPublication, CustomAgent, CustomAgentVersion,
              CustomAgentRelease, CustomSkill, Role, UserRole,
              BillingOrder, BillingPaymentEvent, BillingSubscription, BillingSubscriptionPeriod]
    tables.append(BillingRefund)
    Base.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add_all([User(id=1, username="super", password="x", role="super_admin", status=1),
                User(id=2, username="worker", password="x", role="user", status=1),
                User(id=3, username="colleague", password="x", role="user", status=1),
                User(id=4, username="other", password="x", role="user", status=1)])
    db.add_all([Enterprise(id=10, name="甲", owner_id=1, seat_limit=3, status="active", paid_order_id="a"),
                Enterprise(id=20, name="乙", owner_id=4, seat_limit=3, status="active", paid_order_id="b")])
    db.add_all([EnterpriseMember(enterprise_id=10, user_id=i, role="owner" if i == 1 else "member", status="active")
                for i in (1, 2, 3)])
    db.add(EnterpriseMember(enterprise_id=20, user_id=4, role="owner", status="active"))
    db.commit()
    yield db
    db.close()
    engine.dispose()


def test_draft_versions_are_private_and_do_not_enter_global_assets(assetdb):
    draft = assets.create_draft(assetdb, 10, 2, kind="agent", title="内部代理",
                                definition={"prompt": "仅作私有草稿"}, request_key="create-1")
    assert assets.create_draft(assetdb, 10, 2, kind="agent", title="内部代理",
                               definition={"prompt": "仅作私有草稿"}, request_key="create-1").id == draft.id
    with pytest.raises(ConflictError):
        assets.create_draft(assetdb, 10, 2, kind="agent", title="不同名称", definition={}, request_key="create-1")
    assert [row.id for row in assets.list_drafts(assetdb, 10, 2)] == [draft.id]
    assert assets.list_drafts(assetdb, 10, 3) == []
    for user_id in (1, 3):
        with pytest.raises(NotFoundError):
            assets.get_draft(assetdb, 10, user_id, draft.id)
        with pytest.raises(NotFoundError):
            assets.list_versions(assetdb, 10, user_id, draft.id)
        with pytest.raises(NotFoundError):
            assets.revise_draft(assetdb, 10, user_id, draft.id, title="窃改", definition={},
                                expected_version=1, request_key="unauthorized")
        with pytest.raises(NotFoundError):
            assets.delete_draft(assetdb, 10, user_id, draft.id)
    with pytest.raises(ForbiddenError):
        assets.get_draft(assetdb, 10, 4, draft.id)
    with pytest.raises(NotFoundError):
        assets.get_draft(assetdb, 20, 4, draft.id)
    assert assetdb.query(CustomAgent).count() == 0
    assert assetdb.query(CustomSkill).count() == 0
    assert agent_studio_service.list_catalog(assetdb) == []


def test_global_catalog_cursor_keeps_older_items_reachable_when_new_item_arrives(assetdb):
    draft = assets.create_draft(assetdb, 10, 2, kind="agent", title="目录资产",
                                definition={"prompt": "示例"}, request_key="cursor-draft")
    version = assetdb.query(EnterpriseAssetDraftVersion).filter_by(draft_id=draft.id).one()
    digest = assets._content_hash(draft.kind, version.title, version.definition_json)

    def add_publication(index):
        row = EnterpriseAssetPublication(
            enterprise_id=10, draft_id=draft.id, version_id=version.id, version_number=1,
            requested_by=2, kind="agent", scope="global", status="approved", title=version.title,
            definition_json=version.definition_json, content_sha256=digest, request_key=f"cursor-{index}",
            request_digest=assets._digest("global", 10, 2, draft.id, version.id, 1, digest),
        )
        assetdb.add(row)
        assetdb.flush()
        return row

    rows = [add_publication(index) for index in range(101)]
    assetdb.commit()
    first = assets.list_global_catalog(assetdb, limit=100)
    assert len(first) == 100 and rows[0].id not in {row.id for row in first}
    add_publication(102)
    rows[0].status = "revoked"
    assetdb.commit()
    second = assets.list_global_catalog(assetdb, limit=100, before_id=first[-1].id)
    assert second == []  # the remaining older row was revoked
    rows[0].status = "approved"
    assetdb.commit()
    second = assets.list_global_catalog(assetdb, limit=100, before_id=first[-1].id)
    assert [row.id for row in second] == [rows[0].id]
    assert not ({row.id for row in first} & {row.id for row in second})


def test_revision_idempotency_conflicts_and_membership_revocation(assetdb):
    row = assets.create_draft(assetdb, 10, 2, kind="skill", title="私有技能",
                              definition={"prompt": "v1"}, request_key="create-2")
    version = assets.revise_draft(assetdb, 10, 2, row.id, title="私有技能", definition={"prompt": "v2"},
                                  expected_version=1, request_key="revision-1")
    replay = assets.revise_draft(assetdb, 10, 2, row.id, title="私有技能", definition={"prompt": "v2"},
                                 expected_version=1, request_key="revision-1")
    assert version.id == replay.id and version.version_number == 2
    with pytest.raises(ConflictError):
        assets.revise_draft(assetdb, 10, 2, row.id, title="私有技能", definition={"prompt": "different"},
                            expected_version=1, request_key="revision-1")
    with pytest.raises(ConflictError):
        assets.revise_draft(assetdb, 10, 2, row.id, title="私有技能", definition={},
                            expected_version=1, request_key="other")
    assert [v.version_number for v in assets.list_versions(assetdb, 10, 2, row.id)] == [2, 1]
    membership = assetdb.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    membership.status = "removed"
    assetdb.commit()
    with pytest.raises(ForbiddenError):
        assets.list_drafts(assetdb, 10, 2)
    with pytest.raises(ForbiddenError):
        assets.get_draft(assetdb, 10, 2, row.id)
    with pytest.raises(ForbiddenError):
        assets.revise_draft(assetdb, 10, 2, row.id, title="私有技能", definition={},
                            expected_version=2, request_key="revision-2")


def test_deleted_or_invalid_drafts_cannot_be_replayed_or_modified(assetdb):
    row = assets.create_draft(assetdb, 10, 2, kind="skill", title="待删除", definition={}, request_key="create-3")
    assets.delete_draft(assetdb, 10, 2, row.id)
    with pytest.raises(ConflictError):
        assets.create_draft(assetdb, 10, 2, kind="skill", title="待删除", definition={}, request_key="create-3")
    with pytest.raises(NotFoundError):
        assets.get_draft(assetdb, 10, 2, row.id)
    with pytest.raises(ValidationError):
        assets.create_draft(assetdb, 10, 2, kind="agent", title="合法", definition={}, request_key="UPPER")
    with pytest.raises(ValidationError):
        assets.create_draft(assetdb, 10, 2, kind="agent", title="\ud800", definition={}, request_key="valid")
    with pytest.raises(ValidationError):
        assets.create_draft(assetdb, 10, 2, kind="agent", title="合法", definition={"x": "a" * 60001},
                            request_key="large")


def test_http_private_contract_and_unsupported_publish(assetdb):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: assetdb
    app.dependency_overrides[get_current_user] = lambda x_test_user=Header(): assetdb.get(User, int(x_test_user))
    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})
    with TestClient(app) as client:
        url = "/enterprises/10/asset-drafts"
        result = client.post(url, headers={"x-test-user": "2"}, json={"kind": "agent", "title": "自己的",
                             "definition": {"prompt": "p"}, "request_key": "http-create"})
        assert result.status_code == 200
        draft_id = result.json()["data"]["id"]
        assert result.json()["data"]["execution_available"] is False
        assert client.get(f"{url}/{draft_id}", headers={"x-test-user": "3"}).status_code == 404
        assert client.get(f"{url}/{draft_id}", headers={"x-test-user": "1"}).status_code == 404
        assert client.post(f"{url}/{draft_id}/publish", headers={"x-test-user": "2"}, json={}).status_code == 404
        assert client.post(f"{url}/{draft_id}/execute", headers={"x-test-user": "2"}, json={}).status_code == 404


def test_http_publication_routes_enforce_global_approval_and_catalog_scope(assetdb, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.include_router(publication_router)
    app.include_router(platform_publication_router)
    app.dependency_overrides[get_db] = lambda: assetdb
    app.dependency_overrides[get_current_user] = lambda x_test_user=Header(): assetdb.get(User, int(x_test_user))
    monkeypatch.setattr(assets.rbac_service, "is_super_admin_user", lambda _db, user_id: user_id == 1)

    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    with TestClient(app) as client:
        created = client.post("/enterprises/10/asset-drafts", headers={"x-test-user": "2"}, json={
            "kind": "skill", "title": "路由共享技能", "definition": {"steps": ["read"]},
            "request_key": "http-publication-create",
        })
        assert created.status_code == 200
        draft_id = created.json()["data"]["id"]
        request = client.post(
            f"/enterprises/10/asset-drafts/{draft_id}/publication-requests",
            headers={"x-test-user": "2"},
            json={"scope": "global", "expected_version": 1, "request_key": "http-global-request"},
        )
        assert request.status_code == 200
        publication_id = request.json()["data"]["id"]
        assert request.json()["data"]["execution_available"] is False
        assert client.get("/enterprise-asset-catalog", headers={"x-test-user": "3"}).json()["data"] == []
        assert client.get("/admin/enterprise-asset-publications", headers={"x-test-user": "2"}).status_code == 403
        pending = client.get("/admin/enterprise-asset-publications", headers={"x-test-user": "1"})
        assert pending.status_code == 200
        assert [row["id"] for row in pending.json()["data"]] == [publication_id]
        approved = client.post(
            f"/admin/enterprise-asset-publications/{publication_id}/decision",
            headers={"x-test-user": "1"}, json={"decision": "approve", "reason": "已核对快照"},
        )
        assert approved.status_code == 200
        assert approved.json()["data"]["status"] == "approved"
        assert approved.json()["data"]["execution_available"] is False
        assert approved.json()["data"]["xiaoling_runtime_config_compatible"] is False
        catalog = client.get("/enterprise-asset-catalog", headers={"x-test-user": "3"})
        assert catalog.status_code == 200
        catalog_item = catalog.json()["data"][0]
        assert catalog_item["title"] == "路由共享技能"
        assert catalog_item["xiaoling_runtime_config_compatible"] is False
        assert set(catalog_item) == {
            "id", "version_number", "kind", "scope", "title", "definition",
            "content_sha256", "execution_available", "xiaoling_runtime_config_compatible", "can_revoke",
            "personal_execution_available",
        }
        revoked = client.post(
            f"/admin/enterprise-asset-publications/{publication_id}/revoke",
            headers={"x-test-user": "1"}, json={"reason": "撤下旧版本"},
        )
        assert revoked.status_code == 200
        assert client.get("/enterprise-asset-catalog", headers={"x-test-user": "3"}).json()["data"] == []

        enterprise_request = client.post(
            f"/enterprises/10/asset-drafts/{draft_id}/publication-requests",
            headers={"x-test-user": "2"},
            json={"scope": "enterprise", "expected_version": 1, "request_key": "http-enterprise-request"},
        )
        assert enterprise_request.status_code == 200
        enterprise_publication_id = enterprise_request.json()["data"]["id"]
        enterprise_approved = client.post(
            f"/enterprises/10/asset-publications/{enterprise_publication_id}/decision",
            headers={"x-test-user": "1"}, json={"decision": "approve", "reason": "企业内批准"},
        )
        assert enterprise_approved.status_code == 200
        enterprise_catalog = client.get("/enterprises/10/asset-catalog", headers={"x-test-user": "3"})
        enterprise_catalog_item = enterprise_catalog.json()["data"][0]
        assert enterprise_catalog_item["can_revoke"] is False
        assert "requested_by" not in enterprise_catalog_item and "decided_by" not in enterprise_catalog_item
        own_catalog = client.get("/enterprises/10/asset-catalog", headers={"x-test-user": "2"})
        assert own_catalog.json()["data"][0]["can_revoke"] is True


def test_publication_is_bound_to_exact_version_and_scope(assetdb, monkeypatch):
    draft = assets.create_draft(assetdb, 10, 2, kind="agent", title="企业流程",
                                definition={"prompt": "v1"}, request_key="publish-create-1")
    first = assets.request_publication(
        assetdb, 10, 2, draft.id, scope="enterprise", expected_version=1, request_key="pub-ent-1",
    )
    assert [row.id for row in assets.list_pending_enterprise_publications(assetdb, 10, 1)] == [first.id]
    assert assets.request_publication(
        assetdb, 10, 2, draft.id, scope="enterprise", expected_version=1, request_key="pub-ent-1",
    ).id == first.id
    with pytest.raises(ConflictError):
        assets.request_publication(
            assetdb, 10, 2, draft.id, scope="global", expected_version=1, request_key="pub-ent-1",
        )

    assets.revise_draft(assetdb, 10, 2, draft.id, title="企业流程 v2", definition={"prompt": "v2"},
                        expected_version=1, request_key="publish-revision-1")
    with pytest.raises(ForbiddenError):
        assets.decide_enterprise_publication(
            assetdb, 10, first.id, 2, approve=True, reason="self approve",
        )
    approved = assets.decide_enterprise_publication(
        assetdb, 10, first.id, 1, approve=True, reason="企业范围内发布",
    )
    assert approved.status == "approved"
    assert approved.version_number == 1
    assert approved.title == "企业流程"
    assert approved.definition_json == '{"prompt":"v1"}'
    assert assets.published_scopes_for_draft(assetdb, 10, 2, draft.id) == []
    assert [row.id for row in assets.list_enterprise_catalog(assetdb, 10, 3)] == [first.id]
    with pytest.raises(ForbiddenError):
        assets.list_enterprise_catalog(assetdb, 10, 4)

    monkeypatch.setattr(assets.rbac_service, "is_super_admin_user", lambda _db, user_id: user_id == 1)
    with pytest.raises(ForbiddenError):
        assets.request_publication(
            assetdb, 10, 1, draft.id, scope="global", expected_version=2, request_key="super-own-global",
        )
    global_request = assets.request_publication(
        assetdb, 10, 2, draft.id, scope="global", expected_version=2, request_key="pub-global-2",
    )
    with pytest.raises(NotFoundError):
        assets.decide_enterprise_publication(
            assetdb, 10, global_request.id, 1, approve=True, reason="wrong approval domain",
        )
    with pytest.raises(ForbiddenError):
        assets.decide_global_publication(
            assetdb, global_request.id, 2, approve=True, reason="ordinary member",
        )
    global_approved = assets.decide_global_publication(
        assetdb, global_request.id, 1, approve=True, reason="通过平台安全审查",
    )
    assert global_approved.scope == "global" and global_approved.status == "approved"
    assert [row.id for row in assets.list_global_catalog(assetdb)] == [global_request.id]

    assert assets.published_scopes_for_draft(assetdb, 10, 2, draft.id) == ["global"]

    first.definition_json = '{"prompt":"unexpected alteration"}'
    assetdb.commit()
    with pytest.raises(ConflictError):
        assets.list_enterprise_catalog(assetdb, 10, 3)

    assets.revoke_enterprise_publication(assetdb, 10, first.id, 1, reason="企业内撤回")
    assert assets.list_enterprise_catalog(assetdb, 10, 3) == []
    assets.revoke_global_publication(assetdb, global_request.id, 1, reason="平台下架")
    assert assets.list_global_catalog(assetdb) == []


def test_runtime_publication_resolves_only_active_member_and_exact_supported_snapshot(assetdb, monkeypatch):
    draft = assets.create_draft(
        assetdb, 10, 2, kind="agent", title="提示 Agent",
        definition={"schema_version": 1, "runtime": "prompt", "system_prompt": "只归纳公开资料。"},
        request_key="runtime-agent-create",
    )
    publication = assets.request_publication(
        assetdb, 10, 2, draft.id, scope="enterprise", expected_version=1,
        request_key="runtime-agent-publish",
    )
    assets.decide_enterprise_publication(assetdb, 10, publication.id, 1, approve=True, reason="核对完成")

    resolved = assets.resolve_runtime_publication(
        assetdb, publication.id, user_id=3, enterprise_id=10,
    )
    assert [row.id for row in assets.search_runtime_catalog(
        assetdb, query="提示", user_id=3, enterprise_id=10,
    )] == [publication.id]
    assert resolved == {
        "publication_id": publication.id,
        "kind": "agent",
        "scope": "enterprise",
        "enterprise_id": 10,
        "version_id": publication.version_id,
        "version_number": 1,
        "title": "提示 Agent",
        "content_sha256": publication.content_sha256,
        "definition": {
            "schema_version": 1, "runtime": "prompt", "system_prompt": "只归纳公开资料。",
        },
    }
    with pytest.raises(NotFoundError):
        assets.resolve_runtime_publication(assetdb, publication.id, user_id=4, enterprise_id=20)

    monkeypatch.setattr(assets.rbac_service, "is_super_admin_user", lambda _db, user_id: user_id == 1)
    global_publication = assets.request_publication(
        assetdb, 10, 2, draft.id, scope="global", expected_version=1,
        request_key="runtime-agent-global",
    )
    assets.decide_global_publication(assetdb, global_publication.id, 1, approve=True, reason="平台核验")
    global_result = assets.resolve_runtime_publication(
        assetdb, global_publication.id, user_id=4,
    )
    assert global_result["scope"] == "global" and global_result["enterprise_id"] is None
    assert [row.id for row in assets.search_runtime_catalog(
        assetdb, query="提示", user_id=4, enterprise_id=None,
    )] == [global_publication.id]

    publication.definition_json = '{"schema_version":1,"runtime":"prompt","system_prompt":"tampered"}'
    assetdb.commit()
    with pytest.raises(ConflictError, match="发布快照摘要"):
        assets.resolve_runtime_publication(assetdb, publication.id, user_id=3, enterprise_id=10)


def test_runtime_definition_contract_only_accepts_prompt_agent_and_transform_skill():
    agent = {"schema_version": 1, "runtime": "prompt", "system_prompt": "请总结。"}
    skill = {"schema_version": 1, "skill_type": "llm_transform", "prompt": "改写为简体中文。"}
    assert assets._runtime_definition("agent", json.dumps(agent)) == agent
    assert assets._runtime_definition("skill", json.dumps(skill)) == skill
    with pytest.raises(ConflictError):
        assets._runtime_definition("skill", json.dumps({
            **skill, "tool_code": "arbitrary_tool",
        }))
    with pytest.raises(ConflictError):
        assets._runtime_definition("agent", "[" * 2000 + "]" * 2000)


def test_runtime_support_marker_distinguishes_compatible_approved_and_legacy_publications():
    compatible = SimpleNamespace(
        status="approved", revoked_at=None, kind="agent",
        definition_json=json.dumps({
            "schema_version": 1, "runtime": "prompt", "system_prompt": "请总结。",
        }),
    )
    legacy = SimpleNamespace(
        status="approved", revoked_at=None, kind="skill", definition_json='{"prompt":"旧配置"}',
    )
    pending = SimpleNamespace(
        status="pending", revoked_at=None, kind="agent", definition_json=compatible.definition_json,
    )
    oversized = SimpleNamespace(
        status="approved", revoked_at=None, kind="agent",
        definition_json=json.dumps({
            "schema_version": 1, "runtime": "prompt", "system_prompt": "界" * 1334,
        }, ensure_ascii=False),
    )

    assert assets.xiaoling_runtime_config_compatible(compatible) is True
    assert assets.xiaoling_runtime_config_compatible(legacy) is False
    assert assets.xiaoling_runtime_config_compatible(pending) is False
    assert assets.xiaoling_runtime_config_compatible(oversized) is False


@pytest.mark.parametrize("definition", [
    {"prompt": "legacy"},
    {"schema_version": 1, "runtime": "prompt", "system_prompt": "run", "model": "other-model"},
    {"schema_version": 1, "skill_type": "readonly_tool", "prompt": "read files"},
    {"schema_version": True, "runtime": "prompt", "system_prompt": "run"},
])
def test_runtime_publication_rejects_legacy_unknown_or_unsafe_definition(assetdb, definition):
    draft = assets.create_draft(
        assetdb, 10, 2, kind="agent", title="暂不兼容",
        definition=definition, request_key=f"runtime-legacy-{len(definition)}-{int(bool(definition.get('prompt'))) }",
    )
    publication = assets.request_publication(
        assetdb, 10, 2, draft.id, scope="enterprise", expected_version=1,
        request_key=f"runtime-legacy-publish-{draft.id}",
    )
    assets.decide_enterprise_publication(assetdb, 10, publication.id, 1, approve=True, reason="legacy visible")
    with pytest.raises(ConflictError):
        assets.resolve_runtime_publication(assetdb, publication.id, user_id=2, enterprise_id=10)


def test_publication_snapshot_tampering_and_withdrawal_are_rejected_or_terminal(assetdb):
    draft = assets.create_draft(assetdb, 10, 2, kind="skill", title="只读 Skill",
                                definition={"description": "v1"}, request_key="pub-create-tamper")
    row = assets.request_publication(
        assetdb, 10, 2, draft.id, scope="enterprise", expected_version=1, request_key="pub-withdraw-1",
    )
    with pytest.raises(NotFoundError):
        assets.withdraw_publication(assetdb, 10, row.id, 3)
    withdrawn = assets.withdraw_publication(assetdb, 10, row.id, 2)
    assert withdrawn.status == "withdrawn"
    with pytest.raises(ConflictError):
        assets.decide_enterprise_publication(assetdb, 10, row.id, 1, approve=True)

    tampered = assets.request_publication(
        assetdb, 10, 2, draft.id, scope="enterprise", expected_version=1, request_key="pub-tamper-2",
    )
    tampered.definition_json = '{"description":"tampered"}'
    assetdb.commit()
    with pytest.raises(ConflictError):
        assets.decide_enterprise_publication(assetdb, 10, tampered.id, 1, approve=True)


def test_pending_publication_blocks_draft_delete_until_terminal(assetdb):
    draft = assets.create_draft(assetdb, 10, 2, kind="agent", title="待审批",
                                definition={"prompt": "v1"}, request_key="pub-delete-create")
    publication = assets.request_publication(
        assetdb, 10, 2, draft.id, scope="enterprise", expected_version=1,
        request_key="pub-delete-pending",
    )
    with pytest.raises(ConflictError, match="待审批"):
        assets.delete_draft(assetdb, 10, 2, draft.id)
    assets.withdraw_publication(assetdb, 10, publication.id, 2)
    assets.delete_draft(assetdb, 10, 2, draft.id)
    with pytest.raises(NotFoundError):
        assets.get_draft(assetdb, 10, 2, draft.id)


def test_removed_requester_can_be_rejected_but_not_approved(assetdb):
    draft = assets.create_draft(assetdb, 10, 2, kind="skill", title="离职申请",
                                definition={"description": "v1"}, request_key="pub-inactive-create")
    publication = assets.request_publication(
        assetdb, 10, 2, draft.id, scope="enterprise", expected_version=1,
        request_key="pub-inactive-request",
    )
    membership = assetdb.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    membership.status = "removed"
    assetdb.commit()
    with pytest.raises(ForbiddenError):
        assets.decide_enterprise_publication(assetdb, 10, publication.id, 1, approve=True)
    rejected = assets.decide_enterprise_publication(
        assetdb, 10, publication.id, 1, approve=False, reason="申请人已离开企业",
    )
    assert rejected.status == "rejected"


def test_publication_request_digest_binds_scope_tenant_requester_and_version(assetdb):
    draft = assets.create_draft(assetdb, 10, 2, kind="agent", title="摘要绑定",
                                definition={"prompt": "固定内容"}, request_key="pub-digest-create")
    publication = assets.request_publication(
        assetdb, 10, 2, draft.id, scope="global", expected_version=1,
        request_key="pub-digest-request",
    )
    original = {
        "scope": publication.scope,
        "enterprise_id": publication.enterprise_id,
        "requested_by": publication.requested_by,
        "draft_id": publication.draft_id,
        "version_id": publication.version_id,
        "version_number": publication.version_number,
    }
    mutations = {
        "scope": "enterprise",
        "enterprise_id": 20,
        "requested_by": 3,
        "draft_id": draft.id + 1,
        "version_id": publication.version_id + 1,
        "version_number": 2,
    }
    for field, changed in mutations.items():
        setattr(publication, field, changed)
        with pytest.raises(ConflictError, match="绑定摘要"):
            assets._publication_payload(publication)
        setattr(publication, field, original[field])


def test_enterprise_publication_requires_another_active_approver(assetdb):
    solo_draft = assets.create_draft(assetdb, 20, 4, kind="agent", title="单管理员企业",
                                     definition={"prompt": "v1"}, request_key="pub-solo-create")
    with pytest.raises(ConflictError, match="另一名有效 owner/admin"):
        assets.request_publication(
            assetdb, 20, 4, solo_draft.id, scope="enterprise", expected_version=1,
            request_key="pub-solo-request",
        )

    assetdb.add(EnterpriseMember(enterprise_id=20, user_id=2, role="admin", status="active"))
    assetdb.commit()
    publication = assets.request_publication(
        assetdb, 20, 4, solo_draft.id, scope="enterprise", expected_version=1,
        request_key="pub-solo-request",
    )
    assert [row.id for row in assets.list_pending_enterprise_publications(assetdb, 20, 2)] == [publication.id]


def test_073_migration_matches_models_and_keeps_existing_tables():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/073_enterprise_asset_drafts.py"
    spec = importlib.util.spec_from_file_location("migration_073_enterprise_asset_drafts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "072_enterprise_knowledge"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        inspector = inspect(connection)
        for model in (EnterpriseAssetDraft, EnterpriseAssetDraftVersion):
            columns = {col["name"] for col in inspector.get_columns(model.__tablename__)}
            assert columns == set(model.__table__.columns.keys())
    engine.dispose()


def test_084_migration_matches_enterprise_asset_publication_model():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/084_enterprise_asset_publications.py"
    spec = importlib.util.spec_from_file_location("migration_084_enterprise_asset_publications", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "083_manual_refund_review"
    assert len(module.revision) <= 32
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE enterprise_asset_draft (id INTEGER PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE enterprise_asset_draft_version (id INTEGER PRIMARY KEY)"
        )
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        inspector = inspect(connection)
        columns = {col["name"] for col in inspector.get_columns("enterprise_asset_publication")}
        assert columns == set(EnterpriseAssetPublication.__table__.columns.keys())
        foreign_keys = inspector.get_foreign_keys("enterprise_asset_publication")
        assert {(key["referred_table"], key["options"].get("ondelete")) for key in foreign_keys} == {
            ("enterprise_asset_draft", "RESTRICT"), ("enterprise_asset_draft_version", "RESTRICT"),
        }
        with pytest.raises(RuntimeError, match="不可静默删除"):
            module.downgrade()
    engine.dispose()


def test_085_migration_matches_idempotent_asset_invocation_model():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/085_enterprise_asset_invocations.py"
    spec = importlib.util.spec_from_file_location("migration_085_enterprise_asset_invocations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "084_ent_asset_publications"
    assert len(module.revision) <= 32
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE enterprise_assistant_run (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE enterprise_asset_publication (id INTEGER PRIMARY KEY)")
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        inspector = inspect(connection)
        columns = {col["name"] for col in inspector.get_columns("enterprise_assistant_asset_invocation")}
        assert columns == set(EnterpriseAssistantAssetInvocation.__table__.columns.keys())
        constraints = inspector.get_foreign_keys("enterprise_assistant_asset_invocation")
        assert {(item["referred_table"], item["options"].get("ondelete")) for item in constraints} == {
            ("enterprise_assistant_run", "RESTRICT"),
            ("enterprise_asset_publication", "RESTRICT"),
        }
        with pytest.raises(RuntimeError, match="不可静默删除"):
            module.downgrade()
    engine.dispose()
