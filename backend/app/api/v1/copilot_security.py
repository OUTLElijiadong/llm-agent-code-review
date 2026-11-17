"""小菱安全中心 API。

给管理员小菱（``admin_copilot``）做安全配置的唯一入口：策略读写、预设切换、
紧急停机、注入演练、态势评分与安全事件查询。

权限口径：
- 读（态势/策略/事件）与演练：需要管理员 + ``security:view``。
- 写（策略/预设/停机开关）：只有唯一超级管理员 ``admin``，防止二级管理员自行放宽闸门。
"""
# ┏━ 【文件导读】app/api/v1/copilot_security.py
# ┃ 层级：接口层（路由：接收 HTTP 请求，校验参数后交给业务层）
# ┃ 作用：小菱安全中心接口：读取/保存管理小菱安全策略、一键切预设、紧急停机、注入演练、态势评分、事件查询。
# ┃ 流程：先过管理员与权限校验，写操作再过超级管理员校验；随后调用 copilot_security_service 读写策略并返回
# ┃       最新态势，保存与切换都会写一条审计事件。
# ┃ 接口：共 7 个接口，统一前缀 /api/admin/copilot-security
# ┃ 引用了：core/database、core/dependencies、core/permission_codes、core/rbac_dependency、
# ┃         schemas/common、schemas/copilot_security、services/copilot_security_rules、
# ┃         services/copilot_security_service
# ┃ 被引用：api/__init__
# ┗━ 以上导读只是注释，不影响运行；撤销方法见 docs/后端代码导读20261024/README.md

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_admin, require_super_admin
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.copilot_security import (
    CopilotSecurityCenterOut,
    CopilotSecurityEventListOut,
    CopilotSecurityKillSwitchIn,
    CopilotSecurityPolicyIn,
    CopilotSecurityPolicyOut,
    CopilotSecurityPolicySavedOut,
    CopilotSecurityPostureOut,
    CopilotSecurityPresetIn,
    CopilotSecurityScanIn,
    CopilotSecurityScanOut,
    EventKindOptionOut,
    PresetOptionOut,
)
from app.services import copilot_security_rules as rules
from app.services import copilot_security_service as service

router = APIRouter()


def _presets() -> list[PresetOptionOut]:
    return [PresetOptionOut(preset=name, label=label) for name, label in rules.PRESET_LABELS]


def _event_kinds() -> list[EventKindOptionOut]:
    return [EventKindOptionOut(kind=item["kind"], label=item["label"]) for item in service.EVENT_KINDS]


# ▎接口：GET /api/admin/copilot-security/center
# ▎权限：需管理员 + security:view
@router.get(
    "/center",
    response_model=Resp[CopilotSecurityCenterOut],
    dependencies=[
        Depends(require_admin),
        Depends(require_permission(PermissionCode.SECURITY_VIEW)),
    ],
)
def get_center(db: Session = Depends(get_db)):
    """一次拿全：当前策略 + 态势评分 + 预设目录 + 事件类型目录。"""
    policy = service.load_policy(db)
    return Resp(data=CopilotSecurityCenterOut(
        policy=CopilotSecurityPolicyOut(**policy),
        posture=CopilotSecurityPostureOut(**service.posture(db, policy=policy)),
        presets=_presets(),
        event_kinds=_event_kinds(),
        governed_agent=service.ADMIN_COPILOT_AGENT,
    ))


# ▎接口：GET /api/admin/copilot-security/policy
# ▎权限：需管理员 + security:view
@router.get(
    "/policy",
    response_model=Resp[CopilotSecurityPolicyOut],
    dependencies=[
        Depends(require_admin),
        Depends(require_permission(PermissionCode.SECURITY_VIEW)),
    ],
)
def get_policy(db: Session = Depends(get_db)):
    """读取当前生效的小菱安全策略。"""
    return Resp(data=CopilotSecurityPolicyOut(**service.load_policy(db)))


