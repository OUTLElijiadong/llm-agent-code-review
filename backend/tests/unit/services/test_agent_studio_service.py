"""Tests for declarative Agent Studio ownership and release state machine."""

import pytest

from app.core.exceptions import ConflictError, ForbiddenError, ValidationError
from app.models.agent_governance import AgentProfile, ApprovalItem
from app.models.custom_agent import (  # noqa: F401
    CustomAgent,
    CustomAgentRelease,
    CustomAgentSkillBinding,
    CustomAgentVersion,
    CustomSkill,
    CustomSkillVersion,
    ReviewTaskAgentRelease,
)
from app.models.user import User
from app.services import agent_studio_service, approval_service
from app.services.declarative_agent_runtime import DeclarativeReviewAgentFactory


def _user(db, username: str, role: str) -> User:
    row = User(username=username, password="x", role=role, status=1)
    db.add(row)
    db.commit()
    return row


def _create_package(db, reviewer: User, suffix: str = "one"):
    skill, skill_version = agent_studio_service.create_skill(
        db,
        reviewer,
        code=f"quality_transform_{suffix}",
        name="Quality transform",
        description="Transform review evidence",
        skill_type="llm_transform",
        definition={"prompt": "Return only normalized review issues."},
        requested_capabilities=[],
    )
    agent, version = agent_studio_service.create_agent(
        db,
        reviewer,
        code=f"review_agent_{suffix}",
        name="Review agent",
        description="Checks error handling",
        prompt="Inspect the supplied code and return evidence-backed review issues.",
        review_focus="Error handling and reliability",
        model_config={"temperature": 0.1},
    )
    agent_studio_service.bind_skill(
        db,
        reviewer,
        version.id,
        skill_version_id=skill_version.id,
        position=0,
        config={},
    )
    return agent, version, skill, skill_version


def _test_and_submit(db, reviewer: User, version: CustomAgentVersion) -> ApprovalItem:
    agent_studio_service.test_agent_version(
        db,
        reviewer,
        version.id,
        {"issues": []},
    )
    return agent_studio_service.submit_agent_version(db, reviewer, version.id, "ready")


def test_create_test_submit_and_atomic_publish(db, admin_user):
    reviewer = _user(db, "reviewer_one", "reviewer")
    agent, version, skill, skill_version = _create_package(db, reviewer)

    approval = _test_and_submit(db, reviewer, version)
    duplicate = agent_studio_service.submit_agent_version(db, reviewer, version.id)
    assert duplicate.id == approval.id
    assert db.get(CustomAgentVersion, version.id).status == "pending_approval"

    approval_service.decide_item(db, admin_user, approval.id, approve=True, note="approved")
    assert approval_service.decide_item(db, admin_user, approval.id, approve=True).id == approval.id
    release = db.query(CustomAgentRelease).one()
    assert release.status == "published"
    assert db.get(CustomAgent, agent.id).current_published_version_id == version.id
    assert db.get(CustomAgent, agent.id).is_enabled == 1
    assert db.get(CustomSkill, skill.id).current_published_version_id == skill_version.id
    assert db.get(CustomSkillVersion, skill_version.id).status == "published"
    profile = db.query(AgentProfile).filter(AgentProfile.code == agent.code).one()
    boundary = agent_studio_service._load(profile.config_json, {})["governance_boundary"]
    assert boundary["allowed_tools"] == []
    assert agent_studio_service.publish_for_approval(db, db.get(ApprovalItem, approval.id)).id == release.id
    assert agent_studio_service.list_catalog(db)[0]["code"] == agent.code


def test_published_shared_skill_keeps_published_state(db, admin_user):
    reviewer = _user(db, "reviewer_shared", "reviewer")
    _, first, skill, skill_version = _create_package(db, reviewer, "shared_first")
    approval = _test_and_submit(db, reviewer, first)
    approval_service.decide_item(db, admin_user, approval.id, approve=True)

    _, second = agent_studio_service.create_agent(
        db,
        reviewer,
        code="review_agent_shared_second",
        name="Second agent",
        description="",
        prompt="Inspect code using a globally published exact skill version.",
        review_focus="Reliability",
        model_config={},
    )
    agent_studio_service.bind_skill(db, reviewer, second.id, skill_version_id=skill_version.id, position=0, config={})
    agent_studio_service.test_agent_version(db, reviewer, second.id, {"issues": []})
    agent_studio_service.submit_agent_version(db, reviewer, second.id)
    assert db.get(CustomSkillVersion, skill_version.id).status == "published"
    assert db.get(CustomSkill, skill.id).current_published_version_id == skill_version.id


