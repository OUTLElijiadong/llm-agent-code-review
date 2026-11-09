"""Internal durable clarification evidence. Not an execution authorization gate.

Callers must lock and revalidate these records inside the actual side-effect or
send transaction; ``recorded`` is evidence only, not an authorization grant.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.enterprise_assistant_clarification import (
    EnterpriseAssistantClarification,
    EnterpriseAssistantClarificationDecision,
    EnterpriseAssistantClarificationReview,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage
from app.models.enterprise_org import EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services import enterprise_chat_service, enterprise_org_service
from app.services.enterprise_assistant_runtime_service import _digest, _json, _role_snapshot


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)



def _version_fingerprint(db: Session, room, enterprise_id: int, actor_ids: set[int]) -> str:
    """Lock and hash current authority versions, separate from runtime snapshots."""
    company = enterprise_org_service._company(db, enterprise_id, lock=True)
    users = (db.query(User.id, User.clarification_authority_version).filter(User.id.in_(actor_ids))
             .order_by(User.id).with_for_update(read=True).all())
    members = (db.query(EnterpriseMember.id, EnterpriseMember.user_id, EnterpriseMember.clarification_authority_version)
               .filter(EnterpriseMember.enterprise_id == enterprise_id, EnterpriseMember.user_id.in_(actor_ids))
               .order_by(EnterpriseMember.user_id).with_for_update(read=True).all())
    groups = []
    if room.kind == "group" and room.group_id:
        groups = (db.query(EnterpriseGroupMember.id, EnterpriseGroupMember.user_id,
                           EnterpriseGroupMember.clarification_authority_version)
                  .filter(EnterpriseGroupMember.enterprise_id == enterprise_id,
                          EnterpriseGroupMember.group_id == room.group_id,
                          EnterpriseGroupMember.user_id.in_(actor_ids))
                  .order_by(EnterpriseGroupMember.user_id).with_for_update(read=True).all())
    return _digest({"company": [int(company.id), int(company.clarification_authority_version)],
                    "users": [[int(r.id), int(r.clarification_authority_version)] for r in users],
                    "members": [[int(r.id), int(r.user_id), int(r.clarification_authority_version)] for r in members],
                    "groups": [[int(r.id), int(r.user_id), int(r.clarification_authority_version)] for r in groups]})

def _context(db: Session, run: EnterpriseAssistantRun):
    enterprise_org_service._company(db, int(run.enterprise_id), lock=True)
    room = enterprise_chat_service.get_conversation(
        db, int(run.enterprise_id), int(run.initiator_user_id), int(run.conversation_id),
    )
    state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).first()
    saved = json.loads(state.permission_snapshot_json or "{}") if state else {}
    frozen = saved.get("messages") if isinstance(saved, dict) else None
    query = db.query(EnterpriseChatMessage).filter_by(
        enterprise_id=run.enterprise_id, conversation_id=run.conversation_id, sender_type="human")
    if isinstance(frozen, list):
        if len(frozen) > 30 or _digest(saved) != state.permission_snapshot_sha256:
            raise ForbiddenError("小菱会话快照无效")
        ids = [int(row["message_id"]) for row in frozen if row.get("sender_type") != "assistant"]
        if int(run.source_message_id) not in ids:
            raise ForbiddenError("小菱来源消息不在会话快照中")
        query = query.filter(EnterpriseChatMessage.id.in_(ids), EnterpriseChatMessage.id <= run.source_message_id)
    messages = query.order_by(EnterpriseChatMessage.id.asc()).populate_existing().with_for_update().all()
    snapshot = _role_snapshot(db, enterprise_id=int(run.enterprise_id), room=room, messages=messages,
                              initiator_id=int(run.initiator_user_id), lock_current=True)
    rows = {int(row["message_id"]): row for row in snapshot["messages"]}
    source = rows.get(int(run.source_message_id))
    if not source or int(source.get("sender_id") or 0) != int(run.initiator_user_id):
        raise ForbiddenError("小菱来源消息或发起人权限已变化")
    initiator_rank = int(source.get("rank") or 0)
    if initiator_rank <= 0:
        raise ForbiddenError("小菱发起人不再具有会话权限")
    candidates = [message for message in messages if int(message.sender_id or 0) != int(run.initiator_user_id)
                  and int(rows[int(message.id)].get("rank") or 0) >= initiator_rank
                  and (not isinstance(frozen, list) or "@小菱" in (message.content or ""))]
    source_message = next((message for message in messages if int(message.id) == int(run.source_message_id)), None)
    if source_message is None:
        raise ForbiddenError("小菱来源消息已变化")
    fingerprint = _digest({
        "initiator_rank": initiator_rank,
        "initiator_role": source.get("member_role"),
        "initiator_group_role": source.get("group_role"),
        "source_content": _digest({"text": source_message.content}),
        "messages": [{"id": int(message.id), "author": int(message.sender_id),
                      "rank": int(rows[int(message.id)]["rank"]),
                      "member_role": rows[int(message.id)].get("member_role"),
                      "group_role": rows[int(message.id)].get("group_role"),
                      "content": _digest({"text": message.content})}
                     for message in candidates],
    })
    return room, candidates, rows, fingerprint


def model_send_evidence(db: Session, run: EnterpriseAssistantRun) -> dict[str, Any]:
    """Fingerprint all frozen input and authority rows for the run-level send gate."""
    room, candidates, rows, _candidate_fingerprint = _context(db, run)
    state = (db.query(EnterpriseAssistantRuntimeState)
             .filter_by(assistant_run_id=int(run.id), enterprise_id=int(run.enterprise_id),
                        conversation_id=int(run.conversation_id))
             .populate_existing().with_for_update().first())
    if state is None:
        raise ForbiddenError("小菱运行检查点不存在")
    saved = json.loads(state.permission_snapshot_json or "{}")
    frozen = saved.get("messages") if isinstance(saved, dict) else None
    if not isinstance(frozen, list) or len(frozen) > 30 or _digest(saved) != state.permission_snapshot_sha256:
        raise ForbiddenError("小菱会话快照无效")
    try:
        checkpoint = json.loads(state.checkpoint_json or "{}")
    except (TypeError, ValueError) as exc:
        raise ForbiddenError("小菱运行检查点无效") from exc
    if not isinstance(checkpoint, dict):
        raise ForbiddenError("小菱运行检查点无效")
    request_definition_sha256 = _digest({
        "run_model": str(run.model_name or ""),
        "checkpoint_model": checkpoint.get("model"),
        "instructions": checkpoint.get("instructions"),
        "tools": checkpoint.get("tools"),
    })
    frozen_ids = [int(item["message_id"]) for item in frozen if isinstance(item, dict)
                  and item.get("sender_type") != "assistant"]
    messages = (db.query(EnterpriseChatMessage)
                .filter(EnterpriseChatMessage.enterprise_id == int(run.enterprise_id),
                        EnterpriseChatMessage.conversation_id == int(run.conversation_id),
                        EnterpriseChatMessage.id.in_(frozen_ids))
                .order_by(EnterpriseChatMessage.id).populate_existing().with_for_update().all())
    if [int(row.id) for row in messages] != frozen_ids:
        raise ForbiddenError("小菱冻结消息已变化")
    actor_ids = {int(run.initiator_user_id)}
    for row in messages:
        if row.sender_type == "human" and row.sender_id is not None:
            actor_ids.add(int(row.sender_id))
    versions = _version_fingerprint(db, room, int(run.enterprise_id), actor_ids)
    message_evidence = [{
        "message_id": int(row.id),
        "sender_id": int(row.sender_id) if row.sender_id is not None else None,
        "sender_type": str(row.sender_type),
        "content_sha256": _digest({"content": str(row.content or "")}),
        "rank": int(rows.get(int(row.id), {}).get("rank") or 0),
        "member_role": rows.get(int(row.id), {}).get("member_role"),
        "group_role": rows.get(int(row.id), {}).get("group_role"),
    } for row in messages]
    fingerprint = _digest({
        "run_id": str(run.public_id),
        "source_message_id": int(run.source_message_id),
        "prompt_sha256": str(run.prompt_sha256),
        "permission_snapshot_sha256": str(state.permission_snapshot_sha256),
        "request_definition_sha256": request_definition_sha256,
        "messages": message_evidence,
        "candidate_ids": [int(row.id) for row in candidates],
    })
    return {
        "fingerprint": fingerprint,
        "authority_version_sha256": versions,
        "request_definition_sha256": request_definition_sha256,
        "candidate_message_ids": [int(row.id) for row in candidates],
        "candidates": candidates,
        "rows": rows,
    }


def _run(db: Session, enterprise_id: int, conversation_id: int, run_id: int) -> EnterpriseAssistantRun:
    run = db.query(EnterpriseAssistantRun).filter_by(
        id=run_id, enterprise_id=enterprise_id, conversation_id=conversation_id,
    ).populate_existing().with_for_update().first()
    if run is None:
        raise NotFoundError("企业小菱运行不存在")
    state = db.query(EnterpriseAssistantRuntimeState).filter_by(
        assistant_run_id=run_id, enterprise_id=enterprise_id, conversation_id=conversation_id,
    ).populate_existing().with_for_update().first()
    if state is None or state.stop_requested or state.status in {"cancelled", "completed", "suppressed"}:
        raise ForbiddenError("企业小菱运行已停止或不可澄清")
    return run


def create_request(db: Session, *, enterprise_id: int, conversation_id: int, run_id: int, phase: str,
                   call_id: str, action: str, target: Mapping[str, Any], arguments: Mapping[str, Any],
                   expires_at: datetime) -> EnterpriseAssistantClarification:
    if (phase not in {"model_send", "tool"} or not call_id or len(call_id) > 120 or not action
            or len(action) > 80 or not isinstance(target, Mapping) or not isinstance(arguments, Mapping)
            or expires_at <= _now()):
        raise ValidationError("澄清动作或有效期无效")
    run = _run(db, enterprise_id, conversation_id, run_id)
    room, messages, _rows, fingerprint = _context(db, run)
    authority_versions = _version_fingerprint(db, room, enterprise_id,
        {int(run.initiator_user_id), *(int(m.sender_id) for m in messages)})
    target_json = _json(dict(target))
    arguments_sha = _digest({"action": action, "target": dict(target), "arguments": dict(arguments)})
    existing = db.query(EnterpriseAssistantClarification).filter_by(
        assistant_run_id=run_id, phase=phase, call_id=call_id,
    ).order_by(EnterpriseAssistantClarification.version.desc()).populate_existing().with_for_update().first()
    version = 1
    if phase == "model_send":
        evidence = model_send_evidence(db, run)
        fingerprint = str(evidence["fingerprint"])
        authority_versions = str(evidence["authority_version_sha256"])
    if existing is not None:
        if (existing.arguments_sha256 != arguments_sha or existing.target_json != target_json
                or existing.action != action):
            raise ConflictError("同一澄清调用的动作或参数已变化")
        if existing.status == "cancelled":
            raise ConflictError("已取消的澄清不能重新启用")
        if (existing.status == "pending" and existing.authority_sha256 == fingerprint
                and existing.authority_version_sha256 == authority_versions and existing.expires_at > _now()):
            return existing
        existing.status = "superseded"
        version = int(existing.version) + 1
    record = EnterpriseAssistantClarification(
        assistant_run_id=run_id, enterprise_id=enterprise_id, conversation_id=conversation_id,
        source_message_id=run.source_message_id, initiator_user_id=run.initiator_user_id, phase=phase,
        call_id=call_id, action=action, target_json=target_json, arguments_json=_json(dict(arguments)),
        arguments_sha256=arguments_sha,
        authority_sha256=fingerprint, authority_version_sha256=authority_versions, version=version,
        status="pending", expires_at=expires_at,
    )
    db.add(record)
    db.flush()
    db.add_all([EnterpriseAssistantClarificationDecision(
        request_id=record.id, enterprise_id=enterprise_id, conversation_id=conversation_id,
        source_message_id=message.id, author_user_id=message.sender_id, decision="pending",
    ) for message in messages])
    db.flush()
    return record


def _record(db: Session, enterprise_id: int, conversation_id: int, request_id: int, *, lock: bool = False):
    query = db.query(EnterpriseAssistantClarification).filter_by(
        id=request_id, enterprise_id=enterprise_id, conversation_id=conversation_id,
    ).populate_existing()
    record = (query.with_for_update() if lock else query).first()
    if record is None:
        raise NotFoundError("澄清请求不存在")
    return record


def status(db: Session, *, enterprise_id: int, conversation_id: int, request_id: int) -> str:
    record = _record(db, enterprise_id, conversation_id, request_id)
    run = _run(db, enterprise_id, conversation_id, int(record.assistant_run_id))
    room, messages, rows, current = _context(db, run)
    record = _record(db, enterprise_id, conversation_id, request_id, lock=True)
    if int(record.assistant_run_id) != int(run.id):
        raise ConflictError("澄清关联运行已变化")
    if record.expires_at <= _now():
        return "expired"
    if record.status != "pending":
        return record.status
    versions = _version_fingerprint(db, room, enterprise_id,
        {int(run.initiator_user_id), *(int(m.sender_id) for m in messages)})
    if record.phase == "model_send":
        evidence = model_send_evidence(db, run)
        current = str(evidence["fingerprint"])
        versions = str(evidence["authority_version_sha256"])
    if (current != record.authority_sha256 or not record.authority_version_sha256
            or versions != record.authority_version_sha256):
        return "stale"
    try:
        target = json.loads(record.target_json)
        arguments = json.loads(record.arguments_json)
    except (TypeError, ValueError):
        return "stale"
    if (not isinstance(target, dict) or not isinstance(arguments, dict)
            or _digest({"action": record.action, "target": target, "arguments": arguments}) != record.arguments_sha256):
        return "stale"
    decisions = (db.query(EnterpriseAssistantClarificationDecision).filter_by(request_id=request_id)
                 .order_by(EnterpriseAssistantClarificationDecision.id.asc())
                 .populate_existing().with_for_update().all())
    expected = {int(message.id): int(message.sender_id) for message in messages}
    recorded = {int(row.source_message_id): int(row.author_user_id) for row in decisions}
    if expected != recorded:
        return "stale"
    decider_ids = {int(row.decided_by) for row in decisions if row.decided_by is not None}
    actors = [SimpleNamespace(id=-user_id, sender_id=user_id, sender_type="human") for user_id in decider_ids]
    actor_snapshot = _role_snapshot(db, enterprise_id=enterprise_id, room=room, messages=actors,
                                    initiator_id=int(run.initiator_user_id), lock_current=True)
    ranks = {int(item["sender_id"]): int(item["rank"]) for item in actor_snapshot["messages"]}
    for row in decisions:
        if row.decision not in {"pending", "allow", "deny", "unrelated"}:
            return "stale"
        if row.decided_by is None:
            if row.decision != "pending" or row.decided_at is not None:
                return "stale"
            continue
        if row.decision == "pending" or row.decided_at is None:
            return "stale"
        decider = int(row.decided_by)
        try:
            enterprise_chat_service.get_conversation(db, enterprise_id, decider, conversation_id)
        except (ForbiddenError, NotFoundError):
            return "stale"
        author_rank = int(rows.get(int(row.source_message_id), {}).get("rank") or 0)
        rank = ranks.get(decider, 0)
        if (not row.decider_version_sha256
                or row.decider_version_sha256 != _version_fingerprint(db, room, enterprise_id, {decider})
                or rank <= 0 or rank != int(row.decider_rank or 0) or author_rank != int(row.author_rank or 0)
                or (decider != int(row.author_user_id) and rank <= author_rank)):
            return "stale"
    reviews = (db.query(EnterpriseAssistantClarificationReview).filter_by(request_id=request_id)
               .order_by(EnterpriseAssistantClarificationReview.id).populate_existing().with_for_update().all())
    effective = {int(row.source_message_id): row.decision for row in decisions}
    previous_rank = {int(row.source_message_id): int(row.decider_rank or 0) for row in decisions}
    previous_decider = {int(row.source_message_id): int(row.decided_by or 0) for row in decisions}
    authors = {int(row.source_message_id): int(row.author_user_id) for row in decisions}
    for review in reviews:
        source_id = int(review.source_message_id)
        if source_id not in effective or effective[source_id] == "pending":
            return "stale"
        try:
            enterprise_chat_service.get_conversation(db, enterprise_id, int(review.decided_by), conversation_id)
        except (ForbiddenError, NotFoundError):
            return "stale"
        actor = SimpleNamespace(id=-int(review.decided_by), sender_id=int(review.decided_by), sender_type="human")
        snap = _role_snapshot(db, enterprise_id=enterprise_id, room=room, messages=[actor],
                              initiator_id=int(run.initiator_user_id), lock_current=True)
        rank = int(snap["messages"][0]["rank"])
        actor_id = int(review.decided_by)
        revision = (review.review_kind == "author_revision" and actor_id == authors[source_id]
                    and previous_decider[source_id] == actor_id and rank == int(rows[source_id]["rank"]))
        higher = (review.review_kind == "higher_review" and rank > previous_rank[source_id]
                  and rank > int(rows[source_id]["rank"]))
        if (rank != int(review.decider_rank) or not (revision or higher)
                or review.decider_version_sha256 != _version_fingerprint(db, room, enterprise_id, {actor_id})):
            return "stale"
        effective[source_id] = review.decision
        previous_rank[source_id] = rank
        previous_decider[source_id] = actor_id
    if any(value == "deny" for value in effective.values()):
        return "denied"
    return "waiting" if any(value == "pending" for value in effective.values()) else "recorded"


def decision_status(db: Session, *, enterprise_id: int, conversation_id: int, request_id: int) -> str:
    """Evidence completeness only; 'complete' grants no authority to execute."""
    value = status(db, enterprise_id=enterprise_id, conversation_id=conversation_id, request_id=request_id)
    return "complete" if value == "recorded" else value


def decide(db: Session, *, enterprise_id: int, conversation_id: int, request_id: int, source_message_id: int,
           user_id: int, decision: str) -> None:
    if decision not in {"allow", "deny", "unrelated"}:
        raise ValidationError("未知的澄清决定")
    record = _record(db, enterprise_id, conversation_id, request_id)
    current_status = status(db, enterprise_id=enterprise_id, conversation_id=conversation_id, request_id=request_id)
    if current_status not in {"waiting", "recorded", "denied"}:
        raise ConflictError("澄清请求已结束、过期或会话权限已变化")
    run = _run(db, enterprise_id, conversation_id, int(record.assistant_run_id))
    room, messages, rows, _fingerprint = _context(db, run)
    enterprise_chat_service.get_conversation(db, enterprise_id, user_id, conversation_id)
    row = db.query(EnterpriseAssistantClarificationDecision).filter_by(
        request_id=request_id, source_message_id=source_message_id,
    ).populate_existing().with_for_update().first()
    if row is None or source_message_id not in rows:
        raise NotFoundError("澄清来源不存在")
    author_rank = int(rows[source_message_id].get("rank") or 0)
    actor_message = SimpleNamespace(id=-1, sender_id=user_id, sender_type="human")
    author_snapshot = _role_snapshot(db, enterprise_id=enterprise_id, room=room, messages=[*messages, actor_message],
                                     initiator_id=user_id, lock_current=True)
    decider_rank = max((int(item.get("rank") or 0) for item in author_snapshot["messages"]
                        if int(item.get("sender_id") or 0) == user_id), default=0)
    if user_id != int(row.author_user_id) and decider_rank <= author_rank:
        raise ForbiddenError("仅来源作者或会话内更高权限成员可以澄清")
    if user_id == int(row.author_user_id) and author_rank <= 0:
        raise ForbiddenError("来源作者已失去会话权限")
    if row.decision != "pending":
        reviews = (db.query(EnterpriseAssistantClarificationReview).filter_by(request_id=request_id,
                   source_message_id=source_message_id).order_by(EnterpriseAssistantClarificationReview.id)
                   .populate_existing().with_for_update().all())
        latest = reviews[-1] if reviews else row
        if latest.decision == decision and int(latest.decided_by or 0) == user_id:
            return
        own_revision = user_id == int(row.author_user_id) and int(latest.decided_by or 0) == user_id
        higher_review = (decider_rank > int(latest.decider_rank or 0)
                         and decider_rank > author_rank)
        if not (own_revision or higher_review):
            raise ConflictError("仅作者本人可以修订自己的决定，或更高权限成员可以复核已有决定")
        db.add(EnterpriseAssistantClarificationReview(
            request_id=request_id, enterprise_id=enterprise_id, conversation_id=conversation_id,
            source_message_id=source_message_id, decided_by=user_id, decider_rank=decider_rank,
            review_kind="author_revision" if own_revision else "higher_review",
            decider_version_sha256=_version_fingerprint(db, room, enterprise_id, {user_id}),
            decision=decision, decided_at=_now()))
        db.flush()
        return
    version_fingerprint = _version_fingerprint(db, room, enterprise_id, {user_id})
    row.decision = decision
    row.decided_by = user_id
    row.author_rank = author_rank
    row.decider_rank = decider_rank
    row.decider_version_sha256 = version_fingerprint
    row.decided_at = _now()
    db.flush()
