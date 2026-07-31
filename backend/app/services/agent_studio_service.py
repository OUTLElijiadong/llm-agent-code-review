"""Domain service for authoring and publishing declarative review agents."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.agents.contracts import CONTRACTS, PROTECTED_AGENT_CODES
from app.agents.tool_contracts import FixedToolArgumentError, is_fixed_tool, validate_fixed_tool_arguments
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.permission_codes import PermissionCode
from app.models.agent_governance import AgentProfile, ApprovalItem
from app.models.custom_agent import (
    CustomAgent,
    CustomAgentRelease,
    CustomAgentSkillBinding,
    CustomAgentVersion,
    CustomSkill,
    CustomSkillVersion,
    ReviewTaskAgentRelease,
)
from app.models.user import User
from app.services import audit_service
from app.services.rbac_service import check_permission, is_admin_user

SKILL_TYPES = {"llm_transform", "readonly_tool", "agent_delegate", "sequence_workflow"}
READONLY_TOOLS = {
    "analyze_project",
    "dashboard_summary",
    "detect_language",
    "list_agents",
    "list_code_files",
    "list_projects",
    "list_reports",
    "list_review_issues",
    "list_review_tasks",
    "list_rules",
}
FORBIDDEN_CAPABILITIES = {
    "database_write",
    "dynamic_import",
    "file_write",
    "network",
    "shell",
    "subprocess",
}
ISSUE_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["issues"],
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "description", "severity", "issue_type", "suggestion"],
            },
        }
    },
}
REVIEW_INPUT_SCHEMA = {
    "type": "object",
    "required": ["code", "language", "file_name", "rules", "line_offset", "experience"],
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _checksum(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _is_admin(db: Session, user: User) -> bool:
    return is_admin_user(db, user.id)


def _assert_reviewer(db: Session, user: User) -> None:
    if user.role != "reviewer" and not check_permission(db, user.id, PermissionCode.AGENT_ASSET_CREATE):
        raise ForbiddenError("仅审查员或管理员可使用 Agent 工坊", code=40300)


def _assert_owner(db: Session, owner_id: int, user: User) -> None:
    if owner_id != user.id and not _is_admin(db, user):
        raise ForbiddenError("只能操作自己的 Agent 工坊资产", code=40300)


def _normalize_model_config(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValidationError("模型参数必须是对象", code=40001)
    unknown = set(value) - {"temperature", "max_tokens"}
    if unknown:
        raise ValidationError(f"不允许的模型参数: {', '.join(sorted(unknown))}", code=40001)
    try:
        temperature = float(value.get("temperature", 0.2))
        max_tokens = int(value.get("max_tokens", 4096))
    except (TypeError, ValueError) as exc:
        raise ValidationError("模型参数类型不合法", code=40001) from exc
    if not 0 <= temperature <= 1:
        raise ValidationError("temperature 必须在 0 到 1 之间", code=40001)
    if not 128 <= max_tokens <= 4096:
        raise ValidationError("max_tokens 必须在 128 到 4096 之间", code=40001)
    return {"temperature": temperature, "max_tokens": max_tokens}


def _audit(db: Session, actor: User, action: str, target_type: str, target_id: int, detail: str) -> None:
    audit_service.log(
        db,
        actor,
        action,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
        commit=False,
    )


def _next_version(db: Session, model, foreign_key, asset_id: int) -> int:
    return int(db.query(func.max(model.version_number)).filter(foreign_key == asset_id).scalar() or 0) + 1


def _agent_payload(prompt: str, review_focus: str, model_config: dict) -> dict:
    return {
        "prompt": prompt,
        "review_focus": review_focus,
        "model_config": model_config,
        "input_schema": REVIEW_INPUT_SCHEMA,
        "output_schema": ISSUE_OUTPUT_SCHEMA,
    }


def _skill_payload(skill_type: str, definition: dict, capabilities: list[str]) -> dict:
    return {
        "skill_type": skill_type,
        "definition": definition,
        "requested_capabilities": sorted(set(capabilities)),
    }


def _assert_agent_integrity(version: CustomAgentVersion) -> None:
    actual = _checksum(
        _agent_payload(
            version.prompt,
            version.review_focus,
            _load(version.model_config_json, {}),
        )
    )
    if actual != version.checksum:
        raise ConflictError("Agent 版本校验和不匹配", code=40901)


def _assert_skill_integrity(version: CustomSkillVersion) -> None:
    actual = _checksum(
        _skill_payload(
            version.skill_type,
            _load(version.definition_json, {}),
            _load(version.requested_capabilities_json, []),
        )
    )
    if actual != version.checksum:
        raise ConflictError("Skill 版本校验和不匹配", code=40901)


def _ensure_code_available(code: str) -> None:
    if code in CONTRACTS or code in PROTECTED_AGENT_CODES:
        raise ConflictError("不能覆盖内置或受保护 Agent 编码", code=40901)


def create_agent(
    db: Session,
    actor: User,
    *,
    code: str,
    name: str,
    description: str,
    prompt: str,
    review_focus: str,
    model_config: dict,
) -> tuple[CustomAgent, CustomAgentVersion]:
    _assert_reviewer(db, actor)
    model_config = _normalize_model_config(model_config)
    _ensure_code_available(code)
    if db.query(CustomAgent).filter(CustomAgent.code == code).first():
        raise ConflictError("Agent 编码已存在", code=40901)
    asset = CustomAgent(code=code, name=name, description=description, owner_id=actor.id)
    db.add(asset)
    db.flush()
    payload = _agent_payload(prompt, review_focus, model_config)
    version = CustomAgentVersion(
        agent_id=asset.id,
        version_number=1,
        prompt=prompt,
        review_focus=review_focus,
        model_config_json=_json(model_config),
        input_schema_json=_json(REVIEW_INPUT_SCHEMA),
        output_schema_json=_json(ISSUE_OUTPUT_SCHEMA),
        checksum=_checksum(payload),
        status="draft",
        original_author_id=actor.id,
    )
    db.add(version)
    _audit(db, actor, "agent_studio_create", "custom_agent", asset.id, f"create v{version.version_number}")
    db.commit()
    db.refresh(asset)
    db.refresh(version)
    return asset, version


def revise_agent(
    db: Session,
    actor: User,
    agent_id: int,
    *,
    prompt: str,
    review_focus: str,
    model_config: dict,
    note: str = "",
) -> CustomAgentVersion:
    asset = db.get(CustomAgent, agent_id)
    if not asset:
        raise NotFoundError("Agent 不存在", code=40400)
    _assert_owner(db, asset.owner_id, actor)
    model_config = _normalize_model_config(model_config)
    payload = _agent_payload(prompt, review_focus, model_config)
    version = CustomAgentVersion(
        agent_id=asset.id,
        version_number=_next_version(db, CustomAgentVersion, CustomAgentVersion.agent_id, asset.id),
        prompt=prompt,
        review_focus=review_focus,
        model_config_json=_json(model_config),
        input_schema_json=_json(REVIEW_INPUT_SCHEMA),
        output_schema_json=_json(ISSUE_OUTPUT_SCHEMA),
        checksum=_checksum(payload),
        status="draft",
        original_author_id=asset.owner_id,
        revised_by=actor.id if actor.id != asset.owner_id else None,
        revision_note=note or None,
    )
    db.add(version)
    asset.status = "draft"
    _audit(db, actor, "agent_studio_revise", "custom_agent", asset.id, f"create v{version.version_number}")
    db.commit()
    db.refresh(version)
    return version


def create_skill(
    db: Session,
    actor: User,
    *,
    code: str,
    name: str,
    description: str,
    skill_type: str,
    definition: dict,
    requested_capabilities: list[str],
) -> tuple[CustomSkill, CustomSkillVersion]:
    _assert_reviewer(db, actor)
    if db.query(CustomSkill).filter(CustomSkill.code == code).first():
        raise ConflictError("Skill 编码已存在", code=40901)
    _validate_skill_definition(db, skill_type, definition, requested_capabilities)
    asset = CustomSkill(code=code, name=name, description=description, owner_id=actor.id)
    db.add(asset)
    db.flush()
    payload = _skill_payload(skill_type, definition, requested_capabilities)
    version = CustomSkillVersion(
        skill_id=asset.id,
        version_number=1,
        skill_type=skill_type,
        definition_json=_json(definition),
        requested_capabilities_json=_json(sorted(set(requested_capabilities))),
        checksum=_checksum(payload),
        status="draft",
        original_author_id=actor.id,
    )
    db.add(version)
    _audit(db, actor, "skill_studio_create", "custom_skill", asset.id, f"create v{version.version_number}")
    db.commit()
    db.refresh(asset)
    db.refresh(version)
    return asset, version


def revise_skill(
    db: Session,
    actor: User,
    skill_id: int,
    *,
    skill_type: str,
    definition: dict,
    requested_capabilities: list[str],
    note: str = "",
) -> CustomSkillVersion:
    asset = db.get(CustomSkill, skill_id)
    if not asset:
        raise NotFoundError("Skill 不存在", code=40400)
    _assert_owner(db, asset.owner_id, actor)
    _validate_skill_definition(db, skill_type, definition, requested_capabilities)
    payload = _skill_payload(skill_type, definition, requested_capabilities)
    version = CustomSkillVersion(
        skill_id=asset.id,
        version_number=_next_version(db, CustomSkillVersion, CustomSkillVersion.skill_id, asset.id),
        skill_type=skill_type,
        definition_json=_json(definition),
        requested_capabilities_json=_json(sorted(set(requested_capabilities))),
        checksum=_checksum(payload),
        status="draft",
        original_author_id=asset.owner_id,
        revised_by=actor.id if actor.id != asset.owner_id else None,
        revision_note=note or None,
    )
    db.add(version)
    asset.status = "draft"
    _audit(db, actor, "skill_studio_revise", "custom_skill", asset.id, f"create v{version.version_number}")
    db.commit()
    db.refresh(version)
    return version


def _validate_skill_definition(db: Session, skill_type: str, definition: dict, capabilities: list[str]) -> None:
    if skill_type not in SKILL_TYPES:
        raise ValidationError("Skill 类型不受支持", code=40001)
    forbidden = FORBIDDEN_CAPABILITIES.intersection(capabilities)
    if forbidden:
        raise ValidationError(f"Skill 申请了禁止能力: {', '.join(sorted(forbidden))}", code=40001)
    serialized = _json(definition).lower()
    if any(
        marker in serialized
        for marker in ("subprocess", "os.system", "file_write", "database_write", "http://", "https://")
    ):
        raise ValidationError("Skill 定义包含禁止操作", code=40001)
    if skill_type == "llm_transform":
        if not isinstance(definition.get("prompt"), str) or not definition["prompt"].strip():
            raise ValidationError("llm_transform 必须定义 prompt", code=40001)
    elif skill_type == "readonly_tool":
        tool_code = definition.get("tool_code")
        if tool_code not in READONLY_TOOLS or not is_fixed_tool(str(tool_code)):
            raise ValidationError("readonly_tool 只能使用白名单内只读工具", code=40001)
        try:
            validate_fixed_tool_arguments(str(tool_code), definition.get("arguments", {}))
        except (FixedToolArgumentError, KeyError) as exc:
            raise ValidationError("readonly_tool 参数不符合固定工具契约", code=40001) from exc
    elif skill_type == "agent_delegate":
        target = str(definition.get("agent_code") or "")
        if not target or target in PROTECTED_AGENT_CODES:
            raise ValidationError("不能委派给空目标或受保护 Agent", code=40001)
        published = db.query(CustomAgent).filter(CustomAgent.code == target, CustomAgent.is_enabled == 1).first()
        if target not in CONTRACTS and not published:
            raise ValidationError("委派目标必须是已发布 Agent", code=40001)
        if int(definition.get("max_depth", 2)) > 2:
            raise ValidationError("委派深度不得超过 2", code=40001)
    elif skill_type == "sequence_workflow":
        steps = definition.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            raise ValidationError("顺序工作流必须包含 1至8 个节点", code=40001)
        if len({_json(step) for step in steps}) != len(steps):
            raise ValidationError("顺序工作流不允许重复节点", code=40001)


def bind_skill(
    db: Session,
    actor: User,
    agent_version_id: int,
    *,
    skill_version_id: int,
    position: int,
    config: dict,
) -> CustomAgentSkillBinding:
    version = db.get(CustomAgentVersion, agent_version_id)
    skill_version = db.get(CustomSkillVersion, skill_version_id)
    if not version or not skill_version:
        raise NotFoundError("Agent 或 Skill 版本不存在", code=40400)
    asset = db.get(CustomAgent, version.agent_id)
    skill = db.get(CustomSkill, skill_version.skill_id)
    _assert_owner(db, asset.owner_id, actor)
    if version.status not in {"draft", "testing"}:
        raise ValidationError("已提交版本不可修改绑定", code=40001)
    if skill.owner_id != actor.id and not _is_admin(db, actor) and skill_version.status != "published":
        raise ForbiddenError("只能绑定自有或已发布 Skill", code=40300)
    if (
        db.query(CustomAgentSkillBinding)
        .filter(
            CustomAgentSkillBinding.agent_version_id == agent_version_id,
            CustomAgentSkillBinding.position == position,
        )
        .first()
    ):
        raise ConflictError("Skill 绑定位置已被占用", code=40901)
    row = CustomAgentSkillBinding(
        agent_version_id=agent_version_id,
        skill_version_id=skill_version_id,
        position=position,
        config_json=_json(config),
    )
    db.add(row)
    version.status = "draft"
    version.tested_checksum = None
    version.test_evidence_json = None
    _audit(db, actor, "agent_skill_bind", "custom_agent", asset.id, f"bind skill version {skill_version_id}")
    db.commit()
    db.refresh(row)
    return row


def unbind_skill(db: Session, actor: User, binding_id: int) -> None:
    row = db.get(CustomAgentSkillBinding, binding_id)
    if not row:
        raise NotFoundError("Skill 绑定不存在", code=40400)
    version = db.get(CustomAgentVersion, row.agent_version_id)
    asset = db.get(CustomAgent, version.agent_id) if version else None
    if not version or not asset:
        raise NotFoundError("Agent 版本不存在", code=40400)
    _assert_owner(db, asset.owner_id, actor)
    if version.status not in {"draft", "testing"}:
        raise ValidationError("已提交版本不可修改绑定", code=40001)
    db.delete(row)
    version.status = "draft"
    version.tested_checksum = None
    version.test_evidence_json = None
    _audit(db, actor, "agent_skill_unbind", "custom_agent", asset.id, f"unbind {binding_id}")
    db.commit()


def _bindings(db: Session, agent_version_id: int) -> list[CustomAgentSkillBinding]:
    return (
        db.query(CustomAgentSkillBinding)
        .filter(CustomAgentSkillBinding.agent_version_id == agent_version_id)
        .order_by(CustomAgentSkillBinding.position.asc())
        .all()
    )


def _validate_issue_output(sample: Optional[dict]) -> None:
    if sample is None:
        return
    issues = sample.get("issues")
    if not isinstance(issues, list):
        raise ValidationError("测试输出必须包含 issues 数组", code=40001)
    required = {"title", "description", "severity", "issue_type", "suggestion"}
    if any(not isinstance(item, dict) or not required.issubset(item) for item in issues):
        raise ValidationError("测试 Issue 不符合固定输出契约", code=40001)


def _validate_skill_graph(
    db: Session,
    asset: CustomAgent,
    bindings: list[CustomAgentSkillBinding],
) -> None:
    """Validate exact skill dependencies as a bounded acyclic graph."""
    bound_ids = {row.skill_version_id for row in bindings}
    graph: dict[int, set[int]] = {skill_id: set() for skill_id in bound_ids}
    for binding in bindings:
        skill_version = db.get(CustomSkillVersion, binding.skill_version_id)
        definition = _load(skill_version.definition_json, {})
        if skill_version.skill_type == "agent_delegate":
            if str(definition.get("agent_code") or "") == asset.code:
                raise ValidationError("Agent 不能委派给自身", code=40001)
            continue
        if skill_version.skill_type != "sequence_workflow":
            continue
        for step in definition.get("steps", []):
            if not isinstance(step, dict) or not isinstance(step.get("skill_version_id"), int):
                raise ValidationError("工作流节点必须引用精确 skill_version_id", code=40001)
            target_id = int(step["skill_version_id"])
            target = db.get(CustomSkillVersion, target_id)
            if not target or (target_id not in bound_ids and target.status != "published"):
                raise ValidationError("工作流依赖必须是同包或已发布 Skill", code=40001)
            if target_id in bound_ids:
                graph[binding.skill_version_id].add(target_id)

    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(node: int) -> None:
        if node in visiting:
            raise ValidationError("Skill 工作流存在循环依赖", code=40001)
        if node in visited:
            return
        visiting.add(node)
        for target in graph[node]:
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for skill_id in graph:
        visit(skill_id)


def test_agent_version(
    db: Session,
    actor: User,
    agent_version_id: int,
    sample_output: Optional[dict] = None,
) -> CustomAgentVersion:
    version = db.get(CustomAgentVersion, agent_version_id)
    if not version:
        raise NotFoundError("Agent 版本不存在", code=40400)
    asset = db.get(CustomAgent, version.agent_id)
    _assert_owner(db, asset.owner_id, actor)
    if version.status not in {"draft", "testing"}:
        raise ValidationError("只能测试草稿版本", code=40001)
    _assert_agent_integrity(version)
    _validate_issue_output(sample_output)
    checked_skills = []
    bindings = _bindings(db, version.id)
    _validate_skill_graph(db, asset, bindings)
    for binding in bindings:
        skill_version = db.get(CustomSkillVersion, binding.skill_version_id)
        _assert_skill_integrity(skill_version)
        _validate_skill_definition(
            db,
            skill_version.skill_type,
            _load(skill_version.definition_json, {}),
            _load(skill_version.requested_capabilities_json, []),
        )
        if skill_version.status != "published":
            skill_version.status = "testing"
            skill_version.tested_checksum = skill_version.checksum
            skill_version.test_evidence_json = _json({"passed": True, "checks": ["type", "capability", "dependency"]})
        checked_skills.append(skill_version.id)
    version.status = "testing"
    version.tested_checksum = version.checksum
    version.test_evidence_json = _json(
        {"passed": True, "checks": ["input_schema", "issue_schema", "skill_graph"], "skill_versions": checked_skills}
    )
    _audit(db, actor, "agent_studio_test", "custom_agent", asset.id, f"test v{version.version_number}: passed")
    db.commit()
    db.refresh(version)
    return version


def submit_agent_version(db: Session, actor: User, agent_version_id: int, note: str = "") -> ApprovalItem:
    version = db.query(CustomAgentVersion).filter(CustomAgentVersion.id == agent_version_id).with_for_update().first()
    if not version:
        raise NotFoundError("Agent 版本不存在", code=40400)
    asset = db.get(CustomAgent, version.agent_id)
    _assert_owner(db, asset.owner_id, actor)
    if version.status == "pending_approval":
        existing = (
            db.query(ApprovalItem)
            .filter(
                ApprovalItem.action == "agent_package.publish",
                ApprovalItem.resource == f"custom_agent_version:{version.id}",
                ApprovalItem.status == "pending",
            )
            .first()
        )
        if existing:
            return existing
    if version.status != "testing" or version.tested_checksum != version.checksum:
        raise ValidationError("当前校验和版本必须先通过测试", code=40001)
    _assert_agent_integrity(version)
    bindings = _bindings(db, version.id)
    for binding in bindings:
        skill_version = db.get(CustomSkillVersion, binding.skill_version_id)
        _assert_skill_integrity(skill_version)
        if skill_version.tested_checksum != skill_version.checksum:
            raise ValidationError("绑定 Skill 尚未通过测试", code=40001)
        if skill_version.status != "published":
            conflicting = (
                db.query(CustomAgentSkillBinding)
                .join(
                    CustomAgentVersion,
                    CustomAgentVersion.id == CustomAgentSkillBinding.agent_version_id,
                )
                .filter(
                    CustomAgentSkillBinding.skill_version_id == skill_version.id,
                    CustomAgentSkillBinding.agent_version_id != version.id,
                    CustomAgentVersion.status == "pending_approval",
                )
                .first()
            )
            if conflicting:
                raise ConflictError("未发布 Skill 版本已属于另一待审批发布包", code=40901)
            skill_version.status = "pending_approval"
    version.status = "pending_approval"
    version.submitted_at = _utcnow()
    asset.status = "pending_approval"
    approval = ApprovalItem(
        title=f"发布自定义 Agent: {asset.name} v{version.version_number}",
        agent_code=asset.code,
        action="agent_package.publish",
        resource=f"custom_agent_version:{version.id}",
        risk_level="high",
        status="pending",
        decision_reason=note or "审查员提交 Agent 发布包",
        request_json=_json({"agent_version_id": version.id, "owner_id": asset.owner_id}),
    )
    db.add(approval)
    _audit(db, actor, "agent_studio_submit", "custom_agent", asset.id, f"submit v{version.version_number}")
    db.commit()
    db.refresh(approval)
    return approval


def withdraw_agent_version(db: Session, actor: User, agent_version_id: int, note: str = "") -> CustomAgentVersion:
    version = db.query(CustomAgentVersion).filter(CustomAgentVersion.id == agent_version_id).with_for_update().first()
    if not version:
        raise NotFoundError("Agent 版本不存在", code=40400)
    asset = db.get(CustomAgent, version.agent_id)
    _assert_owner(db, asset.owner_id, actor)
    if version.status == "testing":
        return version
    if version.status != "pending_approval":
        raise ValidationError("只能撤回待审批 Agent 版本", code=40001)
    approval = (
        db.query(ApprovalItem)
        .filter(
            ApprovalItem.action == "agent_package.publish",
            ApprovalItem.resource == f"custom_agent_version:{version.id}",
            ApprovalItem.status == "pending",
        )
        .with_for_update()
        .first()
    )
    if not approval:
        raise ConflictError("待审批记录不存在", code=40901)
    approval.status = "rejected"
    approval.decision = "deny"
    approval.decision_reason = note or "作者撤回发布申请"
    approval.decided_by = actor.id
    approval.decided_at = _utcnow()
    version.status = "testing"
    asset.status = "published" if asset.current_published_version_id else "testing"
    for binding in _bindings(db, version.id):
        skill_version = db.get(CustomSkillVersion, binding.skill_version_id)
        if skill_version.status == "pending_approval":
            skill_version.status = "testing"
    _audit(db, actor, "agent_studio_withdraw", "custom_agent", asset.id, f"withdraw v{version.version_number}")
    db.commit()
    db.refresh(version)
    return version


def reject_for_approval(db: Session, approval: ApprovalItem) -> None:
    payload = _load(approval.request_json, {})
    version = db.get(CustomAgentVersion, int(payload.get("agent_version_id") or 0))
    if not version or version.status == "rejected":
        return
    version.status = "rejected"
    asset = db.get(CustomAgent, version.agent_id)
    if asset and asset.current_published_version_id:
        asset.status = "published"
    elif asset:
        asset.status = "draft"
    for binding in _bindings(db, version.id):
        skill_version = db.get(CustomSkillVersion, binding.skill_version_id)
        if skill_version and skill_version.status == "pending_approval":
            skill_version.status = "testing"


def _manifest(db: Session, version: CustomAgentVersion) -> dict:
    skills = []
    for binding in _bindings(db, version.id):
        skill_version = db.get(CustomSkillVersion, binding.skill_version_id)
        skill = db.get(CustomSkill, skill_version.skill_id)
        skills.append(
            {
                "binding_id": binding.id,
                "position": binding.position,
                "config": _load(binding.config_json, {}),
                "skill_id": skill.id,
                "skill_code": skill.code,
                "skill_version_id": skill_version.id,
                "skill_version": skill_version.version_number,
                "skill_type": skill_version.skill_type,
                "skill_checksum": skill_version.checksum,
            }
        )
    return {
        "schema_version": "1.0",
        "agent_version_id": version.id,
        "agent_version": version.version_number,
        "agent_checksum": version.checksum,
        "input_schema": _load(version.input_schema_json, {}),
        "output_schema": _load(version.output_schema_json, {}),
        "skills": skills,
    }


def _sync_governance_profile(
    db: Session,
    asset: CustomAgent,
    version: CustomAgentVersion,
    release: CustomAgentRelease,
    manifest: dict,
) -> None:
    """Persist a default-deny ToolGateway boundary for the published package."""
    allowed_tools: set[str] = set()
    for item in manifest.get("skills", []):
        if item.get("skill_type") != "readonly_tool":
            continue
        skill_version = db.get(CustomSkillVersion, int(item.get("skill_version_id") or 0))
        definition = _load(skill_version.definition_json if skill_version else None, {})
        tool_code = str(definition.get("tool_code") or "")
        if tool_code in READONLY_TOOLS:
            allowed_tools.add(tool_code)
    profile = db.query(AgentProfile).filter(AgentProfile.code == asset.code).first()
    if not profile:
        profile = AgentProfile(code=asset.code, name=asset.name)
        db.add(profile)
    profile.name = asset.name
    profile.description = asset.description or ""
    profile.category = "custom_review"
    profile.status = "idle"
    profile.icon = "custom_review_agent"
    profile.color = "#2F7D6D"
    profile.is_enabled = 1
    profile.config_json = _json(
        {
            "source": "custom_agent_studio",
            "release_id": release.id,
            "version_id": version.id,
            "governance_boundary": {
                "allowed_tools": sorted(allowed_tools),
                "approval_tools": [],
                "blocked_tools": [],
            },
        }
    )


def publish_for_approval(db: Session, approval: ApprovalItem) -> CustomAgentRelease:
    existing = db.query(CustomAgentRelease).filter(CustomAgentRelease.approval_id == approval.id).first()
    if existing:
        return existing
    payload = _load(approval.request_json, {})
    version = (
        db.query(CustomAgentVersion)
        .filter(CustomAgentVersion.id == int(payload.get("agent_version_id") or 0))
        .with_for_update()
        .first()
    )
    if not version:
        raise NotFoundError("Agent 待发布版本不存在", code=40400)
    if version.status != "pending_approval":
        raise ValidationError("Agent 版本不在待审批状态", code=40001)
    _assert_agent_integrity(version)
    for binding in _bindings(db, version.id):
        _assert_skill_integrity(db.get(CustomSkillVersion, binding.skill_version_id))
    asset = db.query(CustomAgent).filter(CustomAgent.id == version.agent_id).with_for_update().first()
    previous = (
        db.query(CustomAgentRelease)
        .filter(
            CustomAgentRelease.agent_id == asset.id,
            CustomAgentRelease.status == "published",
        )
        .order_by(CustomAgentRelease.id.desc())
        .first()
    )
    manifest = _manifest(db, version)
    release = CustomAgentRelease(
        agent_id=asset.id,
        agent_version_id=version.id,
        approval_id=approval.id,
        previous_release_id=previous.id if previous else None,
        package_manifest_json=_json(manifest),
        package_checksum=_checksum(manifest),
        status="published",
        published_by=int(approval.decided_by or 0),
        published_at=_utcnow(),
    )
    db.add(release)
    db.flush()
    if previous:
        previous.status = "superseded"
    for binding in _bindings(db, version.id):
        skill_version = db.get(CustomSkillVersion, binding.skill_version_id)
        skill = db.get(CustomSkill, skill_version.skill_id)
        skill_version.status = "published"
        skill.current_published_version_id = skill_version.id
        skill.status = "published"
    version.status = "published"
    asset.current_published_version_id = version.id
    asset.status = "published"
    asset.is_enabled = 1
    _sync_governance_profile(db, asset, version, release, manifest)
    return release


def admin_revise_pending(
    db: Session,
    admin: User,
    approval_id: int,
    *,
    prompt: str,
    review_focus: str,
    model_config: dict,
    note: str,
) -> CustomAgentVersion:
    if not _is_admin(db, admin):
        raise ForbiddenError("需要管理员权限", code=40300)
    approval = db.query(ApprovalItem).filter(ApprovalItem.id == approval_id).with_for_update().first()
    if not approval or approval.action != "agent_package.publish":
        raise NotFoundError("Agent 发布审批不存在", code=40400)
    if approval.status != "pending":
        raise ValidationError("只能修订待审批发布包", code=40001)
    payload = _load(approval.request_json, {})
    old = db.get(CustomAgentVersion, int(payload.get("agent_version_id") or 0))
    reject_for_approval(db, approval)
    approval.status = "rejected"
    approval.decision = "deny"
    approval.decision_reason = note or "管理员修订后需重新测试提交"
    approval.decided_by = admin.id
    approval.decided_at = _utcnow()
    asset = db.get(CustomAgent, old.agent_id)
    model_config = _normalize_model_config(model_config)
    value = _agent_payload(prompt, review_focus, model_config)
    revised = CustomAgentVersion(
        agent_id=asset.id,
        version_number=_next_version(db, CustomAgentVersion, CustomAgentVersion.agent_id, asset.id),
        prompt=prompt,
        review_focus=review_focus,
        model_config_json=_json(model_config),
        input_schema_json=_json(REVIEW_INPUT_SCHEMA),
        output_schema_json=_json(ISSUE_OUTPUT_SCHEMA),
        checksum=_checksum(value),
        status="draft",
        original_author_id=asset.owner_id,
        revised_by=admin.id,
        revision_note=note or None,
    )
    db.add(revised)
    db.flush()
    for binding in _bindings(db, old.id):
        db.add(
            CustomAgentSkillBinding(
                agent_version_id=revised.id,
                skill_version_id=binding.skill_version_id,
                position=binding.position,
                config_json=binding.config_json,
            )
        )
    asset.status = "draft"
    _audit(db, admin, "agent_admin_revise", "custom_agent", asset.id, f"revise to v{revised.version_number}")
    db.commit()
    db.refresh(revised)
    return revised


def disable_agent(db: Session, admin: User, agent_id: int) -> CustomAgent:
    if not _is_admin(db, admin):
        raise ForbiddenError("需要管理员权限", code=40300)
    asset = db.query(CustomAgent).filter(CustomAgent.id == agent_id).with_for_update().first()
    if not asset:
        raise NotFoundError("Agent 不存在", code=40400)
    if not asset.is_enabled:
        return asset
    asset.is_enabled = 0
    asset.status = "disabled"
    release = (
        db.query(CustomAgentRelease)
        .filter(
            CustomAgentRelease.agent_id == asset.id,
            CustomAgentRelease.status == "published",
        )
        .first()
    )
    if release:
        release.status = "disabled"
        release.disabled_at = _utcnow()
    profile = db.query(AgentProfile).filter(AgentProfile.code == asset.code).first()
    if profile:
        profile.is_enabled = 0
        profile.status = "disabled"
    version = db.get(CustomAgentVersion, asset.current_published_version_id)
    if version:
        version.status = "disabled"
    _audit(db, admin, "agent_admin_disable", "custom_agent", asset.id, "disable published agent")
    db.commit()
    db.refresh(asset)
    from app.services.declarative_agent_runtime import publish_catalog_invalidation

    publish_catalog_invalidation("disable", asset.code)
    return asset


def rollback_agent(db: Session, admin: User, agent_id: int, target_release_id: int) -> CustomAgentRelease:
    if not _is_admin(db, admin):
        raise ForbiddenError("需要管理员权限", code=40300)
    asset = db.query(CustomAgent).filter(CustomAgent.id == agent_id).with_for_update().first()
    target = db.get(CustomAgentRelease, target_release_id)
    if not asset or not target or target.agent_id != agent_id:
        raise NotFoundError("回滚目标不存在", code=40400)
    current = (
        db.query(CustomAgentRelease)
        .filter(
            CustomAgentRelease.agent_id == agent_id,
            CustomAgentRelease.status == "published",
        )
        .order_by(CustomAgentRelease.id.desc())
        .first()
    )
    if current and current.agent_version_id == target.agent_version_id:
        return current
    manifest = _load(target.package_manifest_json, {})
    rollback_manifest = {
        **manifest,
        "rollback_of_release_id": current.id if current else None,
        "source_release_id": target.id,
    }
    release = CustomAgentRelease(
        agent_id=agent_id,
        agent_version_id=target.agent_version_id,
        approval_id=None,
        previous_release_id=current.id if current else None,
        rollback_of_release_id=current.id if current else None,
        package_manifest_json=_json(rollback_manifest),
        package_checksum=_checksum(rollback_manifest),
        status="published",
        published_by=admin.id,
        published_at=_utcnow(),
    )
    if current:
        current.status = "rolled_back"
        current_version = db.get(CustomAgentVersion, current.agent_version_id)
        if current_version:
            current_version.status = "rolled_back"
    db.add(release)
    db.flush()
    asset.current_published_version_id = target.agent_version_id
    asset.is_enabled = 1
    asset.status = "published"
    target_version = db.get(CustomAgentVersion, target.agent_version_id)
    target_version.status = "published"
    _sync_governance_profile(db, asset, target_version, release, manifest)
    _audit(db, admin, "agent_admin_rollback", "custom_agent", asset.id, f"rollback to release {target.id}")
    db.commit()
    db.refresh(release)
    from app.services.declarative_agent_runtime import publish_catalog_invalidation

    publish_catalog_invalidation("rollback", asset.code)
    return release


def list_catalog(db: Session) -> list[dict]:
    rows = (
        db.query(CustomAgent, CustomAgentVersion, CustomAgentRelease)
        .join(CustomAgentVersion, CustomAgent.current_published_version_id == CustomAgentVersion.id)
        .join(
            CustomAgentRelease,
            (CustomAgentRelease.agent_id == CustomAgent.id)
            & (CustomAgentRelease.agent_version_id == CustomAgentVersion.id)
            & (CustomAgentRelease.status == "published"),
        )
        .filter(CustomAgent.is_enabled == 1)
        .order_by(CustomAgent.id.asc())
        .all()
    )
    result = []
    for asset, version, release in rows:
        manifest = _load(release.package_manifest_json, {})
        result.append(
            {
                "id": asset.id,
                "code": asset.code,
                "name": asset.name,
                "description": asset.description or "",
                "owner_id": asset.owner_id,
                "version_id": version.id,
                "version_number": version.version_number,
                "release_id": release.id,
                "skills": manifest.get("skills", []),
            }
        )
    return result


def snapshot_active_releases(db: Session, task_id: int) -> list[ReviewTaskAgentRelease]:
    existing = db.query(ReviewTaskAgentRelease).filter(ReviewTaskAgentRelease.task_id == task_id).all()
    if existing:
        return existing
    releases = (
        db.query(CustomAgentRelease)
        .join(CustomAgent, CustomAgent.id == CustomAgentRelease.agent_id)
        .filter(
            CustomAgentRelease.status == "published",
            CustomAgent.is_enabled == 1,
        )
        .all()
    )
    rows = [
        ReviewTaskAgentRelease(
            task_id=task_id,
            release_id=release.id,
            agent_version_id=release.agent_version_id,
            package_manifest_json=release.package_manifest_json,
        )
        for release in releases
    ]
    db.add_all(rows)
    db.flush()
    return rows
