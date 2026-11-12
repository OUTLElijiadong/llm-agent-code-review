"""企业会话 API；企业小菱运行与个人 Agent 执行权限严格分离。"""
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query
from pydantic import Field, StrictBool, StrictInt
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ForbiddenError
from app.models.enterprise_org import EnterpriseGroup
from app.models.user import User
from app.schemas.common import Resp, StrictInputModel
from app.services import enterprise_assistant_billing_group as billing_group
from app.services import enterprise_assistant_proactive_service as proactive_service
from app.services import enterprise_assistant_runtime_service as assistant_runtime
from app.services import enterprise_assistant_service as assistant_service
from app.services import enterprise_chat_service as service
from app.services import enterprise_org_service

router = APIRouter(prefix="/enterprises/{enterprise_id}/conversations", tags=["enterprise-chat"])


class ConversationIn(StrictInputModel):
    kind: Literal["company", "group", "direct"]
    group_id: int | None = Field(default=None, gt=0)
    other_user_id: int | None = Field(default=None, gt=0)


class MessageIn(StrictInputModel):
    client_message_id: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=10000)


class AssistantRunIn(StrictInputModel):
    source_message_id: int = Field(gt=0)


class AssistantAnswerIn(StrictInputModel):
    call_id: str = Field(min_length=1, max_length=120)
    answer: str = Field(min_length=1, max_length=4000)


class AssistantCancelIn(StrictInputModel):
    reason: str = Field(default="", max_length=500)


class AssistantApprovalDecisionIn(StrictInputModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=500)


class AssistantClarificationDecisionIn(StrictInputModel):
    source_message_id: StrictInt = Field(gt=0)
    decision: Literal["allow", "deny", "unrelated"]


class ProactivePolicyIn(StrictInputModel):
    enabled: StrictBool
    monthly_credits: StrictInt = Field(ge=0, le=100_000_000)
    expected_version: StrictInt = Field(ge=0)


def _conversation(row):
    return {"id": row.id, "enterprise_id": row.enterprise_id, "kind": row.kind, "group_id": row.group_id,
            "participant_low_id": row.participant_low_id, "participant_high_id": row.participant_high_id,
            "create_time": row.create_time}


def _conversation_with_latest_message(row, latest_message):
    data = _conversation(row)
    if latest_message is None:
        data.update(last_message_id=None, last_message_preview=None, last_message_at=None,
                    last_message_sender_id=None, last_message_sender_type=None)
        return data
    preview = " ".join((latest_message.content or "").split())[:160]
    data.update(last_message_id=latest_message.id, last_message_preview=preview,
                last_message_at=latest_message.create_time, last_message_sender_id=latest_message.sender_id,
                last_message_sender_type=latest_message.sender_type)
    return data


def _message(row):
    return {"id": row.id, "conversation_id": row.conversation_id, "sender_id": row.sender_id,
            "sender_type": row.sender_type, "assistant_run_id": row.assistant_run_id,
            "client_message_id": row.client_message_id, "content": row.content, "create_time": row.create_time}


def _run_result(result):
    data = dict(result)
    if data.get("message") is not None:
        data["message"] = _message(data["message"])
    data["assistant_mode"] = "enterprise_agent_runtime"
    return data


def _require_current_room_member(db: Session, enterprise_id: int, conversation_id: int, user_id: int):
    """Route-level membership check; the service repeats it at each state transition."""
    return service.get_conversation(db, enterprise_id, user_id, conversation_id)


