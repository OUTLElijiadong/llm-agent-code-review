"""唯一超级管理员专用的 MCP 治理 API。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_super_admin
from app.models.user import User
from app.schemas.agent_capability import (
    CapabilityAliasIn,
    McpBindingIn,
    McpServerUpsertIn,
    McpToolUpdateIn,
)
from app.schemas.common import Resp
from app.services import capability_catalog_service, mcp_governance_service

router = APIRouter()


@router.get("/servers", response_model=Resp[list[dict]])
def list_servers(
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    return Resp(data=mcp_governance_service.list_servers(db))


@router.post("/servers/recommended", response_model=Resp[list[dict]])
def seed_recommended_servers(
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    return Resp(data=mcp_governance_service.seed_recommended_servers(db, admin))


@router.post("/servers", response_model=Resp[dict])
def create_server(
    payload: McpServerUpsertIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    row = mcp_governance_service.upsert_server(db, admin, payload.model_dump())
    return Resp(data=next(item for item in mcp_governance_service.list_servers(db) if item["id"] == row.id))


@router.put("/servers/{server_id}", response_model=Resp[dict])
def update_server(
    server_id: int,
    payload: McpServerUpsertIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    row = mcp_governance_service.upsert_server(
        db,
        admin,
        payload.model_dump(),
        server_id=server_id,
    )
    return Resp(data=next(item for item in mcp_governance_service.list_servers(db) if item["id"] == row.id))


@router.delete("/servers/{server_id}", response_model=Resp[None])
def delete_server(
    server_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    mcp_governance_service.delete_server(db, admin, server_id)
    return Resp(data=None)


@router.post("/servers/{server_id}/health", response_model=Resp[dict])
def check_server_health(
    server_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    return Resp(data=mcp_governance_service.check_health(db, server_id, admin))


@router.post("/servers/{server_id}/sync", response_model=Resp[list[dict]])
def sync_server_tools(
    server_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    mcp_governance_service.sync_tools(db, server_id, admin)
    return Resp(data=mcp_governance_service.list_tools(db, server_id=server_id))


@router.get("/tools", response_model=Resp[list[dict]])
def list_tools(
    server_id: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    return Resp(data=mcp_governance_service.list_tools(db, server_id=server_id))


@router.put("/tools/{tool_id}", response_model=Resp[dict])
def update_tool(
    tool_id: int,
    payload: McpToolUpdateIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    row = mcp_governance_service.update_tool(
        db,
        admin,
        tool_id,
        payload.model_dump(exclude_unset=True),
    )
    return Resp(
        data=next(item for item in mcp_governance_service.list_tools(db) if item["id"] == row.id)
    )


@router.get("/bindings", response_model=Resp[list[dict]])
def list_bindings(
    agent_code: str = Query("", max_length=80),
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    return Resp(data=mcp_governance_service.list_bindings(db, agent_code=agent_code))


@router.put("/bindings", response_model=Resp[dict])
def upsert_binding(
    payload: McpBindingIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    row = mcp_governance_service.upsert_binding(db, admin, payload.model_dump())
    return Resp(
        data=next(
            item
            for item in mcp_governance_service.list_bindings(db)
            if item["id"] == row.id
        )
    )


@router.delete("/bindings/{binding_id}", response_model=Resp[None])
def delete_binding(
    binding_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    mcp_governance_service.delete_binding(db, admin, binding_id)
    return Resp(data=None)


@router.get("/aliases", response_model=Resp[list[dict]])
def list_aliases(
    capability_code: str = Query("", max_length=255),
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    return Resp(data=mcp_governance_service.list_aliases(db, capability_code=capability_code))


@router.post("/aliases", response_model=Resp[dict])
def create_alias(
    payload: CapabilityAliasIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    row = mcp_governance_service.upsert_alias(db, admin, payload.model_dump())
    return Resp(data=next(item for item in mcp_governance_service.list_aliases(db) if item["id"] == row.id))


@router.put("/aliases/{alias_id}", response_model=Resp[dict])
def update_alias(
    alias_id: int,
    payload: CapabilityAliasIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    row = mcp_governance_service.upsert_alias(
        db,
        admin,
        payload.model_dump(),
        alias_id=alias_id,
    )
    return Resp(data=next(item for item in mcp_governance_service.list_aliases(db) if item["id"] == row.id))


@router.delete("/aliases/{alias_id}", response_model=Resp[None])
def delete_alias(
    alias_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    mcp_governance_service.delete_alias(db, admin, alias_id)
    return Resp(data=None)


@router.get("/capabilities/search", response_model=Resp[list[dict]])
def search_capabilities(
    query: str = Query("", max_length=200),
    limit: int = Query(10, ge=1, le=20),
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    return Resp(data=capability_catalog_service.search_capabilities(db, admin, query, limit))
