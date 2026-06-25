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
from app.core.dependencies import get_current_user, require_admin
from app.core.exceptions import AuthError, ForbiddenError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.core.security import decode_token
from app.models.user import User
from app.schemas.agent import (
    AgentOverviewOut,
    AgentProfileOut,
    AgentRuntimeOut,
    AgentRuntimeSummaryOut,
    AgentSituationOut,
    AgentSkillRecordOut,
    AgentUsageOut,
    ReviewTypeMappingOut,
    SkillInvokeIn,
    SkillInvokeOut,
    SkillMetaOut,
)
from app.schemas.common import Resp
from app.services import agent_service, skill_service

router = APIRouter()


@router.get("", response_model=Resp[list[AgentProfileOut]],
            dependencies=[Depends(require_permission(PermissionCode.AGENT_VIEW))])
def list_agents(_: User = Depends(get_current_user)):
    """v1.0 兼容: 列出 multi_agent 模块的 5 个静态画像"""
    return Resp(data=[AgentProfileOut(**p) for p in agent_service.list_profiles()])


@router.get("/type-mappings", response_model=Resp[list[ReviewTypeMappingOut]],
            dependencies=[Depends(require_permission(PermissionCode.AGENT_VIEW))])
def list_type_mappings(_: User = Depends(get_current_user)):
    """列出审查类型 → 代理组合映射"""
    return Resp(data=[ReviewTypeMappingOut(**m) for m in agent_service.list_type_mappings()])


@router.get("/usage", response_model=Resp[list[AgentUsageOut]],
            dependencies=[Depends(require_permission(PermissionCode.AGENT_VIEW))])
