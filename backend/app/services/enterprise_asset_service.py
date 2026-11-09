"""仅作者可访问的企业 Agent/Skill 草稿，不提供执行或发布能力。"""
import hashlib
import json
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.enterprise_asset import (
    EnterpriseAssetDraft,
    EnterpriseAssetDraftVersion,
    EnterpriseAssetPublication,
)
from app.models.enterprise_org import EnterpriseAuditEvent, EnterpriseMember
from app.models.user import User
from app.services import rbac_service
from app.services.enterprise_org_service import (
    _company,
    require_enterprise_admin,
    require_enterprise_member,
)


def _check_input(title, definition, request_key):
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
        raise ValidationError("草稿名称无效")
    # 小写 ASCII 避免 MySQL 默认大小写不敏感排序规则导致不同请求键冲突。
    if not isinstance(request_key, str) or re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,79}", request_key) is None:
        raise ValidationError("请求标识无效")
    if not isinstance(definition, dict):
        raise ValidationError("草稿配置必须是对象")
    try:
        title.encode("utf-8")
        body = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(body.encode("utf-8")) > 60000:
            raise ValidationError("草稿配置不能超过 60000 字节")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValidationError("草稿配置不是有效的 JSON 对象") from exc
    return title.strip(), body


def _digest(*values):
    data = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _lock_member(db, enterprise_id, user_id):
    # 与企业成员变动使用相同的企业行锁，避免离职并发后仍写入。
    _company(db, enterprise_id, lock=True)
    require_enterprise_member(db, enterprise_id, user_id, lock=True)


def _draft(db, enterprise_id, user_id, draft_id, *, lock=False):
    query = db.query(EnterpriseAssetDraft).filter_by(
        id=draft_id, enterprise_id=enterprise_id, creator_id=user_id, status="active"
    )
    row = query.populate_existing().with_for_update().first() if lock else query.first()
    if row is None:
        raise NotFoundError("草稿不存在")
    return row


def _audit(db, enterprise_id, user_id, action, draft_id):
    db.add(EnterpriseAuditEvent(enterprise_id=enterprise_id, actor_id=user_id, action=action,
                                detail={"draft_id": draft_id}))


def create_draft(db: Session, enterprise_id: int, user_id: int, *, kind: str, title: str,
                 definition: dict, request_key: str):
    _lock_member(db, enterprise_id, user_id)
    if kind not in {"agent", "skill"}:
        raise ValidationError("草稿类型无效")
    title, body = _check_input(title, definition, request_key)
    digest = _digest(kind, title, body)
    existing = db.query(EnterpriseAssetDraft).filter_by(
        enterprise_id=enterprise_id, creator_id=user_id, request_key=request_key
    ).populate_existing().with_for_update().first()
    if existing:
        if existing.request_digest != digest or existing.status != "active":
            raise ConflictError("请求标识已用于其他草稿或草稿已删除")
        db.commit()
        return existing
    row = EnterpriseAssetDraft(enterprise_id=enterprise_id, creator_id=user_id, kind=kind, title=title,
                               definition_json=body, current_version=1, request_key=request_key,
                               request_digest=digest, status="active")
    db.add(row)
    db.flush()
    db.add(EnterpriseAssetDraftVersion(draft_id=row.id, version_number=1, title=title,
                                      definition_json=body, request_key=request_key, request_digest=digest))
    _audit(db, enterprise_id, user_id, "enterprise_asset_created", row.id)
    db.commit()
    return row


def list_drafts(db: Session, enterprise_id: int, user_id: int, *, offset=0, limit=100):
    require_enterprise_member(db, enterprise_id, user_id)
    return (db.query(EnterpriseAssetDraft).filter_by(enterprise_id=enterprise_id, creator_id=user_id, status="active")
            .order_by(EnterpriseAssetDraft.id.desc()).offset(offset).limit(limit).all())


def published_scopes_for_draft(db: Session, enterprise_id: int, user_id: int, draft_id: int) -> list[str]:
    draft = _draft(db, enterprise_id, user_id, draft_id)
    rows = db.query(EnterpriseAssetPublication.scope).filter(
        EnterpriseAssetPublication.enterprise_id == enterprise_id,
        EnterpriseAssetPublication.draft_id == draft_id,
        EnterpriseAssetPublication.version_number == draft.current_version,
        EnterpriseAssetPublication.status == "approved",
    ).distinct().all()
    return sorted(str(row.scope) for row in rows)


