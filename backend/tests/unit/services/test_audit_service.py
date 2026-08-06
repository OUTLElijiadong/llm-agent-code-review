"""单元测试: 操作审计服务

只验证 `log(...)` 的行为契约:
- 正常路径下会写入一条 AuditLog
- DB 异常不会传出去 (审计失败不能影响主业务)
- actor=None 时 actor_id / actor_name 落 NULL
"""
from app.services import audit_service


class _FakeDb:
    """伪造的 SQLAlchemy Session,记录 add / commit / rollback 调用"""

    def __init__(self, raise_on_commit: bool = False) -> None:
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0
        self._raise = raise_on_commit

    def add(self, entry) -> None:
        self.added.append(entry)

    def commit(self) -> None:
        if self._raise:
            raise RuntimeError("simulated DB failure")
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _FakeUser:
    def __init__(self, uid: int, username: str) -> None:
        self.id = uid
        self.username = username


def test_log_writes_audit_entry_with_actor():
    """带 actor 的正常调用应落库一条,字段对齐传参"""
    db = _FakeDb()
    user = _FakeUser(42, "li")

    audit_service.log(
        db, user, "user",
        target_type="user", target_id=99,
        detail="重置密码", ip="1.2.3.4",
    )

    assert len(db.added) == 1
    entry = db.added[0]
    assert entry.actor_id == 42
    assert entry.actor_name == "li"
    assert entry.action == "user"
    assert entry.target_type == "user"
    assert entry.target_id == "99"  # 字符串化兼容非整型主键
    assert entry.detail == "重置密码"
    assert entry.ip == "1.2.3.4"
    assert entry.status == "success"
    assert db.commits == 1


def test_log_anonymous_actor_falls_back_to_null():
    """actor=None (系统操作或登录失败) 时 actor_id / actor_name 必须为 None"""
    db = _FakeDb()

    audit_service.log(db, None, "login", target_id="ghost", detail="登录失败", status="failed")

    entry = db.added[0]
    assert entry.actor_id is None
    assert entry.actor_name is None
    assert entry.status == "failed"


def test_log_swallows_db_errors():
    """审计失败不能让主业务挂掉"""
    db = _FakeDb(raise_on_commit=True)
    user = _FakeUser(1, "x")

    # 不应抛异常
    audit_service.log(db, user, "user", detail="ok")

    assert db.rollbacks == 1