@router.get("", response_model=Resp[dict])
def list_conversations(enterprise_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = service.list_conversations(db, enterprise_id, user.id)
    latest_messages = service.latest_messages_by_conversation(
        db, enterprise_id, [int(row.id) for row in rows],
    )
    items = [_conversation_with_latest_message(row, latest_messages.get(int(row.id))) for row in rows]
    items.sort(key=lambda item: (item["last_message_at"] or item["create_time"],
                                 item["last_message_id"] or 0, item["id"]), reverse=True)
    return Resp(data={"items": items,
                      "assistant_available": assistant_service.capability_available(db, enterprise_id),
                      "assistant_mode": "enterprise_agent_runtime"})


@router.get("/proactive-settings", response_model=Resp[dict])
def proactive_settings(enterprise_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data=proactive_service.get_policy(db, enterprise_id, int(user.id)))


@router.put("/proactive-settings", response_model=Resp[dict])
def update_proactive_settings(enterprise_id: int, payload: ProactivePolicyIn,
                              db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    result = proactive_service.set_policy(db, enterprise_id, int(user.id), enabled=payload.enabled,
                                          monthly_credits=payload.monthly_credits,
                                          expected_version=payload.expected_version,
                                          expected_token_version=getattr(user, "_authenticated_token_version", None))
    db.commit()
    return Resp(data=result)


@router.post("", response_model=Resp[dict])
def open_conversation(enterprise_id: int, payload: ConversationIn, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    row = service.open_conversation(db, enterprise_id, user.id, payload.kind, group_id=payload.group_id,
                                    other_user_id=payload.other_user_id)
    return Resp(data=_conversation(row))


@router.get("/{conversation_id}/messages", response_model=Resp[dict])
def list_messages(enterprise_id: int, conversation_id: int, after_id: int | None = Query(None, ge=0),
                  before_id: int | None = Query(None, gt=0),
                  limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    rows = service.list_messages(db, enterprise_id, user.id, conversation_id, after_id=after_id,
                                 before_id=before_id, limit=limit)
    return Resp(data={"items": [_message(row) for row in rows],
                      "assistant_available": assistant_service.capability_available(db, enterprise_id),
                      "assistant_mode": "enterprise_agent_runtime"})


@router.get("/{conversation_id}/assistant-billing-context", response_model=Resp[dict])
def assistant_billing_context(enterprise_id: int, conversation_id: int, db: Session = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """Preview only; creation and every paid call perform their own checks."""
    room = _require_current_room_member(db, enterprise_id, conversation_id, int(user.id))
    if room.kind not in {"company", "direct"}:
        raise ForbiddenError("该预览仅适用于公司群和企业私聊")
    token_version = getattr(user, "_authenticated_token_version", None)
    if type(token_version) is not int:
        raise ForbiddenError("登录状态无法核验，请重新登录")
    try:
        snapshot = billing_group.capture(
            db, enterprise_id=enterprise_id, user_id=int(user.id), conversation_id=conversation_id,
            expected_token_version=token_version,
        )
    except billing_group.DefaultBillingGroupRequired:
        status, group_id, group_name = "default_group_required", None, None
    else:
        group_id = snapshot.group_id
        group = (db.query(EnterpriseGroup).filter_by(id=group_id, enterprise_id=enterprise_id, status="active")
                 .populate_existing().with_for_update(read=True).first()) if group_id is not None else None
        if group_id is not None and group is None:
            raise ForbiddenError("默认小组无效，请联系企业管理员重新设置")
        status, group_name = ("group", group.name) if group is not None else ("unassigned", None)
    return Resp(data={"status": status, "group_id": group_id, "group_name": group_name,
                      "enabled": bool(assistant_runtime._CROSS_SCOPE_ATTRIBUTION_VERIFIED)})


@router.post("/{conversation_id}/messages", response_model=Resp[dict])
def send_message(enterprise_id: int, conversation_id: int, payload: MessageIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    token_version = getattr(user, "_authenticated_token_version", None)
    if type(token_version) is not int:
        raise ForbiddenError("登录状态无法核验，请重新登录")
    row = service.send_message(db, enterprise_id, user.id, conversation_id, payload.client_message_id, payload.content,
                               expected_token_version=token_version)
    return Resp(data={"message": _message(row),
                      "assistant_available": assistant_service.capability_available(db, enterprise_id),
                      "assistant_mode": "enterprise_agent_runtime"})


@router.post("/{conversation_id}/assistant-runs", response_model=Resp[dict])
async def request_assistant(enterprise_id: int, conversation_id: int, payload: AssistantRunIn,
                            background_tasks: BackgroundTasks = None,
                            db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """从当前用户本人发送的 @小菱 消息开始企业隔离、企业钱包计量的工具运行。"""
    _require_current_room_member(db, enterprise_id, conversation_id, int(user.id))
    result = await assistant_runtime.run_for_message(
        db, enterprise_id=enterprise_id, user_id=int(user.id), conversation_id=conversation_id,
        source_message_id=payload.source_message_id,
    )
    if background_tasks is not None and result.get("status") == "queued":
        bind = db.get_bind()
        engine = bind.engine if hasattr(bind, "engine") else bind
        background_tasks.add_task(
            assistant_runtime.execute_queued_run, engine, enterprise_id=enterprise_id,
            conversation_id=conversation_id, run_public_id=result["run_id"],
        )
    return Resp(data=_run_result(result))


@router.get("/{conversation_id}/assistant-runs", response_model=Resp[dict])
def list_assistant_runs(enterprise_id: int, conversation_id: int,
                        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    items = assistant_runtime.list_active_runs(
        db, enterprise_id=enterprise_id, user_id=int(user.id), conversation_id=conversation_id,
    )
    return Resp(data={"items": [_run_result(item) for item in items],
                      "assistant_mode": "enterprise_agent_runtime"})


@router.get("/{conversation_id}/assistant-runs/{run_id}", response_model=Resp[dict])
def assistant_run_status(enterprise_id: int, conversation_id: int, run_id: str,
                         db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data=_run_result(assistant_runtime.get_run_status(
        db, enterprise_id=enterprise_id, user_id=int(user.id),
        conversation_id=conversation_id, run_public_id=run_id,
    )))


@router.post("/{conversation_id}/assistant-runs/{run_id}/answer", response_model=Resp[dict])
async def answer_assistant_run(enterprise_id: int, conversation_id: int, run_id: str,
                               payload: AssistantAnswerIn, db: Session = Depends(get_db),
                               user: User = Depends(get_current_user)):
    _require_current_room_member(db, enterprise_id, conversation_id, int(user.id))
    result = await assistant_runtime.answer_run(
        db, enterprise_id=enterprise_id, user_id=int(user.id), conversation_id=conversation_id,
        run_public_id=run_id, call_id=payload.call_id, answer=payload.answer,
    )
    return Resp(data=_run_result(result))


@router.post("/{conversation_id}/assistant-runs/{run_id}/cancel", response_model=Resp[dict])
def cancel_assistant_run(enterprise_id: int, conversation_id: int, run_id: str,
                         payload: AssistantCancelIn, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    result = assistant_runtime.cancel_run(
        db, enterprise_id=enterprise_id, user_id=int(user.id), conversation_id=conversation_id,
        run_public_id=run_id, reason=payload.reason,
    )
    return Resp(data=_run_result(result))


@router.post("/{conversation_id}/assistant-runs/{run_id}/retry", response_model=Resp[dict])
async def retry_assistant_run(enterprise_id: int, conversation_id: int, run_id: str,
                              db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_current_room_member(db, enterprise_id, conversation_id, int(user.id))
    result = await assistant_runtime.retry_run(
        db, enterprise_id=enterprise_id, user_id=int(user.id), conversation_id=conversation_id,
        run_public_id=run_id,
    )
    return Resp(data=_run_result(result))


@router.get("/{conversation_id}/assistant-runs/{run_id}/clarification", response_model=Resp[dict])
def get_assistant_clarification(enterprise_id: int, conversation_id: int, run_id: str,
                                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_current_room_member(db, enterprise_id, conversation_id, int(user.id))
    return Resp(data=assistant_runtime.get_clarification(
        db, enterprise_id=enterprise_id, conversation_id=conversation_id,
        run_public_id=run_id, user_id=int(user.id),
    ))


@router.post("/{conversation_id}/assistant-runs/{run_id}/clarification/{request_id}/decision",
             response_model=Resp[dict])
def decide_assistant_clarification(enterprise_id: int, conversation_id: int, run_id: str,
                                  payload: AssistantClarificationDecisionIn, request_id: int = Path(gt=0),
                                  db: Session = Depends(get_db),
                                  user: User = Depends(get_current_user)):
    _require_current_room_member(db, enterprise_id, conversation_id, int(user.id))
    token_version = getattr(user, "_authenticated_token_version", None)
    token_expires_at = getattr(user, "_authenticated_token_expires_at", None)
    if type(token_version) is not int or type(token_expires_at) is not int:
        raise ForbiddenError("登录状态无法核验，请重新登录")
    return Resp(data=assistant_runtime.decide_clarification(
        db, enterprise_id=enterprise_id, conversation_id=conversation_id, run_public_id=run_id,
        request_id=request_id, source_message_id=payload.source_message_id, user_id=int(user.id),
        decision=payload.decision, expected_token_version=token_version,
        expected_token_expires_at=token_expires_at,
    ))


@router.post("/{conversation_id}/assistant-runs/{run_id}/clarification/{request_id}/resume",
             response_model=Resp[dict])
async def resume_assistant_clarification(enterprise_id: int, conversation_id: int, run_id: str,
                                         request_id: int = Path(gt=0),
                                         db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_current_room_member(db, enterprise_id, conversation_id, int(user.id))
    token_version = getattr(user, "_authenticated_token_version", None)
    token_expires_at = getattr(user, "_authenticated_token_expires_at", None)
    if type(token_version) is not int or type(token_expires_at) is not int:
        raise ForbiddenError("登录状态无法核验，请重新登录")
    result = await assistant_runtime.resume_clarification(
        db, enterprise_id=enterprise_id, conversation_id=conversation_id, run_public_id=run_id,
        request_id=request_id, user_id=int(user.id), expected_token_version=token_version,
        expected_token_expires_at=token_expires_at,
    )
    return Resp(data=_run_result(result))


@router.get("/assistant-approvals", response_model=Resp[dict])
def list_assistant_approvals(enterprise_id: int, limit: int = Query(50, ge=1, le=100),
                             db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    enterprise_org_service.require_enterprise_admin(db, enterprise_id, int(user.id))
    rows = assistant_runtime.list_pending_approvals(
        db, enterprise_id=enterprise_id, user_id=int(user.id), limit=limit,
    )
    return Resp(data={"items": rows, "assistant_mode": "enterprise_agent_runtime"})


@router.post("/assistant-approvals/{approval_id}/decision", response_model=Resp[dict])
async def decide_assistant_approval(enterprise_id: int, approval_id: int,
                                    payload: AssistantApprovalDecisionIn,
                                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    enterprise_org_service.require_enterprise_admin(db, enterprise_id, int(user.id))
    result = await assistant_runtime.decide_approval(
        db, enterprise_id=enterprise_id, approval_id=approval_id, user_id=int(user.id),
        decision=payload.decision, reason=payload.reason,
    )
    return Resp(data={**_run_result(result), "approval_id": approval_id, "decision": payload.decision})