def get_draft(db: Session, enterprise_id: int, user_id: int, draft_id: int):
    require_enterprise_member(db, enterprise_id, user_id)
    return _draft(db, enterprise_id, user_id, draft_id)


def list_versions(db: Session, enterprise_id: int, user_id: int, draft_id: int, *, offset=0, limit=100):
    get_draft(db, enterprise_id, user_id, draft_id)
    return (db.query(EnterpriseAssetDraftVersion).filter_by(draft_id=draft_id)
            .order_by(EnterpriseAssetDraftVersion.version_number.desc()).offset(offset).limit(limit).all())


def revise_draft(db: Session, enterprise_id: int, user_id: int, draft_id: int, *, title: str,
                 definition: dict, expected_version: int, request_key: str):
    _lock_member(db, enterprise_id, user_id)
    row = _draft(db, enterprise_id, user_id, draft_id, lock=True)
    title, body = _check_input(title, definition, request_key)
    if type(expected_version) is not int or expected_version < 1:
        raise ValidationError("预期版本无效")
    digest = _digest(expected_version, title, body)
    existing = db.query(EnterpriseAssetDraftVersion).filter_by(draft_id=draft_id, request_key=request_key).\
        populate_existing().with_for_update().first()
    if existing:
        if existing.request_digest != digest:
            raise ConflictError("请求标识已用于其他版本")
        db.commit()
        return existing
    if row.current_version != expected_version:
        raise ConflictError("草稿已变更，请刷新后重试")
    version = EnterpriseAssetDraftVersion(draft_id=row.id, version_number=row.current_version + 1, title=title,
                                         definition_json=body, request_key=request_key, request_digest=digest)
    row.title, row.definition_json, row.current_version = title, body, version.version_number
    db.add(version)
    _audit(db, enterprise_id, user_id, "enterprise_asset_revised", row.id)
    db.commit()
    return version


