"""所属账号的浏览器动作认领/观察接口；不执行任意服务器或浏览器脚本。"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.agent_browser import BrowserAckIn, BrowserContextIn
from app.schemas.common import Resp
from app.services import agent_browser_service

router = APIRouter(dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))])


@router.get("/pending", response_model=Resp[dict])
def pending(
    run_id: str = Query(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$"),
    session_id: str = Query(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    surface: Literal["user", "admin"] = "user",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    return Resp(
        data=agent_browser_service.pending_actions(
            db,
            user,
            run_id=run_id,
            session_key=session_id,
            surface=surface,
        )
    )


@router.post("/actions/{action_id}/claim", response_model=Resp[dict])
def claim(
    action_id: int, payload: BrowserContextIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Resp[dict]:
    return Resp(
        data=agent_browser_service.claim_action(
            db,
            user,
            action_id,
            session_key=payload.session_id,
            surface=payload.surface,
            nonce=payload.client_nonce,
        )
    )


@router.post("/actions/{action_id}/ack", response_model=Resp[dict])
def acknowledge(
    action_id: int, payload: BrowserAckIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Resp[dict]:
    return Resp(data=agent_browser_service.acknowledge_action(db, user, action_id, payload))
