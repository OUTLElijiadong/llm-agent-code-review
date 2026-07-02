"""
FastAPI应用主入口
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.api import api_router
from app.api.v1 import discussion as discussion_router
from app.api.v1.ws_discussion import ws_discuss
from app.core.config import settings
from app.core.error_handlers import register_handlers
from app.core.logger import setup_logger
from app.core.rate_limit import limiter


def _ensure_schema() -> None:
    """轻量自动补列: 为已存在的旧库补上新增列,避免升级后查询报错

    采用 inspector 判断 + 幂等 ALTER,SQLite/MySQL 通用。
    新建库由 init 脚本/建表逻辑直接带列,这里会跳过。
    """
    from loguru import logger
    from sqlalchemy import inspect, text

    from app.core.database import engine

    try:
        insp = inspect(engine)
        if "user" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("user")}
        if "token_version" in cols:
            return
        tbl = engine.dialect.identifier_preparer.quote("user")
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {tbl} ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"
            ))
        logger.info("[schema] 已为 user 表自动补建 token_version 列")
    except Exception as e:  # 补列失败不应阻断启动
        logger.warning(f"[schema] token_version 自动补列检查失败(忽略): {e}")


def _reconcile_orphan_reviews() -> None:
    """启动时回收孤儿审查任务。

    审查在后台守护线程中执行,进程重启/崩溃会让线程随进程消失,但 DB 中任务仍停留在
    status='running',在前端表现为永远「运行中」。新进程刚启动尚未派生任何审查线程,
    因此此刻所有 running 任务都是上一个进程遗留的孤儿,统一标记为 failed,闭合数据状态。
    """
    from datetime import datetime, timezone

    from loguru import logger
    from sqlalchemy import inspect

    from app.core.database import SessionLocal, engine

    try:
        insp = inspect(engine)
        if "review_task" not in insp.get_table_names():
            return
        db = SessionLocal()
        try:
            from app.models.review_task import ReviewTask

            orphans = db.query(ReviewTask).filter(ReviewTask.status == "running").all()
            if not orphans:
                return
            now = datetime.now(timezone.utc)
            for t in orphans:
                t.status = "failed"
                t.error_message = "进程重启导致审查中断(启动时自动回收)"
                t.end_time = now
            db.commit()
            logger.warning(
                f"[reconcile] 启动回收 {len(orphans)} 个孤儿审查任务(running→failed)"
            )
        finally:
            db.close()
    except Exception as e:  # 回收失败不应阻断启动
        logger.warning(f"[reconcile] 孤儿审查任务回收失败(忽略): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期: 启动时初始化日志、补齐表结构、Agent注册中心与治理调度器"""
    setup_logger()
    _ensure_schema()
    _reconcile_orphan_reviews()
    from app.agents.orchestrator import get_orchestrator
    from app.services.agent_scheduler_runtime import start_agent_governance_scheduler, stop_agent_governance_scheduler

    get_orchestrator()
    start_agent_governance_scheduler()
    try:
        yield
    finally:
        stop_agent_governance_scheduler()


app = FastAPI(
    title="棱镜 Prism · 智能代码审查平台",
    version="1.0.0",
    docs_url="/docs" if settings.openapi_enabled else None,
    redoc_url="/redoc" if settings.openapi_enabled else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 接口限流: 注册 Limiter 与 429 处理器(返回项目统一的 Resp 信封)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """命中限流时返回 429 + 统一错误结构"""
    return JSONResponse(
        status_code=429,
        content={
            "code": 42900,
            "message": "请求过于频繁,请稍后再试",
            "detail": str(exc.detail) if getattr(exc, "detail", None) else None,
            "request_id": request.headers.get("X-Request-Id"),
        },
    )


register_handlers(app)
app.include_router(api_router, prefix="/api")
app.add_api_websocket_route("/api/ws/discuss/{session_id}", ws_discuss)
app.include_router(discussion_router.router, prefix="/api")


@app.get("/healthz")
def healthz():
    """健康检查接口"""
    return {"status": "ok"}
