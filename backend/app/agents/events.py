"""Agent 调用事件模型 (v2.0 M2 + v2.3 M7)

每次 Agent 调度 / 思考 / 进行 / 完成 / 失败 / 追问 / 讨论都广播一条事件,
前端通过 SSE/WS 订阅实时呈现"调度链"。
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class AgentEventType(str, Enum):
    DISPATCH = "dispatch"
    THINKING = "thinking"
    PROGRESS = "progress"
    COMPLETE = "complete"
    FAILED = "failed"
    CLARIFY = "clarify"
    DISCUSS = "discuss"
    # v3.0 AgentSkill 升级: Skill 触发类事件(供 event_bus 订阅触发 Skill)
    # 详见 CONSENSUS §6 事件订阅清单
    REVIEW_ISSUE_STATUS_CHANGED = "review_issue_status_changed"      # 审查问题状态变更
    SECURITY_SCAN_COMPLETED = "security_scan_completed"              # 安全扫描完成
    AI_CALL_THRESHOLD_REACHED = "ai_call_threshold_reached"          # AI 调用量达阈值
    EVOLUTION_PROPOSAL_PROMOTED = "evolution_proposal_promoted"      # 进化提案被批准生效
    SKILL_TRIGGER = "skill_trigger"                                  # Skill 触发通用事件
    ADMIN_ALERT = "admin_alert"                                      # 最高管理员安全监控弹窗


def new_trace_id() -> str:
    """生成新的调用链根 id"""
    return f"trc_{uuid.uuid4().hex[:12]}"


@dataclass
class AgentEvent:
    type: AgentEventType
    agent: str
    trace_id: str
    parent: str = ""
    message: str = ""
    payload: dict = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    # v2.4: 事件归属用户 ID,用于 SSE 按用户隔离
    # None 表示系统级事件(所有订阅者都能收到)
    user_id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value if isinstance(self.type, AgentEventType) else str(self.type)
        return d


# ====== v2.3 M7: 讨论发言数据模型 ======

@dataclass
class DiscussionTurn:
    """一轮 Agent 讨论决策或用户发言。

    `action/stance/reply_to/round_index` 均提供默认值，保证历史 WebSocket
    消息和旧调用方仍可按普通中立发言处理。
    """
    turn_id: int
    agent_code: str
    agent_name: str
    role: str          # "agent" | "user"
    content: str
    action: str = "speak"       # speak | silent
    stance: str = "neutral"     # propose | agree | oppose | question | supplement | neutral
    reply_to: Optional[str] = None
    round_index: int = 0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
