"""小菱安全运营 API（自动化攻防编排）。

给唯一超级管理员一套"看 → 判 → 处置 → 验证"的接口，也是管理小菱调用安全运营能力的
落地入口。所有写动作（封禁/解封/启动渗透）都要求超级管理员身份并写 `secops.*` 审计。

权限口径：
- 读（研判/攻击面/候选/基线/巡检状态）：管理员 + `security:view`
- 写（封禁/解封/启动授权渗透）：仅唯一超级管理员 `admin`
"""
# ┏━ 【文件导读】app/api/v1/secops.py
# ┃ 层级：接口层（路由：接收 HTTP 请求，校验参数后交给业务层）
# ┃ 作用：小菱安全运营接口：多源攻击研判、攻击面盘点、自动封禁候选、自身基线核查、
# ┃       巡检状态，以及封禁/解封/启动已授权渗透测试三个状态变更动作。
# ┃ 流程：读动作校验管理员与 security:view 后直接调 secops_service；写动作只放行唯一超管，
# ┃       由服务层再校验一遍身份、二次确认与白名单，并写审计事件。
# ┃ 引用了：core/database、core/dependencies、core/permission_codes、core/rbac_dependency、
# ┃         schemas/common、schemas/secops、services/secops_service
# ┃ 被引用：api/__init__
# ┗━ 以上导读只是注释，不影响运行；撤销方法见 docs/后端代码导读20261024/README.md

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_admin, require_super_admin
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.secops import (
    SecOpsBaselineOut,
    SecOpsBlockIn,
    SecOpsBlockOut,
    SecOpsCandidatesOut,
    SecOpsDigestOut,
    SecOpsPatrolOut,
    SecOpsPentestIn,
    SecOpsPentestOut,
    SecOpsReleaseIn,
    SecOpsSurfaceOut,
    SecOpsToolCatalogOut,
)
from app.services import copilot_security_service, secops_service

router = APIRouter()
_READ = [
    Depends(require_admin),
    Depends(require_permission(PermissionCode.SECURITY_VIEW)),
]


def _client_ip(request: Request) -> str:
    """取真实来源 IP（沿用安全中心的可信代理链解析）。"""
    return copilot_security_service.resolve_client_ip(request)


# ▎接口：GET /api/admin/secops/digest
# ▎权限：需管理员 + security:view
@router.get("/digest", response_model=Resp[SecOpsDigestOut], dependencies=_READ)
def get_digest(
    hours: int = Query(24, ge=1, le=336),
    limit: int = Query(200, ge=20, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """四类传感器聚合 → 攻击源画像 + 处置建议。"""
    return Resp(data=SecOpsDigestOut(**secops_service.threat_digest(db, user, hours=hours, limit=limit)))


# ▎接口：GET /api/admin/secops/surface
# ▎权限：需管理员 + security:view
@router.get("/surface", response_model=Resp[SecOpsSurfaceOut], dependencies=_READ)
def get_surface(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """自身暴露面盘点（监听端口、公开路径、证书）。"""
    return Resp(data=SecOpsSurfaceOut(**secops_service.surface_audit(db, user)))


# ▎接口：GET /api/admin/secops/candidates
# ▎权限：需管理员 + security:view
@router.get("/candidates", response_model=Resp[SecOpsCandidatesOut], dependencies=_READ)
def get_candidates(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """自动封禁候选名单。"""
    return Resp(data=SecOpsCandidatesOut(**secops_service.block_candidates(db, user)))


# ▎接口：GET /api/admin/secops/baseline
# ▎权限：需管理员 + security:view
@router.get("/baseline", response_model=Resp[SecOpsBaselineOut], dependencies=_READ)
def get_baseline(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """平台自身安全基线核查。"""
    return Resp(data=SecOpsBaselineOut(**secops_service.baseline_check(db, user)))


# ▎接口：GET /api/admin/secops/patrol
# ▎权限：需管理员 + security:view
@router.get("/patrol", response_model=Resp[SecOpsPatrolOut], dependencies=_READ)
def get_patrol(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """自动巡检开关与最近一次结果。"""
    return Resp(data=SecOpsPatrolOut(**secops_service.patrol_status(db)))


# ▎接口：GET /api/admin/secops/tools
# ▎权限：需管理员 + security:view
@router.get("/tools", response_model=Resp[SecOpsToolCatalogOut], dependencies=_READ)
def get_tools(user: User = Depends(require_admin)):
    """安全运营工具目录（小菱可调用的能力清单与风险等级）。"""
    return Resp(data=SecOpsToolCatalogOut(tools=secops_service.tool_catalog()))


# ▎接口：POST /api/admin/secops/block
# ▎权限：仅超级管理员
@router.post("/block", response_model=Resp[SecOpsBlockOut], dependencies=[Depends(require_super_admin)])
def post_block(
    payload: SecOpsBlockIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_super_admin),
):
    """封禁一个攻击源（需 confirm=true，服务层再做一次身份与白名单校验）。"""
    data = secops_service.apply_block(
        db,
        user,
        ip=payload.ip,
        reason=payload.reason,
        duration_seconds=payload.duration_seconds,
        confirm=payload.confirm,
        client_ip=_client_ip(request),
    )
    return Resp(data=SecOpsBlockOut(**data))


# ▎接口：POST /api/admin/secops/release
# ▎权限：仅超级管理员
@router.post("/release", response_model=Resp[SecOpsBlockOut], dependencies=[Depends(require_super_admin)])
def post_release(
    payload: SecOpsReleaseIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_super_admin),
):
    """解封一个来源。"""
    data = secops_service.release_block(
        db, user, ip=payload.ip, reason=payload.reason, client_ip=_client_ip(request),
    )
    return Resp(data=SecOpsBlockOut(**{**data, "duration_seconds": 0}))


# ▎接口：POST /api/admin/secops/pentest/{public_id}/start
# ▎权限：仅超级管理员
@router.post("/pentest/{public_id}/start", response_model=Resp[SecOpsPentestOut],
             dependencies=[Depends(require_super_admin)])
def post_pentest(
    public_id: str,
    payload: SecOpsPentestIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_super_admin),
):
    """启动已授权且在时间窗内的渗透测试委托（不实现攻击逻辑，只转发流水线）。"""
    data = secops_service.start_authorized_pentest(
        db, user, public_id=public_id, client_ip=_client_ip(request),
    )
    return Resp(data=SecOpsPentestOut(**data))
