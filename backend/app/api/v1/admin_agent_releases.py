"""Administrator APIs for custom-agent release governance."""

from typing import Any, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.agent_governance import ApprovalItem
from app.models.custom_agent import (
    CustomAgent,
    CustomAgentRelease,
    CustomAgentVersion,
    CustomSkill,
    CustomSkillVersion,
)
from app.models.user import User
from app.schemas.agent_studio import AdminReviseIn, AssetOut, DecisionIn, ReleaseOut, VersionOut
from app.schemas.common import Resp
from app.services import agent_studio_service, approval_service

router = APIRouter()


def _authoring(version: Optional[CustomAgentVersion]) -> Optional[dict[str, Any]]:
    if version is None:
        return None
    return {
        "version_id": version.id,
        "version_number": version.version_number,
        "prompt": version.prompt,
        "review_focus": version.review_focus,
        "model_config": agent_studio_service._load(version.model_config_json, {}),
        "input_schema": agent_studio_service._load(version.input_schema_json, {}),
        "output_schema": agent_studio_service._load(version.output_schema_json, {}),
        "checksum": version.checksum,
    }


def _dependency_detail(db: Session, item: dict[str, Any]) -> dict[str, Any]:
    skill_version = db.get(CustomSkillVersion, int(item.get("skill_version_id") or 0))
    skill = db.get(CustomSkill, skill_version.skill_id) if skill_version else None
    return {
        **item,
        "skill_name": skill.name if skill else None,
        "skill_description": skill.description if skill else None,
        "skill_type": skill_version.skill_type if skill_version else None,
        "definition": agent_studio_service._load(skill_version.definition_json, {}) if skill_version else {},
        "requested_capabilities": (
            agent_studio_service._load(skill_version.requested_capabilities_json, [])
            if skill_version else []
        ),
        "test_evidence": agent_studio_service._load(skill_version.test_evidence_json, {}) if skill_version else {},
        "status": skill_version.status if skill_version else "missing",
    }


@router.get("", response_model=Resp[list[dict]])
def list_release_approvals(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission(PermissionCode.AGENT_ASSET_APPROVE)),
):
    rows = (
        db.query(ApprovalItem)
        .filter(ApprovalItem.action == "agent_package.publish")
        .order_by(ApprovalItem.id.desc())
        .limit(100)
        .all()
    )
    data = []
    for row in rows:
        request = agent_studio_service._load(row.request_json, {})
        version = db.get(CustomAgentVersion, int(request.get("agent_version_id") or 0))
        agent = db.get(CustomAgent, version.agent_id) if version else None
        previous = db.get(CustomAgentVersion, agent.current_published_version_id) if agent else None
        manifest = agent_studio_service._manifest(db, version) if version else {"skills": []}
        dependencies = [_dependency_detail(db, item) for item in manifest.get("skills", [])]
        capabilities = {
            str(capability)
            for item in dependencies
            for capability in item.get("requested_capabilities", [])
        }
        current_authoring = _authoring(version)
        previous_authoring = _authoring(previous)
        is_initial_release = previous is None and version is not None
        data.append({
            "id": row.id,
            "title": row.title,
            "status": row.status,
            "resource": row.resource,
            "decision_reason": row.decision_reason,
            "agent": AssetOut.model_validate(agent).model_dump() if agent else None,
            "version": VersionOut.model_validate(version).model_dump() if version else None,
            "authoring": current_authoring,
            "previous_authoring": previous_authoring,
            "test_evidence": agent_studio_service._load(version.test_evidence_json, {}) if version else {},
            "test_evidence_kind": "static_contract",
            "dependencies": dependencies,
            "diff": {
                "kind": "initial" if is_initial_release else "update",
                "prompt_changed": bool(
                    version and (previous is None or previous.prompt != version.prompt)
                ),
                "review_focus_changed": bool(
                    version and (previous is None or previous.review_focus != version.review_focus)
                ),
                "model_config_changed": bool(
                    version and (previous is None or previous.model_config_json != version.model_config_json)
                ),
                "from_version": previous.version_number if previous else None,
                "to_version": version.version_number if version else None,
            },
            "estimated_calls_per_chunk": 1 if version else 0,
            "risk": {"level": row.risk_level, "requested_capabilities": sorted(capabilities)},
        })
    return Resp(data=data)


@router.get("/agents", response_model=Resp[list[dict]])
def list_custom_agents(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission(PermissionCode.AGENT_ASSET_APPROVE)),
):
    agents = db.query(CustomAgent).order_by(CustomAgent.id.desc()).all()
    return Resp(data=[{
        "agent": AssetOut.model_validate(agent).model_dump(),
        "releases": [ReleaseOut.model_validate(item).model_dump() for item in (
            db.query(CustomAgentRelease)
            .filter(CustomAgentRelease.agent_id == agent.id)
            .order_by(CustomAgentRelease.id.desc())
            .all()
        )],
    } for agent in agents])


@router.post("/{approval_id}/revise", response_model=Resp[VersionOut])
def admin_revise(
    approval_id: int,
    payload: AdminReviseIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission(PermissionCode.AGENT_ASSET_APPROVE)),
):
    row = agent_studio_service.admin_revise_pending(
        db,
        admin,
        approval_id,
        prompt=payload.prompt,
        review_focus=payload.review_focus,
        model_config=payload.model_config_json,
        note=payload.note,
    )
    return Resp(data=VersionOut.model_validate(row))


@router.post("/{approval_id}/approve", response_model=Resp[ReleaseOut])
def approve_release(
    approval_id: int,
    payload: DecisionIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission(PermissionCode.AGENT_ASSET_PUBLISH)),
):
    approval_service.decide_item(db, admin, approval_id, approve=True, note=payload.note)
    row = db.query(CustomAgentRelease).filter(CustomAgentRelease.approval_id == approval_id).first()
    return Resp(data=ReleaseOut.model_validate(row))


@router.post("/{approval_id}/reject", response_model=Resp[dict])
def reject_release(
    approval_id: int,
    payload: DecisionIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission(PermissionCode.AGENT_ASSET_APPROVE)),
):
    row = approval_service.decide_item(db, admin, approval_id, approve=False, note=payload.note)
    return Resp(data={"approval_id": row.id, "status": row.status})


@router.post("/agents/{agent_id}/disable", response_model=Resp[AssetOut])
def disable_agent(
    agent_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission(PermissionCode.AGENT_ASSET_DISABLE)),
):
    return Resp(data=AssetOut.model_validate(agent_studio_service.disable_agent(db, admin, agent_id)))


@router.post("/agents/{agent_id}/rollback/{release_id}", response_model=Resp[ReleaseOut])
def rollback_agent(
    agent_id: int,
    release_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission(PermissionCode.AGENT_ASSET_ROLLBACK)),
):
    return Resp(data=ReleaseOut.model_validate(agent_studio_service.rollback_agent(db, admin, agent_id, release_id)))
