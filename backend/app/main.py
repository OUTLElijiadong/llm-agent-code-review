"""
FastAPI应用主入口
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from slowapi.errors import RateLimitExceeded

from app.api import api_router
from app.api.responses import router as responses_router
from app.api.v1 import discussion as discussion_router
from app.api.v1.ws_discussion import ws_discuss
from app.core.config import settings
from app.core.error_handlers import register_handlers
from app.core.logger import setup_logger
from app.core.observability import (
    RequestContextMiddleware,
    database_is_ready,
    get_request_id,
    render_metrics,
)
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


def _reconcile_orphan_reviews() -> list[tuple[int, int, str]]:
    """启动时识别孤儿审查任务并返回恢复引用。

    审查在后台守护线程中执行,进程重启/崩溃会让线程随进程消失,但 DB 中任务仍停留在
    status='running',在前端表现为永远「运行中」。新进程刚启动尚未派生任何审查线程,
    因此此刻所有 running 任务都是上一个进程遗留的孤儿。保持 running 并写入恢复提示，
    待 Agent 注册完成后重新派发，避免页面关闭或服务更新造成任务永久中止。
    """
    from loguru import logger
    from sqlalchemy import inspect

    from app.core.database import SessionLocal, engine

    try:
        insp = inspect(engine)
        if "review_task" not in insp.get_table_names():
            return []
        db = SessionLocal()
        try:
            from app.models.review_task import ReviewTask

            orphans = db.query(ReviewTask).filter(ReviewTask.status == "running").all()
            if not orphans:
                return []
            task_refs = [
                (int(t.id), int(t.user_id), str(t.execution_token or ""))
                for t in orphans
            ]
            for t in orphans:
                t.error_message = "进程重启导致审查中断，服务端正在自动恢复"
                t.end_time = None
            db.commit()
            logger.warning(
                f"[reconcile] 识别 {len(orphans)} 个孤儿审查任务，等待自动恢复"
            )
            return task_refs
        finally:
            db.close()
    except Exception as e:  # 回收失败不应阻断启动
        logger.warning(f"[reconcile] 孤儿审查任务识别失败(忽略): {e}")
        return []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期: 启动时初始化日志、补齐表结构、Agent注册中心与治理调度器"""
    setup_logger()
    _ensure_schema()
    orphan_reviews = _reconcile_orphan_reviews()
    from app.agents.event_bus import AgentEventBus
    from app.agents.orchestrator import get_orchestrator

    # 部署重启善后:把执行中的运行转为带恢复标记的可重试状态；原生等待态保留。
    from app.api.v1.agent_responses import sweep_stale_active_runs
    from app.core.database import SessionLocal as _SessionLocal
    from app.services import agent_mesh_service
    from app.services.agent_mesh_dispatcher import start_agent_mesh_dispatcher, stop_agent_mesh_dispatcher
    from app.services.agent_scheduler_runtime import start_agent_governance_scheduler, stop_agent_governance_scheduler
    from app.services.agent_team_dispatcher import start_agent_team_dispatcher, stop_agent_team_dispatcher
    from app.services.jarvis_patrol_service import start_jarvis_patrol, stop_jarvis_patrol

    _sweep_db = _SessionLocal()
    try:
        _swept = sweep_stale_active_runs(_sweep_db)
        _jarvis_cleanup = agent_mesh_service.sweep_blocked_jarvis_messages(_sweep_db)
        if _swept:
            from loguru import logger as _logger

            _logger.info("[startup] 清扫 {} 个部署重启遗留的僵尸运行", _swept)
        if _jarvis_cleanup["messages"] or _jarvis_cleanup["runs"]:
            from loguru import logger as _logger

            _logger.info(
                "[startup] JARVIS 成本保护收敛 messages={} runs={}",
                _jarvis_cleanup["messages"],
                _jarvis_cleanup["runs"],
            )
    finally:
        _sweep_db.close()

    get_orchestrator()
    AgentEventBus.instance().start_relay()
    start_agent_mesh_dispatcher()
    start_agent_team_dispatcher()
    start_agent_governance_scheduler()
    start_jarvis_patrol()
    from app.services.background_task_recovery import start_agent_run_recovery
    from app.services.review_service import resume_interrupted_tasks
    from app.services.sandbox_service import resume_interrupted_environments

    start_agent_run_recovery()
    resume_interrupted_tasks(orphan_reviews)
    resume_interrupted_environments()
    try:
        yield
    finally:
        AgentEventBus.instance().stop_relay()
        stop_agent_mesh_dispatcher()
        stop_agent_team_dispatcher()
        stop_agent_governance_scheduler()
        stop_jarvis_patrol()


app = FastAPI(
    title="棱镜 Prism · 智能代码审查平台",
    version=settings.app_version,
    docs_url="/docs" if settings.openapi_enabled else None,
    redoc_url="/redoc" if settings.openapi_enabled else None,
    openapi_url="/openapi.json" if settings.openapi_enabled else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)

# 接口限流: 注册 Limiter 与 429 处理器(返回项目统一的 Resp 信封)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """命中限流时返回 429 + 统一错误结构"""
    request_id = get_request_id(request)
    return JSONResponse(
        status_code=429,
        headers={"X-Request-Id": request_id},
        content={
            "code": 42900,
            "message": "请求过于频繁,请稍后再试",
            "detail": str(exc.detail) if getattr(exc, "detail", None) else None,
            "request_id": request_id,
        },
    )


register_handlers(app)
app.include_router(api_router, prefix="/api")
app.include_router(responses_router)
app.add_api_websocket_route("/api/ws/discuss/{session_id}", ws_discuss)
app.include_router(discussion_router.router, prefix="/api")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    """返回进程存活状态与不可变发布标识。

    Returns:
        dict[str, str]: 存活状态和当前应用 release。
    """
    return {
        "status": "ok",
        "version": settings.app_version,
        "release": settings.app_release,
    }


@app.get("/readyz", include_in_schema=False)
def readyz() -> JSONResponse:
    """检查数据库连接并返回应用就绪状态。

    Returns:
        JSONResponse: 数据库可用时 200，否则返回不含凭据的 503。
    """
    if database_is_ready():
        return JSONResponse(
            status_code=200,
            content={
                "status": "ready",
                "version": settings.app_version,
                "release": settings.app_release,
            },
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        status_code=503,
        content={
            "status": "not_ready",
            "version": settings.app_version,
            "release": settings.app_release,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """返回 Prometheus 文本指标；公网网关不得代理此端点。

    Returns:
        Response: Prometheus exposition format 响应。
    """
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
