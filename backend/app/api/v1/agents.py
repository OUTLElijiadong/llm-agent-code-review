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
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents import AgentContext
from app.agents.clarify_store import ClarifyStore
from app.agents.event_bus import AgentEventBus
from app.agents.orchestrator import get_request_orchestrator
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AuthError, ForbiddenError
from app.core.security import decode_token
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


def _resolve_sse_user(authorization: Optional[str], token: Optional[str], db: Session) -> User:
    """SSE 专用鉴权: 从 Authorization 头或 token 查询参数解析用户。

    EventSource/部分流式场景无法自定义请求头,故同时支持 ?token= 查询参数。
    缺失/非法一律抛 AuthError(401),避免因「必填 Header 缺失」被 FastAPI 判成 400。
    """
    raw = None
    if authorization and authorization.startswith("Bearer "):
        raw = authorization[7:]
    elif token:
        raw = token
    if not raw:
        raise AuthError("缺少token", code=40100)
    try:
        payload = decode_token(raw)
    except Exception:
        raise AuthError("token非法或已过期", code=40101)
    u = db.get(User, int(payload["sub"]))
    if not u or u.status != 1:
        raise ForbiddenError("账号不存在或已禁用", code=40301)
    return u


@router.get("/events")
async def stream_agent_events(
    replay: int = Query(20, ge=0, le=100),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """SSE 实时事件流: Agent 调度 / 思考 / 完成 / 失败 / 追问

    v2.4 隔离策略:
        - admin: 接收全部事件
        - 非 admin: 仅接收 user_id 匹配的事件,以及 user_id=None 的系统级事件

    Args:
        replay: 订阅初期回放最近 N 条历史事件,默认 20
        token: 可选,SSE 鉴权令牌(等价于 Authorization: Bearer)
    """
    current_user = _resolve_sse_user(authorization, token, db)
    is_admin = current_user.role == "admin"
    current_user_id = current_user.id

    def _should_deliver(ev) -> bool:
        """v2.4: 按用户隔离过滤事件

        Args:
            ev: AgentEvent

        Returns:
            bool: 当前用户是否应收到此事件
        """
        # admin 接收全部
        if is_admin:
            return True
        # 系统级事件(无 user_id)所有用户都能收到
        if ev.user_id is None:
            return True
        # 仅接收自己的事件
        return ev.user_id == current_user_id

    async def event_source():
        bus = AgentEventBus.instance()
        yield ":connected\n\n"
        try:
            sub = bus.subscribe(replay=replay)
            while True:
                try:
                    ev = await asyncio.wait_for(sub.__anext__(), timeout=25.0)
                    if not _should_deliver(ev):
                        continue
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
    owner_user_id = pending.get("user_id")
    if owner_user_id is not None and owner_user_id != user.id and user.role != "admin":
        raise ForbiddenError("无权回填此追问", code=40300)
    intent_name = pending["intent"]
    merged = {**pending.get("payload", {}), **(payload.answers or {})}

    orch = get_request_orchestrator(db, user=user)
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


# =================== v2.4: MetaGPT 编排层 ===================


@router.get("/metagpt/info", response_model=Resp[dict])
def get_metagpt_info(
    _: User = Depends(get_current_user),
):
    """v2.4: MetaGPT 编排层信息

    返回 MetaGPT 模块的版本、可用工厂函数、已注册可适配为 Role 的 Agent 列表。
    用于前端展示多 Agent 宏观调控能力面板。
    """
    from app.agents.metagpt import (
        Environment,
        Message,
        Role,
        RoleAdapter,
        build_discussion_environment,
        build_review_environment,
    )
    from app.agents.registry import AgentRegistry

    registry = AgentRegistry.instance()
    adaptable_agents = []
    for name, agent in registry.list().items():
        adaptable_agents.append({
            "name": agent.name,
            "description": agent.description,
            "category": getattr(agent, "category", "general"),
            "icon": getattr(agent, "icon", ""),
            "color": getattr(agent, "color", ""),
        })

    return Resp(data={
        "version": "v2.4",
        "description": "MetaGPT 风格的多 Agent 编排层,提供 Environment/Role/Message 抽象",
        "components": {
            "Environment": Environment.__doc__.split("\n")[0] if Environment.__doc__ else "",
            "Role": Role.__doc__.split("\n")[0] if Role.__doc__ else "",
            "RoleAdapter": RoleAdapter.__doc__.split("\n")[0] if RoleAdapter.__doc__ else "",
            "Message": Message.__doc__.split("\n")[0] if Message.__doc__ else "",
        },
        "factories": {
            "build_review_environment": "构建代码审查 Environment,从 AgentRegistry 取审查相关 Agent",
            "build_discussion_environment": "构建圆桌讨论 Environment,订阅 DiscussTurn 动作",
        },
        "adaptable_agents": adaptable_agents,
        "default_review_agents": ["code_reviewer", "security_sentinel"],
        "default_discussion_agents": ["code_reviewer", "security_sentinel"],
    })


@router.get("/metagpt/preview", response_model=Resp[dict])
def preview_metagpt_environment(
    mode: str = Query("review", pattern="^(review|discussion)$"),
    user: User = Depends(get_current_user),
):
    """v2.4: 预览 MetaGPT Environment 配置(不实际执行 LLM 调用)

    根据模式构建 Environment,返回角色列表与配置信息,用于前端展示编排拓扑。
    不触发任何 LLM 调用,纯元数据查询。

    Args:
        mode: 环境模式,review=审查环境,discussion=讨论环境
    """
    from app.agents.metagpt import build_discussion_environment, build_review_environment
    from app.agents.registry import AgentRegistry

    trace_id = f"preview_{user.id}_{mode}"
    if mode == "review":
        env = build_review_environment(trace_id=trace_id, user_id=user.id)
    else:
        env = build_discussion_environment(trace_id=trace_id, user_id=user.id)

    registry = AgentRegistry.instance()
    roles_info = []
    for role_name in env.list_roles():
        role = env.get_role(role_name)
        if role is None:
            continue
        info = role.to_dict()
        # 补充实例化的 Agent 元数据
        agent = registry.get(role_name)
        if agent:
            info["agent_icon"] = getattr(agent, "icon", "")
            info["agent_color"] = getattr(agent, "color", "")
            info["agent_category"] = getattr(agent, "category", "")
        roles_info.append(info)

    return Resp(data={
        "mode": mode,
        "env_name": env.name,
        "trace_id": env.trace_id,
        "max_depth": env._max_depth,
        "roles": roles_info,
        "registered_agent_count": len(registry.list()),
    })
