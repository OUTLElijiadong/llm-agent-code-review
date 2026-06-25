"""测试公共 fixture

提供一个隔离的内存 SQLite 会话:每个测试函数全新建表,独立于 .env 配置的
MySQL,因此无需外部依赖即可端到端验证自进化的模型与服务逻辑。
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.agent_governance import (  # noqa: F401,E402
    AgentAlert,
    AgentArtifactVersion,
    AgentJob,
    AgentJobRun,
    AgentKnowledgeChunk,
    AgentKnowledgeDoc,
    AgentKnowledgeSource,
    AgentMemory,
    AgentMetricSnapshot,
    AgentProfile,
    AgentReflection,
    AgentRewardEvent,
    AgentSkillBinding,
    AgentToolPermission,
    ApprovalItem,
    PolicyDecisionLog,
    PolicyRule,
    ToolCallLog,
)
from app.models.ai_call_log import AiCallLog  # noqa: F401,E402
from app.models.api_config import UserApiConfig  # noqa: F401,E402  -- v3.1
from app.models.audit_log import AuditLog  # noqa: F401,E402
from app.models.code_file import CodeFile  # noqa: F401,E402
from app.models.code_version import CodeVersion  # noqa: F401,E402
from app.models.eval_case import EvalCase  # noqa: F401,E402
from app.models.evolution_proposal import EvolutionProposal  # noqa: F401,E402
from app.models.project import Project  # noqa: F401,E402
from app.models.project_member import ProjectMember  # noqa: F401,E402  -- v2.4
from app.models.review_experience import ReviewExperience  # noqa: F401,E402
from app.models.review_issue import ReviewIssue  # noqa: F401,E402
from app.models.review_report import ReviewReport  # noqa: F401,E402
from app.models.review_rule import ReviewRule  # noqa: F401,E402
from app.models.review_task import ReviewTask  # noqa: F401,E402
from app.models.review_task_file import ReviewTaskFile  # noqa: F401,E402

# 显式导入全部 ORM 模型,确保 Base.metadata 注册所有表
from app.models.user import User  # noqa: F401,E402


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ── 工厂助手(以 fixture 暴露 callable,避免跨 conftest 导入) ──

@pytest.fixture
def mk_issue():
    def _f(db, *, issue_type="安全漏洞", status="fixed", task_id=1,
           handled_by=1, title="问题", file_id=None,
           suggestion="", fixed_code="", handled_at=None, severity="高"):
        issue = ReviewIssue(
            task_id=task_id, file_id=file_id, file_name="x.py",
            issue_type=issue_type, severity=severity, title=title,
            description="desc", suggestion=suggestion, fixed_code=fixed_code,
            status=status, handled_by=handled_by,
            handled_at=handled_at or datetime.now(timezone.utc),
        )
        db.add(issue)
        db.commit()
        return issue
    return _f


@pytest.fixture
def mk_rule():
    def _f(db, *, rule_code="security", rule_name="安全漏洞",
           rule_type="security", severity="高", enabled=1, is_builtin=1,
           language="*", sort_order=1):
        rule = ReviewRule(
            rule_code=rule_code, rule_name=rule_name, rule_type=rule_type,
            rule_content="检查", severity=severity, enabled=enabled,
            is_builtin=is_builtin, language=language, sort_order=sort_order,
        )
        db.add(rule)
        db.commit()
        return rule
    return _f


@pytest.fixture
def admin_user(db):
    user = User(username="admin", password="x", email="a@b.c",
                nickname="管理员", role="admin", status=1)
    db.add(user)
    db.commit()
    return user
