"""报告 API 路由 (T12 扩展)

提供报告生成、预览、模板管理与多格式导出能力。

路由分组:
1. T12 新增路由(字面路径优先注册,避免与 /{task_id} 参数路由冲突):
   - POST /generate              生成报告(JSON/HTML/PDF/Word)
   - GET  /tasks/{task_id}       预览报告(HTML,便于 iframe 嵌入)
   - GET  /tasks/{task_id}/export 导出报告(直接下载文件)
   - GET  /templates             列出全部模板
   - POST /templates             创建模板
   - PUT  /templates/{id}        更新模板
   - DELETE /templates/{id}      删除模板
2. 既有路由(参数路由 /{task_id},仅匹配 int):
   - GET    ""                   报告列表
   - GET    /{task_id}           报告详情
   - DELETE /{task_id}           删除报告
   - GET    /{task_id}/export/word  导出 Word(旧)
   - GET    /{task_id}/export/pdf   导出 PDF(旧)

权限点:
- report:view             生成与预览报告
- report:template_manage  模板管理
- report:export:pdf       导出 PDF
- report:export:word      导出 Word
- report:export:json      导出 JSON
- report:export:html      导出 HTML
"""
from io import BytesIO
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import NotFoundError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.report import ReportDetailOut, ReportListItem
from app.schemas.report_template import ReportTemplateIn, ReportTemplateOut, ReportTemplateUpdate
from app.services import report_service, report_template_service
from app.services.rbac_service import check_permission
from app.services.report_exporter import (
    export_to_html,
    export_to_json,
)
from app.services.report_pdf_exporter import export_to_pdf
from app.services.report_word_exporter import export_to_word

router = APIRouter()


# ============ T12 请求体 Schema ============

class ReportGenerateIn(BaseModel):
    """报告生成请求体。

    Attributes:
        task_id: 审查任务 ID。
        format: 导出格式(json/html/pdf/word)。
        template_type: 模板类型(simple/detailed/compliance),仅 html 格式使用。
    """
    task_id: int = Field(..., description="审查任务 ID")
    format: str = Field(default="html", pattern="^(json|html|pdf|word)$", description="导出格式")
    template_type: str = Field(default="detailed", pattern="^(simple|detailed|compliance)$",
                               description="模板类型(仅 html 格式使用)")


# ============ T12 内部辅助函数 ============

def _get_task_with_issues(db: Session, task_id: int, user: User) -> tuple:
    """获取审查任务及其全部问题(含权限校验)。

    校验逻辑:
        1. 任务必须存在且状态为 success(审查完成)
        2. 当前用户必须为管理员或任务发起者

    Args:
        db: 数据库会话。
        task_id: 审查任务 ID。
        user: 当前登录用户。

    Returns:
        tuple: (ReviewTask, List[ReviewIssue], summary, score) 四元组,
            summary 为 AI 总体评价字符串,score 为综合评分。

    Raises:
        NotFoundError: 任务不存在或未完成,或用户无访问权限(code=40400)。
    """
    task = db.get(ReviewTask, task_id)
    if not task or task.status != "success":
        raise NotFoundError(f"审查任务 #{task_id} 不存在或未完成", code=40400)
    # 权限校验:管理员或任务发起者可访问
    if task.user_id != user.id and user.role not in {"admin", "super_admin"}:
        raise NotFoundError("报告不存在", code=40400)

    issues: List[ReviewIssue] = (
        db.query(ReviewIssue)
        .filter(ReviewIssue.task_id == task_id)
        .order_by(ReviewIssue.id.asc())
        .all()
    )
    return task, issues, task.summary or "", task.score or 0


def _get_template_content(db: Session, template_type: str) -> str:
    """获取模板内容(优先数据库内置模板,降级到文件系统)。

    Args:
        db: 数据库会话。
        template_type: 模板类型(simple/detailed/compliance)。

    Returns:
        str: Jinja2 模板字符串。
    """
    return report_template_service.get_builtin_template_content(db, template_type)