def test_owner_and_protected_code_boundaries(db):
    reviewer = _user(db, "reviewer_owner", "reviewer")
    other = _user(db, "reviewer_other", "reviewer")
    user = _user(db, "plain_user", "user")
    agent, version, _, _ = _create_package(db, reviewer, "owner")

    with pytest.raises(ForbiddenError):
        agent_studio_service.revise_agent(
            db,
            other,
            agent.id,
            prompt="A different sufficiently long prompt for this test.",
            review_focus="Quality",
            model_config={},
        )
    with pytest.raises(ForbiddenError):
        agent_studio_service.create_agent(
            db,
            user,
            code="plain_agent",
            name="Plain agent",
            description="",
            prompt="A sufficiently long prompt for this plain user test.",
            review_focus="Quality",
            model_config={},
        )
    with pytest.raises(ConflictError):
        agent_studio_service.create_agent(
            db,
            reviewer,
            code="chat_assistant",
            name="Protected",
            description="",
            prompt="A sufficiently long prompt for protected code test.",
            review_focus="Quality",
            model_config={},
        )
    assert db.get(CustomAgentVersion, version.id).status == "draft"


def test_controlled_skill_types_and_frozen_binding(db):
    reviewer = _user(db, "reviewer_skill", "reviewer")
    with pytest.raises(ValidationError):
        agent_studio_service.create_skill(
            db,
            reviewer,
            code="shell_skill",
            name="Shell skill",
            description="",
            skill_type="readonly_tool",
            definition={"tool_code": "delete_project"},
            requested_capabilities=["shell"],
        )
    agent, version, _, skill_version = _create_package(db, reviewer, "frozen")
    _test_and_submit(db, reviewer, version)
    with pytest.raises(ValidationError):
        agent_studio_service.bind_skill(
            db,
            reviewer,
            version.id,
            skill_version_id=skill_version.id,
            position=1,
            config={},
        )
    assert db.get(CustomAgent, agent.id).status == "pending_approval"


def test_checksum_drift_blocks_submission(db):
    reviewer = _user(db, "reviewer_checksum", "reviewer")
    _, version, _, _ = _create_package(db, reviewer, "checksum")
    agent_studio_service.test_agent_version(db, reviewer, version.id, {"issues": []})
    version.prompt = "Database-side prompt mutation that must not be accepted."
    db.commit()
    with pytest.raises(ConflictError, match="校验和"):
        agent_studio_service.submit_agent_version(db, reviewer, version.id)


def test_author_can_withdraw_pending_submission(db):
    reviewer = _user(db, "reviewer_withdraw", "reviewer")
    agent, version, _, skill_version = _create_package(db, reviewer, "withdraw")
    approval = _test_and_submit(db, reviewer, version)
    withdrawn = agent_studio_service.withdraw_agent_version(db, reviewer, version.id, "needs changes")
    assert withdrawn.status == "testing"
    assert db.get(CustomSkillVersion, skill_version.id).status == "testing"
    assert db.get(ApprovalItem, approval.id).status == "rejected"
    assert db.get(CustomAgent, agent.id).status == "testing"
    assert agent_studio_service.withdraw_agent_version(db, reviewer, version.id).id == version.id


def test_sequence_cycle_is_rejected_by_test(db):
    reviewer = _user(db, "reviewer_cycle", "reviewer")
    agent, version = agent_studio_service.create_agent(
        db,
        reviewer,
        code="cycle_agent",
        name="Cycle agent",
        description="",
        prompt="Inspect supplied code for quality issues and return evidence.",
        review_focus="Quality",
        model_config={},
    )
    skill_a, version_a = agent_studio_service.create_skill(
        db,
        reviewer,
        code="workflow_a",
        name="Workflow A",
        description="",
        skill_type="sequence_workflow",
        definition={"steps": [{"skill_version_id": 999999}]},
        requested_capabilities=[],
    )
    skill_b, version_b = agent_studio_service.create_skill(
        db,
        reviewer,
        code="workflow_b",
        name="Workflow B",
        description="",
        skill_type="sequence_workflow",
        definition={"steps": [{"skill_version_id": version_a.id}]},
        requested_capabilities=[],
    )
    version_a.definition_json = agent_studio_service._json({"steps": [{"skill_version_id": version_b.id}]})
    db.commit()
    agent_studio_service.bind_skill(db, reviewer, version.id, skill_version_id=version_a.id, position=0, config={})
    agent_studio_service.bind_skill(db, reviewer, version.id, skill_version_id=version_b.id, position=1, config={})
    with pytest.raises(ValidationError, match="循环"):
        agent_studio_service.test_agent_version(db, reviewer, version.id, {"issues": []})
    assert skill_a.id and skill_b.id and agent.id


