"""
MetaGPT 风格 Message 模型 (v2.4)

借鉴 MetaGPT 的 Message 设计,但简化为 dataclass,不引入额外依赖。
Message 用于 Environment 中 Role 间的标准通信。

字段说明:
    - role: 发送者角色(如 "code_reviewer" / "security_sentinel" / "user")
    - send_to: 目标角色(空字符串表示广播给所有 Role)
    - content: 消息内容(文本/JSON 字符串)
    - cause_by: 触发该消息的动作名(如 "ReviewCode" / "AskClarify")
    - sent_from: 源角色(冗余于 role,保留以对齐 MetaGPT 命名)
    - metadata: 额外元数据(trace_id / task_id / project_id / file_id 等)
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _new_id() -> str:
    """生成消息唯一 ID"""
    return f"msg_{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    """返回 UTC ISO8601 时间戳"""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Message:
    """MetaGPT 风格的标准消息结构

    Attributes:
        id: 消息唯一 ID(自动生成)
        role: 发送者角色 code(如 "code_reviewer" / "user" / "orchestrator")
        send_to: 目标角色 code,空字符串表示广播
        content: 消息正文(自然语言文本或 JSON 字符串)
        cause_by: 触发该消息的动作名(如 "ReviewCode" / "CrossReview" / "Consensus")
        sent_from: 源角色(冗余字段,便于跨模块读取)
        metadata: 额外元数据(trace_id / task_id / project_id / file_id / user_id 等)
        timestamp: 消息创建时间(UTC ISO8601)
    """

    id: str = field(default_factory=_new_id)
    role: str = ""
    send_to: str = ""
    content: str = ""
    cause_by: str = ""
    sent_from: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        """转为可序列化 dict

        Returns:
            Dict[str, Any]: 消息字典
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        """从 dict 构造 Message(用于反序列化)

        Args:
            data: 消息字典

        Returns:
            Message: 实例
        """
        return cls(
            id=data.get("id", _new_id()),
            role=data.get("role", ""),
            send_to=data.get("send_to", ""),
            content=data.get("content", ""),
            cause_by=data.get("cause_by", ""),
            sent_from=data.get("sent_from", data.get("role", "")),
            metadata=data.get("metadata", {}) or {},
            timestamp=data.get("timestamp", _now_iso()),
        )

    def with_metadata(self, **kwargs: Any) -> "Message":
        """链式追加 metadata 字段,返回新 Message

        Args:
            **kwargs: 要追加的 metadata 字段

        Returns:
            Message: 新实例(原实例不变)
        """
        new_meta = {**self.metadata, **kwargs}
        return Message(
            id=self.id,
            role=self.role,
            send_to=self.send_to,
            content=self.content,
            cause_by=self.cause_by,
            sent_from=self.sent_from,
            metadata=new_meta,
            timestamp=self.timestamp,
        )


def make_message(
    role: str,
    content: str,
    send_to: str = "",
    cause_by: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Message:
    """便捷构造消息

    Args:
        role: 发送者角色 code
        content: 消息正文
        send_to: 目标角色 code(空表示广播)
        cause_by: 触发动作名
        metadata: 额外元数据

    Returns:
        Message: 实例
    """
    return Message(
        role=role,
        send_to=send_to,
        content=content,
        cause_by=cause_by,
        sent_from=role,
        metadata=metadata or {},
    )
