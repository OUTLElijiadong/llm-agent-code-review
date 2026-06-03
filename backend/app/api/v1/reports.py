"""
报告API路由: 列表、详情、导出Word/PDF
"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.report import ReportDetailOut, ReportListItem
from app.services import report_service

router = APIRouter()


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