# ▎接口：PUT /api/admin/copilot-security/policy
# ▎权限：仅超级管理员
@router.put(
    "/policy",
    response_model=Resp[CopilotSecurityPolicySavedOut],
    dependencies=[Depends(require_super_admin)],
)
def update_policy(
    payload: CopilotSecurityPolicyIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    """局部更新策略：只提交要改的字段，未提交的保持原值。"""
    patch = payload.to_patch()
    before = service.load_policy(db)
    client_ip = service.resolve_client_ip(request, policy=before)
    policy = service.save_policy(db, patch=patch, actor=admin, client_ip=client_ip)
    changed = sorted(key for key in policy if before.get(key) != policy.get(key))
    return Resp(data=CopilotSecurityPolicySavedOut(
        policy=CopilotSecurityPolicyOut(**policy),
        changed=changed,
        posture=CopilotSecurityPostureOut(**service.posture(db, policy=policy)),
    ))


# ▎接口：POST /api/admin/copilot-security/policy/preset
# ▎权限：仅超级管理员
@router.post(
    "/policy/preset",
    response_model=Resp[CopilotSecurityPolicySavedOut],
    dependencies=[Depends(require_super_admin)],
)
def apply_preset(
    payload: CopilotSecurityPresetIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    """套用预设。切到 ``max`` 必须显式 ``confirm=true``，避免误触。"""
    if payload.preset == "max" and not payload.confirm:
        from app.core.exceptions import ValidationError

        raise ValidationError("切换到最顶级预设需要显式确认（confirm=true）")
    before = service.load_policy(db)
    client_ip = service.resolve_client_ip(request, policy=before)
    policy = service.apply_preset(
        db, preset=payload.preset, actor=admin, client_ip=client_ip,
    )
    changed = sorted(key for key in policy if before.get(key) != policy.get(key))
    return Resp(data=CopilotSecurityPolicySavedOut(
        policy=CopilotSecurityPolicyOut(**policy),
        changed=changed,
        posture=CopilotSecurityPostureOut(**service.posture(db, policy=policy)),
    ))


# ▎接口：POST /api/admin/copilot-security/kill-switch
# ▎权限：仅超级管理员
@router.post(
    "/kill-switch",
    response_model=Resp[CopilotSecurityPolicySavedOut],
    dependencies=[Depends(require_super_admin)],
)
def toggle_kill_switch(
    payload: CopilotSecurityKillSwitchIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    """紧急停机：打开后管理小菱的一切请求与工具调用立即被拒。"""
    before = service.load_policy(db)
    client_ip = service.resolve_client_ip(request, policy=before)
    policy = service.set_kill_switch(
        db, enabled=payload.enabled, reason=payload.reason, actor=admin, client_ip=client_ip,
    )
    return Resp(data=CopilotSecurityPolicySavedOut(
        policy=CopilotSecurityPolicyOut(**policy),
        changed=["kill_switch"],
        posture=CopilotSecurityPostureOut(**service.posture(db, policy=policy)),
    ))


# ▎接口：GET /api/admin/copilot-security/posture
# ▎权限：需管理员 + security:view
@router.get(
    "/posture",
    response_model=Resp[CopilotSecurityPostureOut],
    dependencies=[
        Depends(require_admin),
        Depends(require_permission(PermissionCode.SECURITY_VIEW)),
    ],
)
def get_posture(db: Session = Depends(get_db)):
    """小菱安全态势评分与整改建议。"""
    return Resp(data=CopilotSecurityPostureOut(**service.posture(db)))


# ▎接口：POST /api/admin/copilot-security/scan
# ▎权限：需管理员 + security:view
@router.post(
    "/scan",
    response_model=Resp[CopilotSecurityScanOut],
    dependencies=[
        Depends(require_admin),
        Depends(require_permission(PermissionCode.SECURITY_VIEW)),
    ],
)
def scan_text(payload: CopilotSecurityScanIn):
    """注入检测演练：返回命中规则、分值与脱敏结果；不落库、不拦截。

    调策略时先用它验证一段输入会不会被拦，避免配错阈值把正常提问挡掉。
    """
    return Resp(data=CopilotSecurityScanOut(**service.inspect_text(payload.text)))


# ▎接口：GET /api/admin/copilot-security/events
# ▎权限：需管理员 + security:view
@router.get(
    "/events",
    response_model=Resp[CopilotSecurityEventListOut],
    dependencies=[
        Depends(require_admin),
        Depends(require_permission(PermissionCode.SECURITY_VIEW)),
    ],
)
def list_events(
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(20, ge=1, le=200),
    kind: Optional[str] = Query(None, max_length=40, description="事件类型短名，如 injection_blocked"),
    status: Optional[str] = Query(None, max_length=20),
    days: int = Query(7, ge=0, le=365),
    db: Session = Depends(get_db),
):
    """分页查询小菱安全事件（来自 audit_log 的 copilot_security.* 记录）。"""
    data = service.list_events(
        db,
        page=page,
        page_size=page_size,
        kind=(kind or "").strip(),
        status=(status or "").strip(),
        days=days,
    )
    return Resp(data=CopilotSecurityEventListOut(**data))
