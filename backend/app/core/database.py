"""
数据库引擎与Session管理模块
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings


def configure_sqlite_connection_pragmas(engine):
    """Apply connection-local SQLite protections used by the application."""
    if engine.dialect.name != "sqlite" or getattr(engine, "_prism_sqlite_pragmas_configured", False):
        return

    @event.listens_for(engine, "connect")
    def _enable_recursive_triggers(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA recursive_triggers=ON")
        finally:
            cursor.close()

    engine._prism_sqlite_pragmas_configured = True

engine_kwargs = {"pool_pre_ping": True}
if "sqlite" in settings.db_url:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    engine_kwargs.update({
        "pool_recycle": 300,
        "pool_size": 10,
        "max_overflow": 20,
        "connect_args": {
            "connect_timeout": 10,
            "read_timeout": 30,
            "write_timeout": 30,
        },
    })

engine = create_engine(
    settings.db_url,
    **engine_kwargs
)
configure_sqlite_connection_pragmas(engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI依赖注入: 获取数据库会话并确保使用后关闭"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
