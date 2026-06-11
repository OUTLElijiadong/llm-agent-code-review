"""
Alembic环境配置
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.core.database import Base

from app.models.user import User
from app.models.project import Project
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.review_rule import ReviewRule
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.review_issue import ReviewIssue
from app.models.ai_call_log import AiCallLog
from app.models.review_report import ReviewReport
from app.models.audit_log import AuditLog
from app.models.api_config import UserApiConfig  # v3.1

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.db_url)

target_metadata = Base.metadata


def run_migrations_offline():
    """离线模式: 生成SQL脚本而非直接连接数据库"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """在线模式: 连接数据库执行迁移"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
