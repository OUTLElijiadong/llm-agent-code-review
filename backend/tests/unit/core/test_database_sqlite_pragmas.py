import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.core.database import configure_sqlite_connection_pragmas


def test_application_sqlite_connections_enable_recursive_triggers():
    engine = create_engine("sqlite://")
    configure_sqlite_connection_pragmas(engine)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA recursive_triggers")).scalar_one() == 1
            connection.execute(text("CREATE TABLE protected (id INTEGER PRIMARY KEY, value TEXT)"))
            connection.execute(text("INSERT INTO protected VALUES (1, 'original')"))
            connection.commit()
            connection.execute(text(
                "CREATE TRIGGER prevent_protected_delete BEFORE DELETE ON protected "
                "BEGIN SELECT RAISE(ABORT, 'protected row'); END"
            ))
            with pytest.raises(IntegrityError, match="protected row"):
                connection.execute(text("INSERT OR REPLACE INTO protected VALUES (1, 'replacement')"))
            connection.rollback()
            assert connection.execute(text("SELECT value FROM protected WHERE id=1")).scalar_one() == "original"
    finally:
        engine.dispose()