def delete_draft(db: Session, enterprise_id: int, user_id: int, draft_id: int):
    _lock_member(db, enterprise_id, user_id)
    row = _draft(db, enterprise_id, user_id, draft_id, lock=True)
    pending = db.query(EnterpriseAssetPublication.id).filter_by(
        enterprise_id=enterprise_id, draft_id=draft_id, status="pending",
    ).first()
    if pending is not None:
        raise ConflictError("草稿有待审批的发布申请，请先撤回或等待审批结束")
    row.status = "deleted"
    _audit(db, enterprise_id, user_id, "enterprise_asset_deleted", row.id)
    db.commit()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _content_hash(kind: str, title: str, definition_json: str) -> str:
    try:
        definition = json.loads(definition_json)
        if not isinstance(definition, dict):
            raise ValueError("snapshot definition must be an object")
        canonical = json.dumps(
            {"kind": kind, "title": title, "definition": definition},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ConflictError("发布快照不是有效的 JSON 对象") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verify_publication_digest(row: EnterpriseAssetPublication) -> None:
    if _content_hash(row.kind, row.title, row.definition_json) != row.content_sha256:
        raise ConflictError("发布快照摘要不匹配，拒绝展示")
    expected_request_digest = _digest(
        row.scope, row.enterprise_id, row.requested_by, row.draft_id,
        row.version_id, row.version_number, row.content_sha256,
    )
    if row.request_digest != expected_request_digest:
        raise ConflictError("发布申请的范围或版本绑定摘要不匹配，拒绝展示")


def _publication(db, enterprise_id, publication_id, *, lock=False):
    query = db.query(EnterpriseAssetPublication).filter_by(
        id=publication_id, enterprise_id=enterprise_id,
    )
    return (query.populate_existing().with_for_update() if lock else query.populate_existing()).first()


def _publication_payload(row: EnterpriseAssetPublication, *, include_enterprise=True) -> dict:
    _verify_publication_digest(row)
    payload = {
        "id": int(row.id), "draft_id": int(row.draft_id), "version_id": int(row.version_id),
        "version_number": int(row.version_number), "kind": row.kind, "scope": row.scope,
        "status": row.status, "title": row.title, "definition": json.loads(row.definition_json),
        "content_sha256": row.content_sha256, "requested_by": int(row.requested_by),
        "execution_available": False,
        "xiaoling_runtime_config_compatible": xiaoling_runtime_config_compatible(row),
        "decided_by": int(row.decided_by) if row.decided_by else None,
        "decided_at": row.decided_at, "decision_reason": row.decision_reason,
        "revoked_by": int(row.revoked_by) if row.revoked_by else None,
        "revoked_at": row.revoked_at, "revocation_reason": row.revocation_reason,
        "create_time": row.create_time,
    }
    if include_enterprise:
        payload["enterprise_id"] = int(row.enterprise_id)
    return payload


def _catalog_payload(row: EnterpriseAssetPublication, *, can_revoke=False) -> dict:
    """Expose only the shared immutable asset fields needed by catalog clients."""
    _verify_publication_digest(row)
    return {
        "id": int(row.id), "version_number": int(row.version_number), "kind": row.kind,
        "scope": row.scope, "title": row.title, "definition": json.loads(row.definition_json),
        "content_sha256": row.content_sha256, "execution_available": False,
        "xiaoling_runtime_config_compatible": xiaoling_runtime_config_compatible(row),
        "can_revoke": bool(can_revoke),
    }


def _verify_publication_snapshot(db, row: EnterpriseAssetPublication, *, require_active_draft=False) -> None:
    draft = db.query(EnterpriseAssetDraft).populate_existing().with_for_update().filter_by(
        id=row.draft_id, enterprise_id=row.enterprise_id,
    ).first()
    version = db.query(EnterpriseAssetDraftVersion).populate_existing().with_for_update().filter_by(
        id=row.version_id, draft_id=row.draft_id, version_number=row.version_number,
    ).first()
    if draft is None or version is None or draft.kind != row.kind:
        raise ConflictError("发布申请的草稿或版本关联已变化")
    if require_active_draft and draft.status != "active":
        raise ConflictError("草稿已删除，不能审批发布")
    if version.title != row.title or version.definition_json != row.definition_json:
        raise ConflictError("发布快照摘要与申请版本不一致")
    _verify_publication_digest(row)


def _require_active_requester(db, row: EnterpriseAssetPublication) -> None:
    # MySQL REPEATABLE READ 下，锁定读读取最新已提交的成员和账号状态，
    # 不受前面普通申请查询建立的旧快照影响。
    member = db.query(EnterpriseMember).populate_existing().with_for_update().filter_by(
        enterprise_id=row.enterprise_id, user_id=row.requested_by, status="active",
    ).first()
    account = db.query(User).populate_existing().with_for_update().filter_by(
        id=row.requested_by, status=1,
    ).first()
    if member is None or account is None:
        raise ForbiddenError("发布申请人已不再是有效企业成员")


def _event_publication(db, row: EnterpriseAssetPublication, actor_id: int, action: str) -> None:
    db.add(EnterpriseAuditEvent(
        enterprise_id=row.enterprise_id, actor_id=actor_id,
        action=action, subject_user_id=row.requested_by,
        detail={
            "publication_id": int(row.id), "draft_id": int(row.draft_id),
            "version_id": int(row.version_id), "scope": row.scope,
            "content_sha256": row.content_sha256,
        },
    ))


def request_publication(db: Session, enterprise_id: int, user_id: int, draft_id: int, *,
                        scope: str, expected_version: int, request_key: str):
    if scope not in {"enterprise", "global"}:
        raise ValidationError("发布范围仅支持企业内或全局")
    if type(expected_version) is not int or expected_version < 1:
        raise ValidationError("发布版本无效")
    if not isinstance(request_key, str) or re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,79}", request_key) is None:
        raise ValidationError("请求标识无效")
    if scope == "global" and rbac_service.is_super_admin_user(db, user_id):
        raise ForbiddenError("平台最高管理员不能提交自己的全局发布申请")

    # Serialize version selection, idempotency keys, and duplicate pending publication checks.
    _lock_member(db, enterprise_id, user_id)
    draft = _draft(db, enterprise_id, user_id, draft_id, lock=True)
    if draft.current_version != expected_version:
        raise ConflictError("草稿版本已变化，请刷新后重新申请")
    version = db.query(EnterpriseAssetDraftVersion).filter_by(
        draft_id=draft_id, version_number=expected_version,
    ).with_for_update().first()
    if version is None:
        raise ConflictError("申请版本不存在")
    content_sha256 = _content_hash(draft.kind, version.title, version.definition_json)
    digest = _digest(scope, enterprise_id, user_id, draft_id, version.id, expected_version, content_sha256)

    existing = db.query(EnterpriseAssetPublication).filter_by(
        enterprise_id=enterprise_id, requested_by=user_id, request_key=request_key,
    ).populate_existing().with_for_update().first()
    if existing:
        if existing.request_digest != digest:
            raise ConflictError("请求标识已用于其他发布申请")
        db.commit()
        return existing

    duplicate = db.query(EnterpriseAssetPublication).filter(
        EnterpriseAssetPublication.enterprise_id == enterprise_id,
        EnterpriseAssetPublication.draft_id == draft_id,
        EnterpriseAssetPublication.version_id == version.id,
        EnterpriseAssetPublication.scope == scope,
        EnterpriseAssetPublication.status.in_({"pending", "approved"}),
    ).with_for_update().first()
    if duplicate:
        db.commit()
        return duplicate

    if scope == "enterprise":
        alternate_approver = (db.query(EnterpriseMember.user_id)
            .join(User, User.id == EnterpriseMember.user_id)
            .filter(
                EnterpriseMember.enterprise_id == enterprise_id,
                EnterpriseMember.status == "active",
                EnterpriseMember.role.in_({"owner", "admin"}),
                EnterpriseMember.user_id != user_id,
                User.status == 1,
            ).first())
        if alternate_approver is None:
            raise ConflictError("企业内发布需要另一名有效 owner/admin 审批，请先设置审批人")

    row = EnterpriseAssetPublication(
        enterprise_id=enterprise_id, draft_id=draft_id, version_id=version.id,
        version_number=expected_version, requested_by=user_id, kind=draft.kind,
        scope=scope, status="pending", title=version.title,
        definition_json=version.definition_json, content_sha256=content_sha256,
        request_key=request_key, request_digest=digest,
    )
    db.add(row)
    db.flush()
    _event_publication(db, row, user_id, "enterprise_asset_publication_requested")
    db.commit()
    return row


