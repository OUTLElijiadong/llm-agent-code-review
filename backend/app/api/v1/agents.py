"""
Agent 中心 API 路由

v1.0 路由保留:
- GET  /agents                   ReviewAgentProfile 5 个静态画像
- GET  /agents/type-mappings     审查类型 → 代理组合映射
- GET  /agents/usage             AiCallLog 聚合调用统计
- GET  /agents/overview          首屏数据聚合

v2.0 新增:
- GET  /agents/runtime           AgentRegistry 真实注册的 Agent 完整元数据
- GET  /agents/runtime/summary   注册总数与 category 分桶
- GET  /agents/situation         态势感知面板数据
- GET  /agents/events            SSE 实时事件流(M2 Agent 调用反馈)
- POST /agents/clarify           Clarify 回填后继续执行(M4 主动提问)
"""
import asyncio
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents import AgentContext
from app.agents.clarify_store import ClarifyStore
from app.agents.event_bus import AgentEventBus
from app.agents.orchestrator import get_orchestrator
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.agent import (
    AgentOverviewOut,
    AgentProfileOut,
    AgentRuntimeOut,
    AgentRuntimeSummaryOut,
    AgentSituationOut,
    AgentUsageOut,
    ReviewTypeMappingOut,
)
from app.schemas.common import Resp
from app.services import agent_service

router = APIRouter()


@router.get("", response_model=Resp[list[AgentProfileOut]])
def list_agents(_: User = Depends(get_current_user)):
    """v1.0 兼容: 列出 multi_agent 模块的 5 个静态画像"""
    return Resp(data=[AgentProfileOut(**p) for p in agent_service.list_profiles()])


@router.get("/type-mappings", response_model=Resp[list[ReviewTypeMappingOut]])
def list_type_mappings(_: User = Depends(get_current_user)):
    """列出审查类型 → 代理组合映射"""
    return Resp(data=[ReviewTypeMappingOut(**m) for m in agent_service.list_type_mappings()])


@router.get("/usage", response_model=Resp[list[AgentUsageOut]])
def get_usage(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """每个代理的调用统计 (普通用户仅自己,管理员看全部)"""
    user_id = None if user.role == "admin" else user.id
    rows = agent_service.get_usage(db, user_id)
    return Resp(data=[AgentUsageOut(**r) for r in rows])


@router.get("/overview", response_model=Resp[AgentOverviewOut])
def get_overview(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 中心首屏一次性返回:画像 + 映射 + 统计"""
    user_id = None if user.role == "admin" else user.id
    data = agent_service.get_overview(db, user_id)
    return Resp(data=AgentOverviewOut(**data))


# =================== v2.0 新增 ===================


@router.get("/runtime", response_model=Resp[list[AgentRuntimeOut]])
def list_runtime_agents(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """v2.0: 真实注册中心枚举,返回所有 BaseAgent 元数据 + 调用统计

    数据源是 AgentRegistry.instance(),确保 UI 显示数量与后端实际运行 Agent 一致。
    """
    user_id = None if user.role == "admin" else user.id
    rows = agent_service.get_runtime_agents(db, user_id)
    return Resp(data=[AgentRuntimeOut(**r) for r in rows])


@router.get("/runtime/summary", response_model=Resp[AgentRuntimeSummaryOut])
def get_runtime_summary(_: User = Depends(get_current_user)):
    """v2.0: 注册中心汇总,仅做计数,不查 DB"""
    return Resp(data=AgentRuntimeSummaryOut(**agent_service.get_runtime_summary()))


@router.get("/situation", response_model=Resp[AgentSituationOut])
def get_situation(
    minutes: int = Query(60, ge=15, le=240),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """v2.0: 态势感知面板数据(在岗/今日调用/N 分钟波形/热点)"""
    user_id = None if user.role == "admin" else user.id
    data = agent_service.get_situation(db, user_id, minutes)
    return Resp(data=AgentSituationOut(**data))


# =================== v2.0 M2: Agent 调用反馈 SSE ===================


@router.get("/events")
async def stream_agent_events(
    replay: int = Query(20, ge=0, le=100),
    user: User = Depends(get_current_user),
):
    """SSE 实时事件流: Agent 调度 / 思考 / 完成 / 失败 / 追问

    Args:
        replay: 订阅初期回放最近 N 条历史事件,默认 20
    """
    async def event_source():
        bus = AgentEventBus.instance()
        yield ":connected\n\n"
        try:
            sub = bus.subscribe(replay=replay)
            while True:
                try:
                    ev = await asyncio.wait_for(sub.__anext__(), timeout=25.0)
                    data = json.dumps(ev.to_dict(), ensure_ascii=False)
                    yield f"event: agent\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ":heartbeat\n\n"
        except asyncio.CancelledError:
            return
        except StopAsyncIteration:
            return

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# =================== v2.0 M4: Clarify 回填 ===================


class ClarifyAnswers(BaseModel):
    clarify_id: str
    answers: dict


@router.post("/clarify", response_model=Resp[dict])
def submit_clarification(
    payload: ClarifyAnswers,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户回填 Clarify 追问的答案后,合并 payload 继续执行原 intent"""
    pending = ClarifyStore.instance().pop(payload.clarify_id)
    if pending is None:
        return Resp(code=41001, message="追问已过期或不存在,请重新提问", data={})
    intent_name = pending["intent"]
    merged = {**pending.get("payload", {}), **(payload.answers or {})}

    orch = get_orchestrator()
    orch.inject_db(db, user=user)
    ctx = AgentContext(user_id=user.id, extra={})
    result = orch.chat_agent.dispatch_with_payload(intent_name, merged, ctx)
    if not result.success:
        from app.ai.exceptions import AiServiceError
        raise AiServiceError(result.error or "执行失败", code=50202)
    if isinstance(result.data, dict):
        return Resp(data={
            "content": result.data.get("content", ""),
            "clarify": result.data.get("clarify"),
            "model": result.model,
        })
    return Resp(data={"content": result.data, "model": result.model})
