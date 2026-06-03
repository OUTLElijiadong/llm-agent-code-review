"""
Agent 中心服务模块: 暴露 multi_agent 画像与调用统计

v2.0 在 v1.0 基础上新增:
- get_runtime_agents():    从 AgentRegistry 真实枚举,带 AiCallLog 统计回填
- get_situation():         态势感知数据(在岗/工作中/今日调用/波形/热点)
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.agents.event_bus import AgentEventBus
from app.agents.events import AgentEventType
from app.agents.registry import AgentRegistry
from app.ai.multi_agent import (
    GENERAL_AGENT,
    MAINTAINABILITY_AGENT,
    PERFORMANCE_AGENT,
    RELIABILITY_AGENT,
    SECURITY_AGENT,
    ReviewAgentProfile,
)
from app.models.ai_call_log import AiCallLog

# 注册顺序即前端展示顺序;通用代理在最前
ALL_AGENTS: tuple[ReviewAgentProfile, ...] = (
    GENERAL_AGENT,
    SECURITY_AGENT,
    RELIABILITY_AGENT,
    PERFORMANCE_AGENT,
    MAINTAINABILITY_AGENT,
)

# review_type → (label, agent_code 列表)
REVIEW_TYPE_LABELS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("quick", "quick · 快速审查", ("general",)),
    ("standard", "standard · 标准审查", ("general",)),
    ("security", "security · 安全审查", ("security", "reliability")),
    ("performance", "performance · 性能审查", ("performance", "maintainability")),
    ("full", "full · 全面审查", ("security", "reliability", "performance", "maintainability")),
)


def profile_to_dict(profile: ReviewAgentProfile, enabled: bool = True) -> dict:
    """把 dataclass 画像转成可被 Pydantic 接受的 dict"""
    return {
        "code": profile.code,
        "name": profile.name,
        "focus": profile.focus,
        "issue_types": list(profile.issue_types),
        "instruction": profile.instruction,
        "enabled": enabled,
    }


def list_profiles() -> list[dict]:
    """返回所有审查代理画像

    Returns:
        list[dict]: 5 个代理画像
    """
    return [profile_to_dict(p) for p in ALL_AGENTS]


def list_type_mappings() -> list[dict]:
    """返回 review_type → 代理组合映射"""
    return [
        {"review_type": rt, "label": label, "agent_codes": list(codes)}
        for rt, label, codes in REVIEW_TYPE_LABELS
    ]


def _model_match_token(code: str) -> str:
    """生成与 `multi_agent.get_model_label` 兼容的匹配片段"""
    return f"%/{code}-agent"


def get_usage(db: Session, user_id: Optional[int] = None) -> list[dict]:
    """统计每个代理近期调用次数(v1.0 兼容)

    通用代理通过 model_name 不带后缀来识别;专项代理通过 `/{code}-agent` 后缀识别;
    多代理审查(model_name 形如 `xxx/multi-agent`)按其覆盖的代理逐一计入。

    Args:
        db: 数据库会话
        user_id: 限定用户(普通用户视角);None 表示全部

    Returns:
        list[dict]: 每个代理一条记录,字段对齐 AgentUsageOut
    """
    base = db.query(
        AiCallLog.model_name,
        AiCallLog.status,
        AiCallLog.create_time,
    )
    if user_id is not None:
        base = base.filter(AiCallLog.user_id == user_id)
    rows = base.all()

    multi_codes = {"security", "reliability", "performance", "maintainability"}
    usage: dict[str, dict] = {
        agent.code: {
            "code": agent.code,
            "name": agent.name,
            "call_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "last_called_at": None,
        }
        for agent in ALL_AGENTS
    }

    for model_name, status, create_time in rows:
        if not model_name:
            continue
        if model_name.endswith("/multi-agent"):
            codes = multi_codes
        else:
            matched = None
            for code in {a.code for a in ALL_AGENTS if a.code != "general"}:
                if model_name.endswith(f"/{code}-agent"):
                    matched = code
                    break
            codes = {matched} if matched else {"general"}

        for code in codes:
            slot = usage.get(code)
            if slot is None:
                continue
            slot["call_count"] += 1
            if status == "success":
                slot["success_count"] += 1
            elif status == "failed":
                slot["failed_count"] += 1
            if create_time is not None:
                iso = create_time.isoformat() if isinstance(create_time, datetime) else str(create_time)
                if slot["last_called_at"] is None or iso > slot["last_called_at"]:
                    slot["last_called_at"] = iso

    return [usage[a.code] for a in ALL_AGENTS]


def get_overview(db: Session, user_id: Optional[int] = None) -> dict:
    """Agent 中心首屏数据聚合"""
    return {
        "agents": list_profiles(),
        "type_mappings": list_type_mappings(),
        "usage": get_usage(db, user_id),
    }


# =================== v2.0 新增 ===================


def _ai_log_base_query(db: Session, user_id: Optional[int]):
    q = db.query(
        AiCallLog.model_name,
        AiCallLog.status,
        AiCallLog.create_time,
    )
    if user_id is not None:
        q = q.filter(AiCallLog.user_id == user_id)
    return q


# 审查画像 code → 注册中心 BaseAgent code 映射
# 保证 AiCallLog 中的 /security-agent 等记录能被正确归因到 security_sentinel 等真实 Agent
_PROFILE_TO_REGISTRY_CODE: dict[str, str] = {
    "general": "code_reviewer",
    "security": "security_sentinel",
    "reliability": "code_reviewer",
    "performance": "code_reviewer",
    "maintainability": "code_reviewer",
}

# 反向: registry_code → profile_codes (用于 multi-agent 统计)
_REGISTRY_TO_PROFILE_CODES: dict[str, set[str]] = {
    "security_sentinel": {"security"},
    "code_reviewer": {"general", "reliability", "performance", "maintainability"},
}


def _agent_codes_from_model_name(model_name: str, all_codes: set[str]) -> set[str]:
    """从 AiCallLog.model_name 反推涉及的 Agent code 集合

    支持三种 model_name 格式:
    1. ``deepseek-chat/security-agent`` → 映射到 registry code(如 security_sentinel)
    2. ``deepseek-chat/multi-agent``   → 匹配所有已知的 registry codes
    3. ``deepseek-chat`` (raw)         → 无后缀,根据 all_codes 中有哪些 registry code 来决定

    Args:
        model_name: AiCallLog.model_name 字段值
        all_codes: 当前上下文中所有合法的 registry agent code 集合

    Returns:
        set[str]: 应计入的 registry agent code 集合
    """
    if not model_name:
        return set()
    # multi-agent: 所有已知 agent 都计入
    if model_name.endswith("/multi-agent"):
        result: set[str] = set()
        for profile_code, reg_code in _PROFILE_TO_REGISTRY_CODE.items():
            if reg_code in all_codes:
                result.add(reg_code)
        return result
    # 解析 /xxx-agent 后缀
    if "/" in model_name and "-agent" in model_name:
        suffix = model_name.rsplit("/", 1)[-1]
        profile_code = suffix.replace("-agent", "")
        # 先映射到 registry code
        reg_code = _PROFILE_TO_REGISTRY_CODE.get(profile_code)
        if reg_code and reg_code in all_codes:
            return {reg_code}
        # 如果 profile_code 本身就是 registry code(如 security_sentinel-agent)
        if profile_code in all_codes:
            return {profile_code}
        # 兜底: 如果有 code_reviewer,归给 code_reviewer
        if "code_reviewer" in all_codes:
            return {"code_reviewer"}
        return set()
    # 无后缀(raw model name): 如果 all_codes 里有 code_reviewer,归给它
    if "code_reviewer" in all_codes:
        return {"code_reviewer"}
    return set()


def _aggregate_log_stats(
    db: Session, user_id: Optional[int], codes: set[str],
) -> dict[str, dict]:
    """按 Agent code 聚合调用次数/成功/失败/最近一次时间"""
    stats: dict[str, dict] = {
        code: {"call_count": 0, "success_count": 0, "failed_count": 0,
               "last_called_at": None}
        for code in codes
    }
    rows = _ai_log_base_query(db, user_id).all()
    for model_name, status, ctime in rows:
        for code in _agent_codes_from_model_name(model_name, codes):
            slot = stats[code]
            slot["call_count"] += 1
            if status == "success":
                slot["success_count"] += 1
            elif status == "failed":
                slot["failed_count"] += 1
            if ctime is not None:
                iso = ctime.isoformat() if isinstance(ctime, datetime) else str(ctime)
                if slot["last_called_at"] is None or iso > slot["last_called_at"]:
                    slot["last_called_at"] = iso
    return stats


def get_runtime_agents(db: Session, user_id: Optional[int] = None) -> list[dict]:
    """v2.0: 从 AgentRegistry 真实枚举 Agent + 回填 AiCallLog 统计

    保证前端看到的 Agent 数量 / 名称 / 描述与后端实际注册的 BaseAgent 完全一致。

    Args:
        db: 数据库会话
        user_id: 普通用户视角,只统计自己的调用;管理员传 None

    Returns:
        list[dict]: 符合 AgentRuntimeOut Schema 的字段
    """
    runtime = AgentRegistry.instance().list_runtime()
    codes = {r["code"] for r in runtime}
    stats = _aggregate_log_stats(db, user_id, codes)
    for item in runtime:
        s = stats.get(item["code"], {})
        item["call_count"] = s.get("call_count", 0)
        item["success_count"] = s.get("success_count", 0)
        item["failed_count"] = s.get("failed_count", 0)
        item["last_called_at"] = s.get("last_called_at")
    return runtime


def get_runtime_summary() -> dict:
    """注册中心汇总: 总数 + category 分桶 (不查 DB,纯运行时)"""
    return AgentRegistry.instance().summary()


def get_situation(db: Session, user_id: Optional[int] = None,
                  minutes: int = 60) -> dict:
    """v2.0 态势感知数据

    通过 EventBus 近 60s 事件判断 Agent 是否正在工作,
    不再硬编码 working=0。

    Args:
        db: 数据库会话
        user_id: 用户范围 (None 表示全平台,仅 admin)
        minutes: 波形覆盖的最近 N 分钟,默认 60

    Returns:
        dict: 符合 AgentSituationOut Schema
    """
    runtime = AgentRegistry.instance().list_runtime()
    online = len(runtime)
    agent_codes = {r["code"] for r in runtime}

    # 通过 EventBus 近期事件判断哪些 Agent 正在工作
    import time as _time
    now_ts = _time.time()
    recent_events = AgentEventBus.instance().recent(limit=200)
    active_agents: set[str] = set()
    for ev in recent_events:
        try:
            ev_ts = datetime.fromisoformat(ev.timestamp).timestamp()
        except (ValueError, TypeError):
            continue
        if (now_ts - ev_ts) <= 60 and ev.type in (
            AgentEventType.DISPATCH, AgentEventType.THINKING, AgentEventType.PROGRESS,
        ):
            active_agents.add(ev.agent)
    working = len(active_agents & agent_codes)
    idle = max(0, online - working)

    # 今日调用数 + 热点 Agent
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    base = _ai_log_base_query(db, user_id).filter(AiCallLog.create_time >= today_start)
    rows_today = base.all()
    today_calls = len(rows_today)

    codes = {r["code"] for r in runtime}
    hotspot_counts: dict[str, int] = defaultdict(int)
    for model_name, _, _ in rows_today:
        for code in _agent_codes_from_model_name(model_name, codes):
            hotspot_counts[code] += 1
    name_by_code = {r["code"]: r["name"] for r in runtime}
    hotspots = [
        {"code": c, "name": name_by_code.get(c, c), "count": cnt}
        for c, cnt in sorted(hotspot_counts.items(), key=lambda x: -x[1])[:5]
    ]

    # 近 N 分钟调用波形(按分钟桶)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    spectrum_q = db.query(AiCallLog.create_time).filter(
        AiCallLog.create_time >= cutoff
    )
    if user_id is not None:
        spectrum_q = spectrum_q.filter(AiCallLog.user_id == user_id)
    raw: dict[str, int] = defaultdict(int)
    for (create_time,) in spectrum_q.all():
        if create_time.tzinfo:
            bucket = create_time.strftime("%H:%M")
        else:
            bucket = create_time.replace(tzinfo=timezone.utc).strftime("%H:%M")
        raw[bucket] += 1
    spectrum: list[dict] = []
    base_t = datetime.now(timezone.utc) - timedelta(minutes=minutes - 1)
    for i in range(minutes):
        t = base_t + timedelta(minutes=i)
        bucket = t.strftime("%H:%M")
        spectrum.append({"bucket": bucket, "count": int(raw.get(bucket, 0) or 0)})

    return {
        "online": online,
        "working": working,
        "idle": idle,
        "today_calls": today_calls,
        "spectrum": spectrum,
        "hotspots": hotspots,
    }