def list_my_publications(db: Session, enterprise_id: int, user_id: int, *, offset=0, limit=100):
    require_enterprise_member(db, enterprise_id, user_id)
    return (db.query(EnterpriseAssetPublication).filter_by(
        enterprise_id=enterprise_id, requested_by=user_id,
    ).order_by(EnterpriseAssetPublication.id.desc()).offset(offset).limit(limit).all())


def withdraw_publication(db: Session, enterprise_id: int, publication_id: int, user_id: int):
    _lock_member(db, enterprise_id, user_id)
    row = _publication(db, enterprise_id, publication_id, lock=True)
    if row is None or int(row.requested_by) != int(user_id):
        raise NotFoundError("发布申请不存在")
    if row.status != "pending":
        raise ConflictError("只有待审批申请可以撤回")
    row.status = "withdrawn"
    row.withdrawn_at = _utcnow()
    _event_publication(db, row, user_id, "enterprise_asset_publication_withdrawn")
    db.commit()
    return row


def list_pending_enterprise_publications(db: Session, enterprise_id: int, user_id: int, *, offset=0, limit=100):
    require_enterprise_admin(db, enterprise_id, user_id)
    rows = (db.query(EnterpriseAssetPublication).filter_by(
        enterprise_id=enterprise_id, scope="enterprise", status="pending",
    ).filter(EnterpriseAssetPublication.requested_by != user_id)
        .order_by(EnterpriseAssetPublication.id.asc()).offset(offset).limit(limit).all())
    for row in rows:
        _verify_publication_digest(row)
    return rows


