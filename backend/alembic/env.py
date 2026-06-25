"""
Alembic环境配置
"""
from importlib import import_module
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import settings
from app.core.database import Base

_MODEL_MODULES = (
    "app.models.agent_governance",
    "app.models.ai_call_log",
    "app.models.api_config",
    "app.models.audit_log",
    "app.models.code_file",
    "app.models.code_version",
    "app.models.project",
    "app.models.review_issue",
    "app.models.review_report",
    "app.models.review_rule",
    "app.models.review_task",
    "app.models.review_task_file",
    "app.models.user",
)

for module_name in _MODEL_MODULES:
    import_module(module_name)

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