def test_admin_revision_requires_retest_and_reapproval(db, admin_user):
    reviewer = _user(db, "reviewer_revise", "reviewer")
    agent, version, _, _ = _create_package(db, reviewer, "revise")
    approval = _test_and_submit(db, reviewer, version)

    revised = agent_studio_service.admin_revise_pending(
        db,
        admin_user,
        approval.id,
        prompt="Administrator revised prompt that still returns fixed issue schema.",
        review_focus="Reliability and evidence",
        model_config={"temperature": 0},
        note="Narrow scope",
    )
    assert revised.revised_by == admin_user.id
    assert revised.original_author_id == reviewer.id
    assert revised.status == "draft"
    assert db.get(ApprovalItem, approval.id).status == "rejected"
    assert db.query(CustomAgentSkillBinding).filter(CustomAgentSkillBinding.agent_version_id == revised.id).count() == 1
    with pytest.raises(ValidationError, match="测试"):
        agent_studio_service.submit_agent_version(db, admin_user, revised.id)
    assert db.get(CustomAgent, agent.id).status == "draft"


def test_reject_disable_rollback_and_snapshot_are_idempotent(db, admin_user):
    reviewer = _user(db, "reviewer_lifecycle", "reviewer")
    agent, first, _, _ = _create_package(db, reviewer, "lifecycle")
    first_approval = _test_and_submit(db, reviewer, first)
    approval_service.decide_item(db, admin_user, first_approval.id, approve=True)
    first_release = db.query(CustomAgentRelease).one()
    snapshots = agent_studio_service.snapshot_active_releases(db, 501)
    assert len(snapshots) == 1
    profiles = DeclarativeReviewAgentFactory.snapshot_profiles(db, 501)
    assert profiles[0].is_custom is True
    assert profiles[0].release_id == first_release.id
    assert agent_studio_service.snapshot_active_releases(db, 501)[0].id == snapshots[0].id
    db.commit()

    second = agent_studio_service.revise_agent(
        db,
        reviewer,
        agent.id,
        prompt="Second version of the review prompt with stronger evidence requirements.",
        review_focus="Evidence",
        model_config={},
    )
    second_approval = _test_and_submit(db, reviewer, second)
    approval_service.decide_item(db, admin_user, second_approval.id, approve=True)
    second_release = db.query(CustomAgentRelease).filter(CustomAgentRelease.status == "published").one()
    rollback = agent_studio_service.rollback_agent(db, admin_user, agent.id, first_release.id)
    assert rollback.agent_version_id == first.id
    assert rollback.rollback_of_release_id == second_release.id
    assert agent_studio_service.rollback_agent(db, admin_user, agent.id, first_release.id).id == rollback.id
    disabled = agent_studio_service.disable_agent(db, admin_user, agent.id)
    assert disabled.is_enabled == 0
    assert agent_studio_service.disable_agent(db, admin_user, agent.id).is_enabled == 0

    rejected_version = agent_studio_service.revise_agent(
        db,
        reviewer,
        agent.id,
        prompt="Third version that the administrator will reject after testing.",
        review_focus="Maintainability",
        model_config={},
    )
    rejected_approval = _test_and_submit(db, reviewer, rejected_version)
    approval_service.decide_item(db, admin_user, rejected_approval.id, approve=False)
    assert db.get(CustomAgentVersion, rejected_version.id).status == "rejected"


def test_sequence_workflow_expands_exact_skill_versions_at_runtime(db):
    reviewer = _user(db, "reviewer_sequence_runtime", "reviewer")
    _, transform = agent_studio_service.create_skill(
        db,
        reviewer,
        code="runtime_transform",
        name="Runtime transform",
        description="",
        skill_type="llm_transform",
        definition={"prompt": "Normalize the evidence into the fixed Issue schema."},
        requested_capabilities=[],
    )
    _, workflow = agent_studio_service.create_skill(
        db,
        reviewer,
        code="runtime_sequence",
        name="Runtime sequence",
        description="",
        skill_type="sequence_workflow",
        definition={"steps": [{"skill_version_id": transform.id}]},
        requested_capabilities=[],
    )

    rendered = DeclarativeReviewAgentFactory._compile_skill_version(
        db,
        "runtime_agent",
        workflow.id,
        user=None,
        depth=0,
        path=(),
    )

    assert "步骤 1" in rendered
    assert "Normalize the evidence" in rendered
