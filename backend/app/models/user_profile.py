"""
用户画像表 ORM 模型(与 user 一对一)

让 AI 更懂每个用户:
- 显式: 用户填写爱好/目标/技术栈/关注重点/经验水平/偏好语言
- 隐式: 系统从行为(采纳/忽略的问题类型、项目语言分布、论坛活跃)推断,
        写入 derived_summary / derived_stats,供个性化注入使用。
"""
from sqlalchemy import BigInteger, Boolean, Column, DateTime, String, Text

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class UserProfile(Base, IdMixin, TimestampMixin):
    __tablename__ = "user_profile"

    user_id = Column(BigInteger, nullable=False, unique=True, comment="用户ID(一对一)")

    # ── 显式画像(用户填写) ──
    hobbies = Column(Text, comment="爱好/兴趣")
    goals = Column(Text, comment="学习/工作目标")
    tech_stack = Column(Text, comment="常用技术栈(逗号分隔或自由文本)")
    focus_areas = Column(Text, comment="关注重点 JSON 数组,如 ['性能','安全']")
    preferred_language = Column(String(50), comment="偏好编程语言")
    experience_level = Column(
        String(20), comment="beginner/intermediate/advanced",
    )
    auto_learn = Column(Boolean, nullable=False, default=True, comment="是否允许隐式学习")

    # ── 隐式画像(AI/系统推断) ──
    derived_summary = Column(Text, comment="AI 综合画像摘要")
    derived_stats = Column(Text, comment="行为统计 JSON: 偏好/关注类型/语言分布等")
    last_learned_at = Column(DateTime, comment="最近一次隐式学习时间(UTC)")
