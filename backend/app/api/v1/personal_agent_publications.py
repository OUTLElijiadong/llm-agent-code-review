"""Global publication of personally authored single-text Agents."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import Field, StrictBool
from sqlalchemy.orm import Session

from app.api.v1.personal_agents import _run_factory
from app.core.database import get_db
from app.core.dependencies import get_current_user, require_super_admin
from app.core.exceptions import PaymentRequiredError, ServiceUnavailableError
from app.models.personal_agent import PersonalAgentPublication
from app.models.user import User
from app.schemas.common import Resp, StrictInputModel
from app.services import billing_ledger_service as ledger
from app.services import personal_agent_execution_service as execution
from app.services import personal_agent_history_service as history
from app.services import personal_agent_publication_service as publication

router = APIRouter(tags=["个人 Agent 全局发布"])


class SubmitIn(StrictInputModel):
    expected_version: int = Field(ge=1, strict=True)
    request_key: str = Field(min_length=1, max_length=80)


class DecisionIn(StrictInputModel):
    approve: StrictBool
    reason: str = Field(default="", max_length=500)


class ReasonIn(StrictInputModel):
    reason: str = Field(default="", max_length=500)


class RunIn(StrictInputModel):
    request_key: str = Field(min_length=1, max_length=80)
    input_text: str = Field(min_length=1, max_length=12000)


def _own(row):
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "version_number": row.version_number,
        "title": row.title,
        "status": row.status,
        "decision_reason": row.decision_reason,
        "revocation_reason": row.revocation_reason,
        "create_time": row.create_time,
    }


@router.get("/me/agent-runs", response_model=Resp[list[dict]])
def run_history(
    source: Literal["private", "public", "enterprise_public"],
    before_id: int | None = Query(None, ge=1, le=9223372036854775807),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return Resp(data=history.list_runs(db, owner_id=int(user.id), source=source, before_id=before_id, limit=limit))


@router.post("/me/agents/{agent_id}/publication-requests", response_model=Resp[dict])
def submit(agent_id: int, payload: SubmitIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = publication.submit(db, owner_id=int(user.id), agent_id=agent_id,
                             token_version=int(user.token_version or 0), **payload.model_dump())
    return Resp(data=_own(row))


@router.get("/me/personal-agent-publications", response_model=Resp[list[dict]])
def mine(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (
        db.query(PersonalAgentPublication)
        .filter_by(owner_id=user.id)
        .order_by(PersonalAgentPublication.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    for row in rows:
        publication._verify(row)
    return Resp(data=[_own(row) for row in rows])


@router.post("/me/personal-agent-publications/{publication_id}/withdraw", response_model=Resp[dict])
def withdraw(publication_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data=_own(publication.withdraw(db, owner_id=int(user.id), publication_id=publication_id,
                                              token_version=int(user.token_version or 0))))


@router.get("/admin/personal-agent-publications", response_model=Resp[list[dict]])
def pending(
    status: Literal["pending", "approved", "rejected", "withdrawn", "revoked"] = "pending",
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(require_super_admin),
):
    publication._admin(db, int(user.id), int(user.token_version or 0))
    rows = (
        db.query(PersonalAgentPublication)
        .filter_by(status=status)
        .order_by(PersonalAgentPublication.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    for row in rows:
        publication._verify(row)
    return Resp(
        data=[
            {**_own(row), "prompt": row.prompt, "owner_id": row.owner_id, "content_hash": row.content_hash}
            for row in rows
        ]
    )


@router.post("/admin/personal-agent-publications/{publication_id}/decision", response_model=Resp[dict])
def decide(
    publication_id: int, payload: DecisionIn, db: Session = Depends(get_db), user: User = Depends(require_super_admin)
):
    return Resp(
        data=_own(
            publication.decide(
                db,
                actor_id=int(user.id),
                token_version=int(user.token_version or 0),
                publication_id=publication_id,
                **payload.model_dump(),
            )
        )
    )


@router.post("/admin/personal-agent-publications/{publication_id}/revoke", response_model=Resp[dict])
def revoke(
    publication_id: int, payload: ReasonIn, db: Session = Depends(get_db), user: User = Depends(require_super_admin)
):
    return Resp(
        data=_own(
            publication.revoke(
                db,
                actor_id=int(user.id),
                token_version=int(user.token_version or 0),
                publication_id=publication_id,
                reason=payload.reason,
            )
        )
    )


@router.get("/personal-agent-catalog", response_model=Resp[list[dict]])
def catalog(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    ready = execution.is_available(_run_factory(db))
    return Resp(
        data=[{**row, "execution_available": ready} for row in publication.catalog(db, offset=offset, limit=limit)]
    )


@router.post("/personal-agent-catalog/{publication_id}/runs", response_model=Resp[dict])
def run(publication_id: int, payload: RunIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        return Resp(
            data=execution.execute_publication(
                _run_factory(db),
                owner_id=int(user.id),
                publication_id=publication_id,
                token_version=int(user.token_version or 0),
                **payload.model_dump(),
            )
        )
    except ledger.InsufficientCredits as exc:
        raise PaymentRequiredError(str(exc)) from exc
    except ledger.BillingError as exc:
        raise ServiceUnavailableError(str(exc)) from exc


@router.get("/personal-agent-catalog/{publication_id}/runs/by-request/{request_key}", response_model=Resp[dict])
def by_request(
    publication_id: int, request_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    return Resp(
        data=execution.get_publication_run(
            _run_factory(db),
            owner_id=int(user.id),
            publication_id=publication_id,
            request_key=request_key,
            token_version=int(user.token_version or 0),
        )
    )


@router.get("/personal-agent-catalog/{publication_id}/runs/{run_id}", response_model=Resp[dict])
def get_run(publication_id: int, run_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(
        data=execution.get_publication_run(
            _run_factory(db),
            owner_id=int(user.id),
            publication_id=publication_id,
            run_id=run_id,
            token_version=int(user.token_version or 0),
        )
    )