def _decide_publication(db, row: EnterpriseAssetPublication, user_id: int, *, approve: bool, reason: str):
    decision = "approved" if approve else "rejected"
    if not isinstance(reason, str) or len(reason) > 500:
        raise ValidationError("审批说明最多 500 个字符")
    if row.status == decision and int(row.decided_by or 0) == int(user_id) and row.decision_reason == reason.strip():
        return row
    if row.status != "pending":
        raise ConflictError("该发布申请已被处理，不能重复决定")
    if int(row.requested_by) == int(user_id):
        raise ForbiddenError("发布申请人不能审批自己的申请")
    # 批准需要来源和作者仍有效；拒绝仍须能终结已删草稿或离职申请，避免审批队列永久悬挂。
    if approve:
        _require_active_requester(db, row)
        _verify_publication_snapshot(db, row, require_active_draft=True)
    else:
        _verify_publication_digest(row)
    row.status = decision
    row.decided_by = int(user_id)
    row.decided_at = _utcnow()
    row.decision_reason = reason.strip()
    _event_publication(
        db, row, user_id,
        "enterprise_asset_publication_approved" if approve else "enterprise_asset_publication_rejected",
    )
    db.commit()
    return row


def decide_enterprise_publication(db: Session, enterprise_id: int, publication_id: int, user_id: int, *,
                                  approve: bool, reason: str = ""):
    _company(db, enterprise_id, lock=True)
    require_enterprise_admin(db, enterprise_id, user_id, lock=True)
    row = _publication(db, enterprise_id, publication_id, lock=True)
    if row is None or row.scope != "enterprise":
        raise NotFoundError("企业内发布申请不存在")
    result = _decide_publication(db, row, user_id, approve=approve, reason=reason)
    db.commit()
    return result


def list_enterprise_catalog(db: Session, enterprise_id: int, user_id: int, *, offset=0, limit=100):
    require_enterprise_member(db, enterprise_id, user_id)
    rows = (db.query(EnterpriseAssetPublication).filter_by(
        enterprise_id=enterprise_id, scope="enterprise", status="approved",
    ).order_by(EnterpriseAssetPublication.id.desc()).offset(offset).limit(limit).all())
    for row in rows:
        _verify_publication_digest(row)
    return rows


def list_global_catalog(db: Session, *, offset=0, limit=100, before_id=None):
    query = db.query(EnterpriseAssetPublication).filter_by(
        scope="global", status="approved",
    )
    if before_id is not None:
        query = query.filter(EnterpriseAssetPublication.id < before_id)
    rows = query.order_by(EnterpriseAssetPublication.id.desc()).offset(offset).limit(limit).all()
    for row in rows:
        _verify_publication_digest(row)
    return rows


def search_runtime_catalog(db: Session, *, query: str, user_id: int,
                           enterprise_id: int | None, kind: str | None = None,
                           limit: int = 10) -> list[EnterpriseAssetPublication]:
    """Search approved catalog titles in SQL, before applying bounded results."""
    if (not isinstance(query, str) or not query.strip() or len(query) > 100
            or (kind is not None and kind not in {"agent", "skill"})
            or type(limit) is not int or not 1 <= limit <= 10):
        raise ValidationError("发布目录搜索条件无效")
    account = db.query(User.id).filter_by(id=user_id, status=1).first()
    if account is None:
        raise ForbiddenError("当前账号不可访问已发布资产")

    scopes: list[tuple[str, int | None]] = [("global", None)]
    if enterprise_id is not None:
        company = _company(db, int(enterprise_id), lock=True)
        if company.status != "active":
            raise ForbiddenError("企业已暂停，不能搜索企业 Agent/Skill 目录")
        require_enterprise_member(db, int(enterprise_id), int(user_id), lock=True)
        scopes.insert(0, ("enterprise", int(enterprise_id)))

    matches: list[EnterpriseAssetPublication] = []
    for scope, scoped_enterprise_id in scopes:
        filters = [
            EnterpriseAssetPublication.scope == scope,
            EnterpriseAssetPublication.status == "approved",
            EnterpriseAssetPublication.title.contains(query.strip(), autoescape=True),
        ]
        if scoped_enterprise_id is not None:
            filters.append(EnterpriseAssetPublication.enterprise_id == scoped_enterprise_id)
        if kind is not None:
            filters.append(EnterpriseAssetPublication.kind == kind)
        rows = (db.query(EnterpriseAssetPublication).filter(*filters)
                .order_by(EnterpriseAssetPublication.id.desc()).limit(limit).all())
        for row in rows:
            _verify_publication_digest(row)
        matches.extend(rows)
    matches.sort(key=lambda row: int(row.id), reverse=True)
    return matches[:limit]


