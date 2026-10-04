"""已发布声明式依赖的运行完整性回归；数据库/模型均为本地样本。"""

import json
from types import SimpleNamespace

import pytest

from app.ai.multi_agent import GENERAL_AGENT
from app.core.exceptions import ConflictError, ValidationError
from app.models.custom_agent import CustomAgentRelease
from app.models.user import User
from app.services import agent_studio_service, approval_service, published_agent_tools
from app.services.declarative_agent_runtime import DeclarativeReviewAgentFactory


def _published_package(db, admin_user):
    reviewer = User(username="integrity_reviewer", password="local-only", role="reviewer", status=1)
    db.add(reviewer)
    db.commit()
    _, skill_version = agent_studio_service.create_skill(
        db, reviewer, code="integrity_skill", name="Integrity skill", description="",
        skill_type="llm_transform", definition={"prompt": "Read evidence only."}, requested_capabilities=[],
    )
    asset, version = agent_studio_service.create_agent(
        db, reviewer, code="integrity_agent", name="Integrity agent", description="",
        prompt="Review supplied code.", review_focus="Account isolation", model_config={},
    )
    agent_studio_service.bind_skill(db, reviewer, version.id, skill_version_id=skill_version.id, position=0, config={})
    agent_studio_service.test_agent_version(db, reviewer, version.id, {"issues": []})
    approval = agent_studio_service.submit_agent_version(db, reviewer, version.id, "local fixture")
    approval_service.decide_item(db, admin_user, approval.id, approve=True)
    release = db.query(CustomAgentRelease).filter_by(approval_id=approval.id).one()
    return asset, version, skill_version, release


@pytest.mark.parametrize("field", ["prompt", "review_focus", "model_config_json"])
def test_runtime_revalidates_published_template_payload_checksum(db, admin_user, field):
    asset, version, _, release = _published_package(db, admin_user)
    expected_checksum = version.checksum
    setattr(version, field, '{"max_tokens":2048}' if field == "model_config_json" else "Mutated after approval")
    db.commit()

    with pytest.raises((ConflictError, ValidationError), match="校验|checksum"):
        DeclarativeReviewAgentFactory.resolve_release(
            db, asset.code, release_id=release.id, version_id=version.id,
            package_checksum=release.package_checksum, template_checksum=expected_checksum,
        )


def test_runtime_revalidates_published_skill_payload_checksum(db, admin_user):
    asset, version, skill, release = _published_package(db, admin_user)
    skill.definition_json = json.dumps({"prompt": "Mutated after approval"})
    db.commit()

    with pytest.raises((ConflictError, ValidationError), match="校验|checksum"):
        DeclarativeReviewAgentFactory.resolve_release(
            db, asset.code, release_id=release.id, version_id=version.id,
            package_checksum=release.package_checksum, template_checksum=version.checksum,
        )


def test_recomputed_template_checksum_still_must_match_frozen_manifest(db, admin_user):
    asset, version, _, release = _published_package(db, admin_user)
    version.prompt = "Mutated with its digest after approval"
    version.checksum = agent_studio_service._checksum(agent_studio_service._agent_payload(
        version.prompt, version.review_focus, json.loads(version.model_config_json),
    ))
    db.commit()

    with pytest.raises(ValidationError, match="冻结发布包 checksum"):
        DeclarativeReviewAgentFactory.resolve_published(db, asset.code)


def test_recomputed_skill_checksum_still_must_match_frozen_dependency(db, admin_user):
    asset, _, skill, _ = _published_package(db, admin_user)
    definition = {"prompt": "Mutated with its digest after approval"}
    skill.definition_json = json.dumps(definition)
    skill.checksum = agent_studio_service._checksum(agent_studio_service._skill_payload(
        skill.skill_type, definition, [],
    ))
    db.commit()

    with pytest.raises(ValidationError, match="发布依赖快照"):
        DeclarativeReviewAgentFactory.resolve_published(db, asset.code)


@pytest.mark.parametrize("edge_limit,depth", [(1, 1), (2, 2)], ids=["edge-depth-one", "global-depth-two"])
def test_exhausted_delegation_depth_does_not_silently_claim_complete(db, admin_user, monkeypatch, edge_limit, depth):
    profile = GENERAL_AGENT.__class__(**{
        **GENERAL_AGENT.__dict__, "code": "depth_agent", "release_id": 10, "version_id": 100,
    })
    definition = SimpleNamespace(
        to_profile=lambda: profile, release_id=10, version_id=100,
        delegated_agents=({
            "kind": "custom", "agent_code": "required_child", "release_id": 20, "version_id": 200,
            "package_checksum": "p" * 64, "template_checksum": "t" * 64, "max_depth": edge_limit,
        },),
    )
    monkeypatch.setattr(published_agent_tools, "_require_invoke_permission", lambda *_: None)
    monkeypatch.setattr(
        published_agent_tools.DeclarativeReviewAgentFactory, "resolve_published", lambda *_a, **_kw: definition,
    )
    calls = []

    class LocalModel:
        def __init__(self, **_kwargs):
            pass

        def call_raw(self, **kwargs):
            calls.append(kwargs)
            return '{"summary":"parent complete","score":100,"issues":[]}', {}

        def log_deferred(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", LocalModel)
    monkeypatch.setattr(published_agent_tools, "resolve_api_config", lambda *_: object())
    monkeypatch.setattr(published_agent_tools, "resolve_subagent_config", lambda *_: object())

    with pytest.raises(ValidationError, match="深度|完整审查"):
        published_agent_tools.invoke_published_agent(
            db, admin_user, agent_code="depth_agent", code="pass", _delegation_depth=depth,
        )
    assert calls == []


def test_real_frozen_release_chain_beyond_depth_is_rejected_before_model(db, admin_user, monkeypatch):
    reviewer = User(username="depth_reviewer", password="local-only", role="reviewer", status=1)
    db.add(reviewer)
    db.commit()
    child = None
    for depth in range(3, -1, -1):
        asset, version = agent_studio_service.create_agent(
            db, reviewer, code=f"depth_chain_{depth}", name=f"Chain {depth}", description="",
            prompt="Review supplied evidence.", review_focus="Read-only reliability", model_config={},
        )
        if child:
            _, skill = agent_studio_service.create_skill(
                db, reviewer, code=f"depth_skill_{depth}", name="Required delegation", description="",
                skill_type="agent_delegate", definition={"agent_code": child.code, "max_depth": 2},
                requested_capabilities=[],
            )
            agent_studio_service.bind_skill(db, reviewer, version.id, skill_version_id=skill.id, position=0, config={})
        agent_studio_service.test_agent_version(db, reviewer, version.id, {"issues": []})
        approval = agent_studio_service.submit_agent_version(db, reviewer, version.id, "local fixture")
        approval_service.decide_item(db, admin_user, approval.id, approve=True)
        child = asset

    class UnexpectedModel:
        def __init__(self, **_kwargs):
            pytest.fail("Required descendants cannot be silently skipped before a successful parent report")

    monkeypatch.setattr(published_agent_tools, "_require_invoke_permission", lambda *_: None)
    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", UnexpectedModel)
    monkeypatch.setattr(published_agent_tools, "resolve_api_config", lambda *_: object())
    monkeypatch.setattr(published_agent_tools, "resolve_subagent_config", lambda *_: object())

    with pytest.raises(ValidationError, match="深度.*必需子 Agent 未执行"):
        published_agent_tools.invoke_published_agent(db, admin_user, agent_code=child.code, code="pass")