def get_usage(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """每个代理的调用统计 (普通用户仅自己,管理员看全部)"""
    user_id = None if user.role == "admin" else user.id
    rows = agent_service.get_usage(db, user_id)
    return Resp(data=[AgentUsageOut(**r) for r in rows])


@router.get("/overview", response_model=Resp[AgentOverviewOut],
            dependencies=[Depends(require_permission(PermissionCode.AGENT_VIEW))])
def get_overview(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 中心首屏一次性返回:画像 + 映射 + 统计"""
    user_id = None if user.role == "admin" else user.id
    data = agent_service.get_overview(db, user_id)
    return Resp(data=AgentOverviewOut(**data))


# =================== v2.0 新增 ===================


@router.get("/runtime", response_model=Resp[list[AgentRuntimeOut]],
            dependencies=[Depends(require_permission(PermissionCode.AGENT_VIEW))])
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


@router.get("/runtime/summary", response_model=Resp[AgentRuntimeSummaryOut],
            dependencies=[Depends(require_permission(PermissionCode.AGENT_VIEW))])
def get_runtime_summary(_: User = Depends(get_current_user)):
    """v2.0: 注册中心汇总,仅做计数,不查 DB"""
    return Resp(data=AgentRuntimeSummaryOut(**agent_service.get_runtime_summary()))


@router.get("/situation", response_model=Resp[AgentSituationOut],
            dependencies=[Depends(require_permission(PermissionCode.AGENT_VIEW))])
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


@router.post("/clarify", response_model=Resp[dict],
             dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))])
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


@router.get("/metagpt/info", response_model=Resp[dict],
            dependencies=[Depends(require_permission(PermissionCode.AGENT_VIEW))])
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
    )
    from app.agents.registry import AgentRegistry

    registry = AgentRegistry.instance()
    # registry.list() 返回的是 name -> description 字符串映射,不能当 Agent 对象用。
    # 改用 list_runtime() 拿规范化的运行时元数据 dict(含 name/description/category/icon/color)。
    adaptable_agents = [
        {
            "name": item["name"],
            "description": item.get("description", ""),
            "category": item.get("category", "general"),
            "icon": item.get("icon", ""),
            "color": item.get("color", ""),
        }
        for item in registry.list_runtime()
    ]

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


@router.get("/metagpt/preview", response_model=Resp[dict],
            dependencies=[Depends(require_permission(PermissionCode.AGENT_VIEW))])
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


# =================== v3.0 AgentSkill 升级: Skill 管理路由 ===================


@router.get(
    "/{agent_name}/skills",
    response_model=Resp[list[SkillMetaOut]],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_VIEW))],
)
def list_agent_skills(
    agent_name: str,
    _: User = Depends(get_current_user),
):
    """列出指定 Agent 挂载的所有 Skill 元数据

    供前端 SkillManager 页面展示每个 Agent 的 Skill 列表,
    含 name/description/type/invocable/agent_name 字段。

    Args:
        agent_name: Agent name(如 code_reviewer)

    Returns:
        Resp[list[SkillMetaOut]]: Skill 元数据列表
    """
    from app.agents.orchestrator import get_orchestrator

    orch = get_orchestrator()
    skills = orch.list_agent_skills(agent_name)
    return Resp(data=[SkillMetaOut(**s) for s in skills])


@router.post(
    "/{agent_name}/skills/{skill_name}/invoke",
    response_model=Resp[SkillInvokeOut],
)
def invoke_agent_skill(
    agent_name: str,
    skill_name: str,
    payload: SkillInvokeIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """手动调用指定 Agent 的指定 Skill(admin only, 写 audit_log)

    通过 Orchestrator.invoke_skill 调用 skill_service 统一入口,
    自动写 agent_skill_record(trigger_type=manual)与 audit_log。

    Args:
        agent_name: Agent name(如 code_reviewer)
        skill_name: Skill name(如 code_reviewer.self_improve)
        payload: SkillInvokeIn 请求体, 含 action 与 params
        db: 数据库会话
        admin: 当前管理员用户(由 require_admin 注入)

    Returns:
        Resp[SkillInvokeOut]: Skill 调用结果, 含 success/data/effect/duration_ms/record_id
    """
    from app.agents.base import AgentContext
    from app.agents.events import new_trace_id

    # 生成 trace_id 写入 ctx.extra,供 Skill 日志透传(tid=xxx 前缀)
    trace_id = new_trace_id()
    orch = get_request_orchestrator(db, user=admin)
    ctx = AgentContext(
        user_id=admin.id,
        extra={
            "api": "invoke_agent_skill",
            "trace_id": trace_id,
        },
    )

    # 组装 params: action 优先, 合并 payload.params
    params: dict = {}
    if payload.action:
        params["action"] = payload.action
    if payload.params:
        params.update(payload.params)

    result = orch.invoke_skill(
        agent_name=agent_name,
        skill_name=skill_name,
        params=params,
        ctx=ctx,
        trigger_type="manual",
        trigger_source=f"api:POST /agents/{agent_name}/skills/{skill_name}/invoke",
    )

    data = result.data if isinstance(result.data, dict) else {}
    return Resp(data=SkillInvokeOut(
        success=result.success,
        data=data.get("data"),
        error=result.error or data.get("error"),
        effect=data.get("effect", "success" if result.success else "failed"),
        duration_ms=data.get("duration_ms", result.duration_ms),
        record_id=data.get("record_id"),
    ))


@router.get(
    "/skill-records",
    response_model=Resp[list[AgentSkillRecordOut]],
)
def list_skill_records(
    agent_name: Optional[str] = Query(None, description="按 Agent 过滤"),
    skill_name: Optional[str] = Query(None, description="按 Skill 过滤"),
    trigger_type: Optional[str] = Query(
        None, description="按触发类型过滤(manual/scheduled/event/proactive)"
    ),
    limit: int = Query(20, ge=1, le=100, description="返回上限,默认 20"),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """查询 Skill 调用记录(admin only)

    供 SkillManager 页面展示调用历史,支持按 Agent/Skill/触发类型过滤。

    Args:
        agent_name: 按 Agent 过滤(可选)
        skill_name: 按 Skill 过滤(可选)
        trigger_type: 按触发类型过滤(可选, manual/scheduled/event/proactive)
        limit: 返回上限(1-100, 默认 20)
        db: 数据库会话
        admin: 当前管理员用户(由 require_admin 注入)

    Returns:
        Resp[list[AgentSkillRecordOut]]: Skill 调用记录列表(按 create_time 倒序)
    """
    records = skill_service.list_recent_records(
        db=db,
        agent_name=agent_name,
        skill_name=skill_name,
        trigger_type=trigger_type,
        limit=limit,
    )
    return Resp(data=[AgentSkillRecordOut(**r) for r in records])
