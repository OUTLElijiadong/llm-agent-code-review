"""MySQL 全新建库脚本与 User ORM/迁移的关键字段契约。"""

import re
from pathlib import Path

from app.models.user import User

ROOT = Path(__file__).resolve().parents[3]


def test_mysql_init_password_column_matches_bcrypt_orm_and_migration() -> None:
    init_sql = (ROOT / "deploy/mysql/init.sql").read_text(encoding="utf-8")
    migration = (ROOT / "backend/alembic/versions/052_user_password_len.py").read_text(encoding="utf-8")

    assert User.__table__.c.password.type.length == 60
    assert re.search(r"\bpassword\s+VARCHAR\(60\)\s+NOT NULL", init_sql, re.IGNORECASE)
    assert not re.search(r"\bpassword\s+VARCHAR\(255\)", init_sql, re.IGNORECASE)
    assert re.search(r"_BCRYPT_HASH_LEN\s*=\s*60\b", migration)


def test_mysql_init_role_comment_lists_current_core_roles() -> None:
    init_sql = (ROOT / "deploy/mysql/init.sql").read_text(encoding="utf-8")

    assert "DEFAULT 'user' COMMENT 'user/reviewer/admin/super_admin'" in init_sql
