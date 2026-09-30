"""新密码策略：长度和常见密码阻止，不要求字符种类组合。"""

import re

from app.core.exceptions import BadRequestError

MIN_PASSWORD_LENGTH = 15
MAX_PASSWORD_LENGTH = 64
MAX_PASSWORD_BYTES = 72  # 当前 bcrypt 实现的安全输入上限；禁止静默截断。

_COMMON_PASSWORDS = frozenset({
    "123456789012345",
    "1234567890123456",
    "password1234567",
    "qwertyuiopasdfg",
    "qwerty123456789",
    "iloveyou1234567",
    "adminadminadmin",
    "letmeinletmein1",
    "welcome1234567",
})
_COMMON_BASES = ("password", "admin", "welcome", "qwerty", "letmein", "iloveyou", "changeme")


def _is_obvious_pattern(password: str) -> bool:
    """阻止极低熵的重复字符和常见词根加数字，不强制字符类别组合。"""
    folded = password.casefold()
    if folded and len(set(folded)) == 1:
        return True
    compact = re.sub(r"[^a-z0-9]", "", folded)
    if compact and len(set(compact)) == 1:
        return True
    if any(len(compact) >= len(base) * 2 and compact == base * (len(compact) // len(base))
           for base in _COMMON_BASES):
        return True
    return any(
        compact.startswith(base)
        and compact[len(base):].isdigit()
        and len(compact[len(base):]) >= 3
        for base in _COMMON_BASES
    )


def validate_new_password(password: str, *, username: str = "") -> None:
    """验证新密码长度、bcrypt 字节上限和小型常见密码阻止表。"""
    length = len(password)
    if length < MIN_PASSWORD_LENGTH:
        raise BadRequestError("新密码至少需要 15 个字符")
    if length > MAX_PASSWORD_LENGTH:
        raise BadRequestError("新密码最多允许 64 个字符")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise BadRequestError("新密码编码后不能超过 72 字节")

    folded = password.casefold()
    if folded in _COMMON_PASSWORDS or _is_obvious_pattern(password) or (username and folded == username.casefold()):
        raise BadRequestError("该密码过于常见或与账号相同，请换一个不易猜测的密码")
