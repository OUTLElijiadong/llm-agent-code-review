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
    """一轮 Agent 讨论发言"""
    turn_id: int
    agent_code: str
    agent_name: str
    role: str          # "agent" | "user"
    content: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
