"""
操作审计日志表 ORM 模型

用于管理员审计视图(F-A3),记录登录、用户管理、规则变更、Agent 启用/禁用等关键操作。
表与 ai_call_log 分离以避免污染调用日志,并支持非 LLM 触发的操作流水。
"""
from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, String, Text

from app.core.database import Base
from app.models.base import IdMixin


class AuditLog(Base, IdMixin):
    __tablename__ = "audit_log"

    actor_id = Column(BigInteger, comment="操作者用户ID;系统操作可为 NULL")
    actor_name = Column(String(80), comment="操作者用户名快照,便于用户删除后仍可读")
    action = Column(String(40), nullable=False, comment="操作类型: login/user/rule/ai/project/agent")
    target_type = Column(String(40), comment="对象类型: user/project/rule/agent/...")
    target_id = Column(String(80), comment="对象ID或键,支持字符串以适配非自增主键")
    detail = Column(Text, comment="操作说明,自由文本")
    status = Column(String(20), nullable=False, default="success", comment="success/failed")
    ip = Column(String(64), comment="请求来源 IP")
    create_time = Column(DateTime, nullable=False, default=datetime.utcnow, comment="发生时间")
