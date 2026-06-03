"""
FastAPI应用主入口
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.api.v1 import discussion as discussion_router
from app.api.v1.ws_discussion import ws_discuss
from app.core.config import settings
from app.core.error_handlers import register_handlers
from app.core.logger import setup_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期: 启动时初始化日志与Agent注册中心"""
    setup_logger()
    from app.agents.orchestrator import get_orchestrator
    get_orchestrator()
    yield


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

register_handlers(app)
app.include_router(api_router, prefix="/api")
app.add_api_websocket_route("/api/ws/discuss/{session_id}", ws_discuss)
app.include_router(discussion_router.router, prefix="/api")


@app.get("/healthz")
def healthz():
    """健康检查接口"""
    return {"status": "ok"}
