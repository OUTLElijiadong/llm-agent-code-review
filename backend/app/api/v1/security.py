"""SecuritySentinel 安全审计 API 路由 (v2.1 新增)

所有端点经 Orchestrator 委派给 SecuritySentinelAgent 执行。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.agents import AgentContext
from app.agents.orchestrator import get_orchestrator, get_request_orchestrator
from app.ai.exceptions import AiServiceError
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.security import (
    FullChainAuditIn,
    SecurityChecklistOut,
    SecurityDashboardSummaryOut,
    SecurityScanAllProjectsIn,
    SecurityScanFileIn,
    SecurityScanOut,
    SecurityScanProjectIn,
    SecurityScanTaskIn,
)
from app.services import security_service

router = APIRouter()


def _ctx(user: User) -> AgentContext:
    return AgentContext(user_id=user.id)


@router.get(
    "/checklist",
    response_model=Resp[SecurityChecklistOut],
    dependencies=[Depends(require_permission(PermissionCode.SECURITY_VIEW))],
)
def get_checklist(_: User = Depends(get_current_user)):
    """返回 OWASP Top10 + 内置敏感信息正则规则清单"""
    orch = get_orchestrator()
    data = orch.security_sentinel.get_checklist()
    return Resp(data=SecurityChecklistOut(**data))


@router.post(
    "/scan-file",
    response_model=Resp[SecurityScanOut],
    dependencies=[Depends(require_permission(PermissionCode.SECURITY_SCAN))],
)
def scan_file(
    payload: SecurityScanFileIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """单文件深度安全扫描"""
    orch = get_request_orchestrator(db, user=user)
    result = orch.security_sentinel.scan_file(
        file_id=payload.file_id,
        scan_depth=payload.scan_depth,
        ctx=_ctx(user),
    )
    if not result.success:
        raise AiServiceError(result.error or "安全扫描失败", code=50220)
    return Resp(data=SecurityScanOut(**result.data))


@router.post(
    "/scan-task",
    response_model=Resp[SecurityScanOut],
    dependencies=[Depends(require_permission(PermissionCode.SECURITY_SCAN))],
)
def scan_task(
    payload: SecurityScanTaskIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """任务级安全复审:为已落库的安全 issue 补 OWASP/CWE 标签"""
    orch = get_request_orchestrator(db, user=user)
    result = orch.security_sentinel.scan_task(
        task_id=payload.task_id, ctx=_ctx(user),
    )
    if not result.success:
        raise AiServiceError(result.error or "安全复审失败", code=50220)
    return Resp(data=SecurityScanOut(**result.data))


@router.post(
    "/scan-project",
    response_model=Resp[SecurityScanOut],
    dependencies=[Depends(require_permission(PermissionCode.SECURITY_SCAN))],
)
def scan_project(
    payload: SecurityScanProjectIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """项目级威胁建模:跨文件数据流追踪"""
    orch = get_request_orchestrator(db, user=user)
    result = orch.security_sentinel.scan_project(
        project_id=payload.project_id,
        top_n=payload.top_n,
        trace_dataflow=payload.trace_dataflow,
        scan_mode=payload.scan_mode,
        ctx=_ctx(user),
    )
    if not result.success:
        raise AiServiceError(result.error or "项目安全扫描失败", code=50220)
    return Resp(data=SecurityScanOut(**result.data))


@router.post(
    "/fullchain-audit",
    dependencies=[Depends(require_permission(PermissionCode.SECURITY_SCAN))],
)
def fullchain_audit(
    payload: FullChainAuditIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """全链路源码审计 (v3.3): Recon → Analysis → Verification → Report.

    四角色分工协作,黑板共享上下文,知识库压误报,可选沙箱实测验证漏洞可利用性。
    返回完整 findings + 全链路各阶段产出(侦察攻击面/分析结论/验证可复现清单/报告)。
    """
    from app.agents.fullchain_audit_agent import FullChainAuditOrchestrator

    orch = get_request_orchestrator(db, user=user)
    orchestrator = FullChainAuditOrchestrator(orch.security_sentinel)
    result = orchestrator.run(
        project_id=payload.project_id,
        actor=user,
        top_n=payload.top_n,
        trace_dataflow=payload.trace_dataflow,
        enable_sandbox=payload.enable_sandbox,
        ctx=_ctx(user),
    )
    if not result.success:
        raise AiServiceError(result.error or "全链路审计失败", code=50220)
    return Resp(data=result.data)


@router.post(
    "/scan-all-projects",
    response_model=Resp[SecurityScanOut],
    dependencies=[Depends(require_permission(PermissionCode.SECURITY_SCAN))],
)
def scan_all_projects(
    payload: SecurityScanAllProjectsIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """全量项目安全扫描:扫描当前用户可见的全部活跃项目"""
    orch = get_request_orchestrator(db, user=user)
    result = orch.security_sentinel.scan_all_projects(
        top_n_per_project=payload.top_n_per_project,
        trace_dataflow=payload.trace_dataflow,
        ctx=_ctx(user),
    )
    if not result.success:
        raise AiServiceError(result.error or "全量项目安全扫描失败", code=50220)
    return Resp(data=SecurityScanOut(**result.data))


@router.get(
    "/findings",
    response_model=Resp[SecurityScanOut],
    dependencies=[Depends(require_permission(PermissionCode.SECURITY_VIEW))],
)
def list_findings(
    task_id: int = Query(..., description="审查任务 ID"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询某个任务下已落库的安全 finding(等价于 scan-task)"""
    orch = get_request_orchestrator(db, user=user)
    result = orch.security_sentinel.scan_task(task_id=task_id, ctx=_ctx(user))
    if not result.success:
        raise AiServiceError(result.error or "查询安全发现失败", code=50220)
    return Resp(data=SecurityScanOut(**result.data))


@router.get(
    "/dashboard-summary",
    response_model=Resp[SecurityDashboardSummaryOut],
    dependencies=[Depends(require_permission(PermissionCode.SECURITY_VIEW))],
)
def dashboard_summary(
    days: int = Query(30, ge=1, le=365, description="统计窗口天数"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """工作台安全态势汇总 (v2.1.1)

    返回当前用户范围内所有项目的安全状况聚合,供 Dashboard 顶部卡片使用。
    """
    data = security_service.get_dashboard_summary(db, user=user, days=days)
    return Resp(data=SecurityDashboardSummaryOut(**data))
