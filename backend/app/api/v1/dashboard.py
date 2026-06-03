"""
仪表盘API路由
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.dashboard import FrequencyItem, IssueTypeItem, RiskItem, ScoreTrendItem, SummaryOut
from app.services import dashboard_service

router = APIRouter()


@router.get("/summary", response_model=Resp[SummaryOut])
def summary(scope: str = Query("mine"), db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    """仪表盘汇总指标"""
    data = dashboard_service.get_summary(db, user)
    return Resp(data=SummaryOut(**data))


@router.get("/risk-distribution", response_model=Resp[list[RiskItem]])
def risk_distribution(days: int = Query(30), db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """风险等级分布"""
    data = dashboard_service.get_risk_distribution(db, user, days)
    return Resp(data=[RiskItem(**d) for d in data])


@router.get("/issue-type-statistics", response_model=Resp[list[IssueTypeItem]])
def issue_type_statistics(days: int = Query(30), db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """问题类型分布"""
    data = dashboard_service.get_issue_type_statistics(db, user, days)
    return Resp(data=[IssueTypeItem(**d) for d in data])


@router.get("/score-trend", response_model=Resp[list[ScoreTrendItem]])
def score_trend(limit: int = Query(10), db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """评分趋势"""
    data = dashboard_service.get_score_trend(db, user, limit)
    return Resp(data=[ScoreTrendItem(**d) for d in data])


@router.get("/review-frequency", response_model=Resp[list[FrequencyItem]])
def review_frequency(days: int = Query(30), db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """审查频次趋势"""
    data = dashboard_service.get_review_frequency(db, user, days)
    return Resp(data=[FrequencyItem(**d) for d in data])