def _build_download_response(
    content: bytes,
    media_type: str,
    filename: str,
) -> StreamingResponse:
    """构建文件下载响应(StreamingResponse + Content-Disposition)。

    Args:
        content: 文件二进制内容。
        media_type: MIME 类型(如 application/pdf)。
        filename: 下载文件名。

    Returns:
        StreamingResponse: 带 Content-Disposition: attachment 的流式响应。
    """
    return StreamingResponse(
        BytesIO(content),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============ T12 报告生成与导出路由(字面路径优先) ============

@router.post("/generate")
def generate_report(
    payload: ReportGenerateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.REPORT_VIEW)),
):
    """生成报告(支持 JSON / HTML / PDF / Word 四种格式)。

    根据 payload.format 调用对应导出器:
    - json: 返回 JSON 响应
    - html: 返回 HTML 响应(使用指定模板渲染)
    - pdf:  返回 PDF 字节流下载
    - word: 返回 Word 字节流下载

    Args:
        payload: 报告生成请求体(task_id / format / template_type)。
        db: 数据库会话(由 Depends 注入)。
        user: 当前用户(由 require_permission 注入,已校验 report:view 权限)。

    Returns:
        Response: 根据格式返回 JSONResponse / HTMLResponse / StreamingResponse。

    Raises:
        NotFoundError: 任务不存在或未完成(404)。
    """
    task, issues, summary, score = _get_task_with_issues(db, payload.task_id, user)
    fmt = payload.format

    if fmt == "json":
        json_str = export_to_json(task, issues, summary, score)
        return Response(content=json_str, media_type="application/json")

    if fmt == "html":
        template_content = _get_template_content(db, payload.template_type)
        html_str = export_to_html(task, issues, summary, score, template_content)
        return HTMLResponse(content=html_str)

    if fmt == "pdf":
        pdf_bytes = export_to_pdf(task, issues, summary, score, payload.template_type)
        return _build_download_response(
            pdf_bytes,
            "application/pdf",
            f"review_report_{payload.task_id}.pdf",
        )

    if fmt == "word":
        word_bytes = export_to_word(task, issues, summary, score, payload.template_type)
        return _build_download_response(
            word_bytes,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            f"review_report_{payload.task_id}.docx",
        )

    # 理论上不会到达(Pydantic pattern 已校验)
    return Resp(code=40001, message=f"不支持的格式: {fmt}")


@router.get("/tasks/{task_id}")
def preview_report(
    task_id: int,
    template_type: str = Query(default="detailed", pattern="^(simple|detailed|compliance)$"),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.REPORT_VIEW)),
):
    """预览报告(返回 HTML,便于前端 iframe 嵌入)。

    Args:
        task_id: 审查任务 ID(路径参数)。
        template_type: 模板类型(simple/detailed/compliance),默认 detailed。
        db: 数据库会话(由 Depends 注入)。
        user: 当前用户(由 require_permission 注入,已校验 report:view 权限)。

    Returns:
        HTMLResponse: 渲染后的 HTML 报告字符串。

    Raises:
        NotFoundError: 任务不存在或未完成(404)。
    """
    task, issues, summary, score = _get_task_with_issues(db, task_id, user)
    template_content = _get_template_content(db, template_type)
    html_str = export_to_html(task, issues, summary, score, template_content)
    return HTMLResponse(content=html_str)


def _require_report_export_permission(
    format: str = Query(default="pdf", pattern="^(json|html|pdf|word)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> User:
    """按实际导出格式校验权限，避免使用不存在的 report:export。"""
    permission_by_format = {
        "json": PermissionCode.REPORT_EXPORT_JSON,
        "html": PermissionCode.REPORT_EXPORT_HTML,
        "pdf": PermissionCode.REPORT_EXPORT_PDF,
        "word": PermissionCode.REPORT_EXPORT_WORD,
    }
    permission = permission_by_format[format]
    if not check_permission(db, user.id, permission):
        from app.core.exceptions import PermissionError
        raise PermissionError(
            f"无操作权限: 需要 {permission}",
            detail={"required_permission": permission, "format": format},
        )
    return user


@router.get("/tasks/{task_id}/export")
def export_report(
    task_id: int,
    format: str = Query(default="pdf", pattern="^(json|html|pdf|word)$"),
    template_type: str = Query(default="detailed", pattern="^(simple|detailed|compliance)$"),
    db: Session = Depends(get_db),
    user: User = Depends(_require_report_export_permission),
):
    """导出报告(直接下载文件,返回 StreamingResponse with Content-Disposition: attachment)。

    Args:
        task_id: 审查任务 ID(路径参数)。
        format: 导出格式(json/html/pdf/word),默认 pdf。
        template_type: 模板类型(simple/detailed/compliance),默认 detailed。
        db: 数据库会话(由 Depends 注入)。
        user: 当前用户(按 format 校验对应 report:export:* 权限)。

    Returns:
        StreamingResponse: 带下载头的流式响应;JSON/HTML 格式同样以文件下载方式返回。

    Raises:
        NotFoundError: 任务不存在或未完成(404)。
    """
    task, issues, summary, score = _get_task_with_issues(db, task_id, user)

    if format == "json":
        json_str = export_to_json(task, issues, summary, score)
        return _build_download_response(
            json_str.encode("utf-8"),
            "application/json",
            f"review_report_{task_id}.json",
        )

    if format == "html":
        template_content = _get_template_content(db, template_type)
        html_str = export_to_html(task, issues, summary, score, template_content)
        return _build_download_response(
            html_str.encode("utf-8"),
            "text/html",
            f"review_report_{task_id}.html",
        )

    if format == "pdf":
        pdf_bytes = export_to_pdf(task, issues, summary, score, template_type)
        return _build_download_response(
            pdf_bytes,
            "application/pdf",
            f"review_report_{task_id}.pdf",
        )

    if format == "word":
        word_bytes = export_to_word(task, issues, summary, score, template_type)
        return _build_download_response(
            word_bytes,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            f"review_report_{task_id}.docx",
        )

    # 理论上不会到达
    return Resp(code=40001, message=f"不支持的格式: {format}")


# ============ T12 模板管理路由 ============

@router.get("/templates", response_model=Resp[List[ReportTemplateOut]])
def list_templates(
    template_type: Optional[str] = Query(default=None, pattern="^(simple|detailed|compliance|custom)$"),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("report:template_manage")),
):
    """列出全部报告模板(可按类型筛选)。

    Args:
        template_type: 模板类型筛选(simple/detailed/compliance/custom),为空返回全部。
        db: 数据库会话(由 Depends 注入)。
        user: 当前用户(由 require_permission 注入,已校验 report:template_manage 权限)。

    Returns:
        Resp[List[ReportTemplateOut]]: 模板列表(含内置与自定义)。
    """
    templates = report_template_service.list_templates(db, template_type)
    return Resp(data=[ReportTemplateOut.model_validate(t) for t in templates])


@router.post("/templates", response_model=Resp[ReportTemplateOut])
def create_template(
    payload: ReportTemplateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("report:template_manage")),
):
    """创建自定义报告模板。

    Args:
        payload: 模板创建请求体(name/type/content/description)。
        db: 数据库会话(由 Depends 注入)。
        user: 当前用户(由 require_permission 注入,已校验 report:template_manage 权限)。

    Returns:
        Resp[ReportTemplateOut]: 已创建的模板对象。
    """
    template = report_template_service.create_template(db, payload, creator_id=user.id)
    return Resp(data=ReportTemplateOut.model_validate(template))


