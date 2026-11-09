"""Private personal Agent management and gated single-text execution; publication is unavailable."""

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import PaymentRequiredError, ServiceUnavailableError
from app.models.user import User
from app.schemas.common import Resp, StrictInputModel
from app.services import billing_ledger_service as ledger
from app.services import personal_agent_execution_service as execution
from app.services import personal_agent_service as service

router = APIRouter(prefix="/me/agents", tags=["个人 Agent"])


class CreateIn(StrictInputModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=30000)
    request_key: str = Field(min_length=1, max_length=80)


class ReviseIn(CreateIn):
    expected_version: int = Field(ge=1, strict=True)


class DisableIn(StrictInputModel):
    expected_version: int = Field(ge=1, strict=True)


class RunIn(StrictInputModel):
    request_key: str = Field(min_length=1, max_length=80)
    version_number: int = Field(ge=1, strict=True)
    input_text: str = Field(min_length=1, max_length=12000)


def _version(row):
    return {
        "version_number": row.version_number,
        "title": row.title,
        "prompt": row.prompt,
        "create_time": row.create_time,
    }


def _out(db, row, *, ready=None):
    version = service.current_version(db, row)
    owner = db.get(User, row.owner_id)
    if ready is None:
        ready = execution.is_available(_run_factory(db))
    return {
        "id": row.id,
        "title": version.title,
        "prompt": version.prompt,
        "current_version": row.current_version,
        "status": row.status,
        "execution_available": bool(ready and row.status == "active"),
        "publication_available": bool(
            row.status == "active" and owner is not None and owner.status == 1
            and not (owner.username == "admin" and owner.role == "super_admin")
        ),
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


@router.get("", response_model=Resp[list[dict]])
def list_agents(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = service.list_agents(db, user.id, offset=offset, limit=limit)
    ready = execution.is_available(_run_factory(db)) if rows else False
    return Resp(data=[_out(db, row, ready=ready) for row in rows])


@router.post("", response_model=Resp[dict])
def create(payload: CreateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data=_out(db, service.create(db, user.id, **payload.model_dump())))


@router.get("/{agent_id}", response_model=Resp[dict])
def get(agent_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data=_out(db, service.get(db, user.id, agent_id)))


@router.put("/{agent_id}", response_model=Resp[dict])
def revise(agent_id: int, payload: ReviseIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data=_out(db, service.revise(db, user.id, agent_id, **payload.model_dump())))


@router.get("/{agent_id}/versions", response_model=Resp[list[dict]])
def versions(
    agent_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return Resp(data=[_version(row) for row in service.versions(db, user.id, agent_id, offset=offset, limit=limit)])


@router.post("/{agent_id}/disable", response_model=Resp[dict])
def disable(agent_id: int, payload: DisableIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data=_out(db, service.disable(db, user.id, agent_id, payload.expected_version)))


def _run_factory(db: Session):
    return sessionmaker(bind=db.get_bind(), autocommit=False, autoflush=False, expire_on_commit=False)


@router.post("/{agent_id}/runs", response_model=Resp[dict])
def start_run(agent_id: int, payload: RunIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        return Resp(data=execution.execute(_run_factory(db), owner_id=int(user.id), agent_id=agent_id,
                                           token_version=int(user.token_version or 0), **payload.model_dump()))
    except ledger.InsufficientCredits as exc:
        raise PaymentRequiredError(str(exc)) from exc
    except ledger.BillingError as exc:
        raise ServiceUnavailableError(str(exc)) from exc


@router.get("/{agent_id}/runs/by-request/{request_key}", response_model=Resp[dict])
def get_run_by_request(agent_id: int, request_key: str, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    return Resp(data=execution.get_by_request(_run_factory(db), owner_id=int(user.id), agent_id=agent_id,
                                             request_key=request_key, token_version=int(user.token_version or 0)))


@router.get("/{agent_id}/runs/{run_id}", response_model=Resp[dict])
def get_run(agent_id: int, run_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    result = execution.get_run(_run_factory(db), owner_id=int(user.id), run_id=run_id,
                               token_version=int(user.token_version or 0))
    if result["agent_id"] != agent_id:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("个人 Agent 运行不存在")
    return Resp(data=result)
