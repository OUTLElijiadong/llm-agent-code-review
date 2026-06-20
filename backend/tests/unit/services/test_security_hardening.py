"""安全加固回归测试(层叠在 main 之上的独有修复)

- list_files 防御(L2):缺少 project_id 直接拒绝,不跨用户返回
- JWT 必填声明(M4):缺少 sub/exp 的令牌被拒绝
- WebSocket 会话归属(H3):register_pending 记录发起人,供连接时越权校验

注:跨用户串号(C1)是并发隔离重构,项目列表 N+1 是查询优化,均不在单测覆盖。
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.core.security import decode_token, hash_password
from app.models.user import User
from app.services import code_file_service


def _mk_user(db, username="alice", password="secret123", role="user", status=1):
    user = User(
        username=username, password=hash_password(password),
        email=f"{username}@x.com", nickname=username, role=role, status=status,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ── L2: list_files 缺少 project_id 防御 ──

def test_list_files_requires_project_id(db):
    """不带 project_id 调用必须报错,绝不能跨用户返回全库文件"""
    user = _mk_user(db, username="owner")
    with pytest.raises(ValidationError):
        code_file_service.list_files(db, user, project_id=None)


# ── M4: JWT 必填声明 ──

def test_decode_token_rejects_missing_sub():
    """签名有效但缺少 sub 的令牌应被拒绝"""
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    forged = jwt.encode(
        {"role": "admin", "exp": exp}, settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(forged)


def test_decode_token_rejects_missing_exp():
    """缺少 exp(永不过期)的令牌应被拒绝"""
    forged = jwt.encode(
        {"sub": "1", "role": "user"}, settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(forged)


# ── H3: WebSocket 会话归属登记 ──

def test_register_pending_records_owner():
    """发起讨论时应记录 session 归属,供 WS 连接做越权校验"""
    from app.api.v1 import ws_discussion

    sid = "disc_testowner"
    ws_discussion.register_pending(sid, user_id=42, profiles=(), code="", language="py")
    try:
        assert ws_discussion._session_owners.get(sid) == 42
    finally:
        ws_discussion._pending.pop(sid, None)
        ws_discussion._session_owners.pop(sid, None)