@router.put("/templates/{template_id}", response_model=Resp[ReportTemplateOut])
def update_template(
    template_id: int,
    payload: ReportTemplateUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("report:template_manage")),
):
    """更新报告模板(内置模板亦可修改内容,但 is_builtin 不可变)。

    Args:
        template_id: 模板主键 ID(路径参数)。
        payload: 模板更新请求体(全部字段可选)。
        db: 数据库会话(由 Depends 注入)。
        user: 当前用户(由 require_permission 注入,已校验 report:template_manage 权限)。

    Returns:
        Resp[ReportTemplateOut]: 更新后的模板对象。

    Raises:
        NotFoundError: 模板不存在(404)。
    """
    template = report_template_service.update_template(db, template_id, payload)
    return Resp(data=ReportTemplateOut.model_validate(template))


@router.delete("/templates/{template_id}")
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("report:template_manage")),
):
    """删除报告模板(系统内置模板不可删除,返回 400)。

    Args:
        template_id: 模板主键 ID(路径参数)。
        db: 数据库会话(由 Depends 注入)。
        user: 当前用户(由 require_permission 注入,已校验 report:template_manage 权限)。

    Returns:
        Resp[None]: 删除成功返回 data=None;内置模板删除失败返回 400。

    Raises:
        NotFoundError: 模板不存在(404)。
    """
    try:
        report_template_service.delete_template(db, template_id)
    except ValueError as e:
        # 内置模板不可删除,返回 400(而非全局 500)
        return Response(
            content='{"code": 40000, "message": "' + str(e) + '", "data": null}',
            media_type="application/json",
            status_code=400,
        )
    return Resp(data=None)


# ============ 既有路由(参数路由 /{task_id},仅匹配 int) ============

@router.delete("/{task_id}", response_model=Resp[None])
def delete_report(task_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """删除报告"""
    report_service.delete_report(db, user, task_id)
    return Resp(data=None)


@router.get("", response_model=Resp[PageOut[ReportListItem]])
def list_reports(
    project_id: int = Query(None),
    start: str = Query(""),
    end: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """报告列表"""
    result = report_service.list_reports(db, user, project_id, start, end, page, page_size)
    return Resp(data=PageOut(**result))


@router.get("/{task_id}", response_model=Resp[ReportDetailOut])
def get_report(task_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """报告详情"""
    data = report_service.get_report_detail(db, user, task_id)
    return Resp(data=ReportDetailOut(**data))


@router.get("/{task_id}/export/word")
def export_word(task_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """导出Word报告"""
    from app.exporters.word_exporter import export_word_report
    detail = report_service.get_report_detail(db, user, task_id)
    buffer = export_word_report(detail)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename=review_report_{task_id}.docx"},
    )


@router.get("/{task_id}/export/pdf")
def export_pdf(task_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """导出PDF报告"""
    from app.exporters.pdf_exporter import export_pdf_report
    detail = report_service.get_report_detail(db, user, task_id)
    buffer = export_pdf_report(detail)
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=review_report_{task_id}.pdf"},
    )
