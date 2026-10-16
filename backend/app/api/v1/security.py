"""SecuritySentinel 安全审计 API 路由 (v2.1 新增)

所有端点经 Orchestrator 委派给 SecuritySentinelAgent 执行。
"""
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.agents import AgentContext
from app.agents.orchestrator import get_orchestrator, get_request_orchestrator
from app.ai.exceptions import AiServiceError
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.code_file import CodeFile
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.security import (
    FullChainAuditIn,
    SecurityChecklistOut,
    SecurityDashboardSummaryOut,
    SecurityRuleCatalogOut,
    SecurityScanAllProjectsIn,
    SecurityScanFileIn,
    SecurityScanOut,
    SecurityScanProjectIn,
    SecurityScanTaskIn,
)
from app.services import security_rule_catalog_service, security_service
from app.services.project_member_service import require_project_execution
from app.services.rbac_service import check_permission
from app.services.security_report_service import infer_coverage_status, persist_security_report

router = APIRouter()


def _ctx(user: User) -> AgentContext:
    return AgentContext(user_id=user.id)


def _report_execution_status(result, data: dict) -> str:
    if result.success:
        compliance = data.get("compliance") if isinstance(data.get("compliance"), dict) else {}
        coverage_status = infer_coverage_status("success", compliance)
        return "partial" if coverage_status != "complete" else "success"
    has_partial_result = bool(
        data.get("findings")
        or data.get("threat_model")
        or data.get("file_count")
        or (isinstance(data.get("compliance"), dict) and data["compliance"].get("scanned_file_count"))
    )
    return "partial" if has_partial_result else "failed"


def _persist_scan_result(
    db: Session, user: User, *, project_id: int, report_kind: str,
    task_name: str, result, data: dict | None = None,
) -> dict:
    if result.failure_kind == "empty_scan_input":
        raise AiServiceError(result.error or "缺少可见且可扫描的非空输入", code=50220)
    if result.failure_kind in {"authorization_revoked", "unauthorized", "permission_denied"}:
        raise AiServiceError(result.error or "当前账号无权查看此扫描结果", code=40320)
    payload = dict(data if data is not None else (result.data or {}))
    status = _report_execution_status(result, payload)
    coverage = payload.get("compliance") if isinstance(payload.get("compliance"), dict) else {}
    coverage = dict(coverage)
    if status != "success" and not coverage.get("unscanned"):
        coverage["unscanned"] = [result.error or "执行未完成，未覆盖范围无法确认"]
    task_id = persist_security_report(
        db,
        owner=user,
        project_id=project_id,
        report_kind=report_kind,
        task_name=task_name,
        execution_status=status,
        result=payload,
        error_message=str(result.error or ""),
        coverage=coverage,
    )
    payload.update({
        "execution_status": status,
        "error_message": str(result.error or ""),
        "report_task_id": task_id,
    })
    return payload


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


@router.get(
    "/rule-catalog",
    response_model=Resp[SecurityRuleCatalogOut],
    dependencies=[Depends(require_permission(PermissionCode.SECURITY_VIEW))],
)
def get_rule_catalog(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """统一返回提示规则、平台静态规则、敏感规则和外部执行器状态。"""
    include_review_rules = check_permission(db, user.id, PermissionCode.RULE_VIEW)
    return Resp(data=SecurityRuleCatalogOut(**security_rule_catalog_service.build_catalog(
        db,
        user,
        include_review_rules=include_review_rules,
    )))


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
    file = db.get(CodeFile, payload.file_id)
    if file is None or file.status != "active":
        raise AiServiceError("代码文件不存在或已删除", code=40420)
    require_project_execution(db, int(file.project_id), user)
    orch = get_request_orchestrator(db, user=user)
    result = orch.security_sentinel.scan_file(
        file_id=payload.file_id,
        scan_depth=payload.scan_depth,
        ctx=_ctx(user),
    )
    data = _persist_scan_result(
        db, user, project_id=int(file.project_id), report_kind="security_scan",
        task_name=f"文件安全扫描 · {file.file_path or file.file_name}", result=result,
    )
    return Resp(data=SecurityScanOut(**data))


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
    require_project_execution(db, payload.project_id, user)
    orch = get_request_orchestrator(db, user=user)
    result = orch.security_sentinel.scan_project(
        project_id=payload.project_id,
        top_n=payload.top_n,
        trace_dataflow=payload.trace_dataflow,
        scan_mode=payload.scan_mode,
        ctx=_ctx(user),
    )
    data = _persist_scan_result(
        db, user, project_id=payload.project_id, report_kind="security_scan",
        task_name="项目安全审计", result=result,
    )
    return Resp(data=SecurityScanOut(**data))


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

    require_project_execution(db, payload.project_id, user)
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
    data = _persist_scan_result(
        db, user, project_id=payload.project_id, report_kind="fullchain_audit",
        task_name="全链路源码审计", result=result,
    )
    return Resp(data=SecurityScanOut(**data))


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
    """按显式模式扫描当前用户可见的全部活跃项目。"""
    orch = get_request_orchestrator(db, user=user)
    result = orch.security_sentinel.scan_all_projects(
        top_n_per_project=payload.top_n_per_project,
        trace_dataflow=payload.trace_dataflow,
        scan_mode=payload.scan_mode,
        ctx=_ctx(user),
    )
    if result.failure_kind == "empty_scan_input":
        raise AiServiceError(result.error or "当前账号没有可见且可扫描的项目", code=50220)
    aggregate = dict(result.data or {})
    project_results = aggregate.pop("project_results", [])
    report_task_ids: list[int] = []
    if isinstance(project_results, list):
        for item in project_results:
            if not isinstance(item, dict) or not item.get("project_id"):
                continue
            if item.get("failure_kind") in {"authorization_revoked", "unauthorized", "permission_denied"}:
                continue
            child_result = SimpleNamespace(
                success=item.get("execution_status") == "success",
                failure_kind=item.get("failure_kind", ""),
                error=item.get("error_message", ""),
                data=item.get("data") if isinstance(item.get("data"), dict) else {},
            )
            report_data = _persist_scan_result(
                db, user, project_id=int(item["project_id"]), report_kind="security_scan",
                task_name=f"安全扫描 · {item.get('project_name') or item['project_id']}",
                result=child_result,
            )
            report_task_ids.append(int(report_data["report_task_id"]))
    aggregate["report_task_ids"] = report_task_ids
    aggregate["execution_status"] = _report_execution_status(result, aggregate)
    aggregate["error_message"] = str(result.error or "")
    return Resp(data=SecurityScanOut(**aggregate))


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