_MAX_RUNTIME_PROMPT_BYTES = 4000


def _runtime_prompt_is_compatible(prompt: object) -> bool:
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
        return False
    try:
        return len(prompt.encode("utf-8")) <= _MAX_RUNTIME_PROMPT_BYTES
    except UnicodeEncodeError:
        return False


def _runtime_definition(kind: str, definition_json: str) -> dict:
    """Parse the deliberately small, non-tool executable asset contract.

    Enterprise publications predate this runtime contract and may contain arbitrary
    legacy JSON. They remain visible in catalogs but are never executable unless the
    immutable snapshot matches this exact versioned schema.
    """
    try:
        value = json.loads(definition_json)
    except (TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise ConflictError("发布资产配置损坏，拒绝执行") from exc
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int \
            or value.get("schema_version") != 1:
        raise ConflictError("发布资产没有受支持的运行时配置版本")
    if kind == "agent":
        allowed = {"schema_version", "runtime", "system_prompt"}
        prompt = value.get("system_prompt")
        valid = value.get("runtime") == "prompt" and _runtime_prompt_is_compatible(prompt)
    elif kind == "skill":
        allowed = {"schema_version", "skill_type", "prompt"}
        prompt = value.get("prompt")
        valid = value.get("skill_type") == "llm_transform" and _runtime_prompt_is_compatible(prompt)
    else:
        raise ConflictError("发布资产类型不支持运行")
    if set(value) != allowed or not valid:
        raise ConflictError("发布资产运行配置无效或包含未授权字段")
    return value


def xiaoling_runtime_config_compatible(row: EnterpriseAssetPublication) -> bool:
    """Report only immutable config-format/size compatibility, never runtime availability."""
    if row.status != "approved" or row.revoked_at is not None:
        return False
    try:
        _runtime_definition(row.kind, row.definition_json)
    except ConflictError:
        return False
    return True


def resolve_runtime_publication(db: Session, publication_id: int, *, user_id: int,
                                enterprise_id: int | None = None) -> dict:
    """Resolve one approved immutable publication inside the caller's live scope.

    This is a server-only resolver for the staged runtime integration. It never
    resolves by title/code or latest draft version and deliberately does not route
    a model, tool, provider, or API key from publication data.
    """
    account = db.query(User).filter_by(id=user_id, status=1).first()
    if account is None:
        raise ForbiddenError("当前账号不可访问已发布资产")
    candidate = db.query(EnterpriseAssetPublication).filter_by(id=publication_id).first()
    if candidate is None:
        raise NotFoundError("已发布资产不存在或已撤销")
    if candidate.scope == "enterprise":
        if enterprise_id is None or int(candidate.enterprise_id) != int(enterprise_id):
            raise NotFoundError("企业内发布资产不存在")
        company = _company(db, int(enterprise_id), lock=True)
        if company.status != "active":
            raise ForbiddenError("企业已暂停，不能使用企业发布资产")
        require_enterprise_member(db, int(enterprise_id), int(user_id), lock=True)
    elif candidate.scope != "global":
        raise NotFoundError("已发布资产不存在或发布范围无效")
    elif enterprise_id is not None:
        # A global asset is public to signed-in accounts, but an enterprise chat
        # invocation still needs a live actor in its originating enterprise.
        company = _company(db, int(enterprise_id), lock=True)
        if company.status != "active":
            raise ForbiddenError("企业已暂停，不能在企业会话中使用全局资产")
        require_enterprise_member(db, int(enterprise_id), int(user_id), lock=True)

    # Enterprise publication mutation follows company -> publication lock order.
    # Reload after authorization so a concurrent revocation cannot use the stale
    # catalog snapshot selected above.
    row = (db.query(EnterpriseAssetPublication).filter_by(
        id=publication_id, status="approved",
    ).populate_existing().with_for_update().first())
    if row is None or row.revoked_at is not None or row.scope != candidate.scope:
        raise NotFoundError("已发布资产不存在或已撤销")
    if row.scope == "enterprise" and int(row.enterprise_id) != int(enterprise_id or 0):
        raise NotFoundError("企业内发布资产不存在")

    _verify_publication_snapshot(db, row)
    definition = _runtime_definition(row.kind, row.definition_json)
    return {
        "publication_id": int(row.id), "kind": row.kind, "scope": row.scope,
        "enterprise_id": int(row.enterprise_id) if row.scope == "enterprise" else None,
        "version_id": int(row.version_id), "version_number": int(row.version_number),
        "title": row.title, "content_sha256": row.content_sha256,
        "definition": definition,
    }


def _require_platform_super_admin(db: Session, user_id: int) -> None:
    if not rbac_service.is_super_admin_user(db, user_id):
        raise ForbiddenError("仅平台最高管理员可以审批或撤销全局发布")


def list_global_publications(db: Session, user_id: int, *, status="pending", offset=0, limit=100):
    _require_platform_super_admin(db, user_id)
    if status not in {"pending", "approved", "rejected", "revoked"}:
        raise ValidationError("发布状态无效")
    rows = (db.query(EnterpriseAssetPublication).filter_by(
        scope="global", status=status,
    ).filter(EnterpriseAssetPublication.requested_by != user_id)
        .order_by(EnterpriseAssetPublication.id.asc()).offset(offset).limit(limit).all())
    for row in rows:
        _verify_publication_digest(row)
    return rows


def decide_global_publication(db: Session, publication_id: int, user_id: int, *, approve: bool, reason: str = ""):
    _require_platform_super_admin(db, user_id)
    # 与企业成员变更及企业内审批保持同一锁顺序：先锁企业，再锁申请。
    # 前置普通读只用于查出企业 ID；随后资格检查通过锁定读读取当前状态。
    initial = db.query(EnterpriseAssetPublication.enterprise_id).filter_by(
        id=publication_id, scope="global",
    ).first()
    if initial is None:
        raise NotFoundError("全局发布申请不存在")
    _company(db, int(initial.enterprise_id), lock=True)
    row = db.query(EnterpriseAssetPublication).filter_by(
        id=publication_id, scope="global",
    ).populate_existing().with_for_update().first()
    if row is None:
        raise NotFoundError("全局发布申请不存在")
    result = _decide_publication(db, row, user_id, approve=approve, reason=reason)
    db.commit()
    return result


def _revoke_publication(db, row: EnterpriseAssetPublication, user_id: int, reason: str):
    if not isinstance(reason, str) or len(reason) > 500:
        raise ValidationError("撤销说明最多 500 个字符")
    if row.status == "revoked" and int(row.revoked_by or 0) == int(user_id) and row.revocation_reason == reason.strip():
        return row
    if row.status != "approved":
        raise ConflictError("只有已发布资产可以撤销")
    row.status = "revoked"
    row.revoked_by = int(user_id)
    row.revoked_at = _utcnow()
    row.revocation_reason = reason.strip()
    _event_publication(db, row, user_id, "enterprise_asset_publication_revoked")
    db.commit()
    return row


def revoke_enterprise_publication(db: Session, enterprise_id: int, publication_id: int, user_id: int, *,
                                  reason: str = ""):
    _lock_member(db, enterprise_id, user_id)
    row = _publication(db, enterprise_id, publication_id, lock=True)
    if row is None or row.scope != "enterprise":
        raise NotFoundError("企业内发布资产不存在")
    if int(row.requested_by) != int(user_id):
        require_enterprise_admin(db, enterprise_id, user_id, lock=True)
    result = _revoke_publication(db, row, user_id, reason)
    db.commit()
    return result


def revoke_global_publication(db: Session, publication_id: int, user_id: int, *, reason: str = ""):
    _require_platform_super_admin(db, user_id)
    row = db.query(EnterpriseAssetPublication).filter_by(
        id=publication_id, scope="global",
    ).populate_existing().with_for_update().first()
    if row is None:
        raise NotFoundError("全局发布资产不存在")
    result = _revoke_publication(db, row, user_id, reason)
    db.commit()
    return result
