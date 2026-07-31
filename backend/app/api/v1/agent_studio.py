"""Reviewer-facing Agent Studio APIs."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.custom_agent import (
    CustomAgent,
    CustomAgentSkillBinding,
    CustomAgentVersion,
    CustomSkill,
    CustomSkillVersion,
)
from app.models.user import User
from app.schemas.agent_studio import (
    AgentCreateIn,
    AgentReviseIn,
    AssetOut,
    SkillBindingIn,
    SkillCreateIn,
    SkillReviseIn,
    SubmitIn,
    VersionOut,
    VersionTestIn,
)
from app.schemas.common import Resp
from app.services import agent_studio_service

router = APIRouter()


@router.get("/agents", response_model=Resp[list[AssetOut]])
def list_owned_agents(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.AGENT_ASSET_UPDATE_OWN)),
):
    rows = db.query(CustomAgent).filter(CustomAgent.owner_id == user.id).order_by(CustomAgent.id.desc()).all()
    return Resp(data=[AssetOut.model_validate(row) for row in rows])


@router.post("/agents", response_model=Resp[dict])
def create_agent(
    payload: AgentCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.AGENT_ASSET_CREATE)),
):
    asset, version = agent_studio_service.create_agent(
        db,
        user,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        prompt=payload.prompt,
        review_focus=payload.review_focus,
        model_config=payload.model_config_json,
    )
    return Resp(
        data={
            "agent": AssetOut.model_validate(asset).model_dump(),
            "version": VersionOut.model_validate(version).model_dump(),
        }
    )


@router.get("/agents/{agent_id}/versions", response_model=Resp[list[VersionOut]])
def list_agent_versions(
    agent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.AGENT_ASSET_UPDATE_OWN)),
):
    asset = db.get(CustomAgent, agent_id)
    agent_studio_service._assert_owner(db, asset.owner_id if asset else 0, user)
    rows = (
        db.query(CustomAgentVersion)
        .filter(CustomAgentVersion.agent_id == agent_id)
        .order_by(CustomAgentVersion.version_number.desc())
        .all()
    )
    return Resp(data=[VersionOut.model_validate(row) for row in rows])


@router.get("/agent-versions/{version_id}", response_model=Resp[dict])
def get_agent_version(
    version_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.AGENT_ASSET_UPDATE_OWN)),
):
    version = db.get(CustomAgentVersion, version_id)
    asset = db.get(CustomAgent, version.agent_id) if version else None
    agent_studio_service._assert_owner(db, asset.owner_id if asset else 0, user)
    bindings = (
        db.query(CustomAgentSkillBinding)
        .filter(CustomAgentSkillBinding.agent_version_id == version_id)
        .order_by(CustomAgentSkillBinding.position.asc())
        .all()
    )
    return Resp(data={
        **VersionOut.model_validate(version).model_dump(),
        "prompt": version.prompt,
        "review_focus": version.review_focus,
        "model_config": agent_studio_service._load(version.model_config_json, {}),
        "bindings": [{
            "id": item.id,
            "skill_version_id": item.skill_version_id,
            "position": item.position,
            "config": agent_studio_service._load(item.config_json, {}),
        } for item in bindings],
    })


@router.post("/agents/{agent_id}/versions", response_model=Resp[VersionOut])
def revise_agent(
    agent_id: int,
    payload: AgentReviseIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.AGENT_ASSET_UPDATE_OWN)),
):
    row = agent_studio_service.revise_agent(
        db,
        user,
        agent_id,
        prompt=payload.prompt,
        review_focus=payload.review_focus,
        model_config=payload.model_config_json,
        note=payload.note,
    )
    return Resp(data=VersionOut.model_validate(row))


@router.post("/agent-versions/{version_id}/skills", response_model=Resp[dict])
def bind_skill(
    version_id: int,
    payload: SkillBindingIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.AGENT_ASSET_UPDATE_OWN)),
):
    row = agent_studio_service.bind_skill(
        db,
        user,
        version_id,
        skill_version_id=payload.skill_version_id,
        position=payload.position,
        config=payload.config,
    )
    return Resp(
        data={
            "id": row.id,
            "agent_version_id": row.agent_version_id,
            "skill_version_id": row.skill_version_id,
            "position": row.position,
        }
    )


@router.delete("/bindings/{binding_id}", response_model=Resp[dict])
def unbind_skill(
    binding_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.AGENT_ASSET_UPDATE_OWN)),
):
    agent_studio_service.unbind_skill(db, user, binding_id)
    return Resp(data={"binding_id": binding_id, "removed": True})


@router.post("/agent-versions/{version_id}/test", response_model=Resp[VersionOut])
def test_version(
    version_id: int,
    payload: VersionTestIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.AGENT_ASSET_TEST)),
):
    row = agent_studio_service.test_agent_version(db, user, version_id, payload.sample_output)
    return Resp(data=VersionOut.model_validate(row))


@router.post("/agent-versions/{version_id}/submit", response_model=Resp[dict])
def submit_version(
    version_id: int,
    payload: SubmitIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.AGENT_ASSET_SUBMIT)),
):
    row = agent_studio_service.submit_agent_version(db, user, version_id, payload.note)
    return Resp(data={"approval_id": row.id, "status": row.status})


@router.post("/agent-versions/{version_id}/withdraw", response_model=Resp[VersionOut])
def withdraw_version(
    version_id: int,
    payload: SubmitIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.AGENT_ASSET_SUBMIT)),
):
    row = agent_studio_service.withdraw_agent_version(db, user, version_id, payload.note)
    return Resp(data=VersionOut.model_validate(row))


@router.get("/skills", response_model=Resp[list[AssetOut]])
def list_owned_skills(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.SKILL_ASSET_UPDATE_OWN)),
):
    rows = db.query(CustomSkill).filter(CustomSkill.owner_id == user.id).order_by(CustomSkill.id.desc()).all()
    return Resp(data=[AssetOut.model_validate(row) for row in rows])


@router.post("/skills", response_model=Resp[dict])
def create_skill(
    payload: SkillCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.SKILL_ASSET_CREATE)),
):
    asset, version = agent_studio_service.create_skill(
        db,
        user,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        skill_type=payload.skill_type,
        definition=payload.definition,
        requested_capabilities=payload.requested_capabilities,
    )
    return Resp(
        data={
            "skill": AssetOut.model_validate(asset).model_dump(),
            "version": VersionOut.model_validate(version).model_dump(),
        }
    )


@router.get("/skills/{skill_id}/versions", response_model=Resp[list[VersionOut]])
def list_skill_versions(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.SKILL_ASSET_UPDATE_OWN)),
):
    asset = db.get(CustomSkill, skill_id)
    agent_studio_service._assert_owner(db, asset.owner_id if asset else 0, user)
    rows = (
        db.query(CustomSkillVersion)
        .filter(CustomSkillVersion.skill_id == skill_id)
        .order_by(CustomSkillVersion.version_number.desc())
        .all()
    )
    return Resp(data=[VersionOut.model_validate(row) for row in rows])


@router.get("/skill-versions/{version_id}", response_model=Resp[dict])
def get_skill_version(
    version_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.SKILL_ASSET_UPDATE_OWN)),
):
    version = db.get(CustomSkillVersion, version_id)
    asset = db.get(CustomSkill, version.skill_id) if version else None
    agent_studio_service._assert_owner(db, asset.owner_id if asset else 0, user)
    return Resp(data={
        **VersionOut.model_validate(version).model_dump(),
        "skill_type": version.skill_type,
        "definition": agent_studio_service._load(version.definition_json, {}),
        "requested_capabilities": agent_studio_service._load(version.requested_capabilities_json, []),
    })


@router.post("/skills/{skill_id}/versions", response_model=Resp[VersionOut])
def revise_skill(
    skill_id: int,
    payload: SkillReviseIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.SKILL_ASSET_UPDATE_OWN)),
):
    row = agent_studio_service.revise_skill(
        db,
        user,
        skill_id,
        skill_type=payload.skill_type,
        definition=payload.definition,
        requested_capabilities=payload.requested_capabilities,
        note=payload.note,
    )
    return Resp(data=VersionOut.model_validate(row))
