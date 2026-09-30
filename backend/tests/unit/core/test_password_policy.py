"""密码策略边界与当前 bcrypt 输入上限回归。"""

import pytest

from app.core.exceptions import BadRequestError
from app.core.password_policy import validate_new_password
from app.core.security import hash_password, verify_password


def test_long_passphrase_without_character_composition_is_accepted() -> None:
    phrase = "correct horse battery staple"
    validate_new_password(phrase, username="member-7")
    hashed = hash_password(phrase)
    assert verify_password(phrase, hashed)


@pytest.mark.parametrize(
    ("password", "username", "message"),
    [
        ("123456", "member-7", "至少需要 15"),
        ("123456789012345", "member-7", "过于常见"),
        ("aaaaaaaaaaaaaaa", "member-7", "过于常见"),
        ("界" * 15, "member-7", "过于常见"),
        ("admin1234567890", "member-7", "过于常见"),
        ("passwordpassword", "member-7", "过于常见"),
        ("member-7", "member-7", "至少需要 15"),
        ("long-enough-user-name", "long-enough-user-name", "与账号相同"),
        ("中" * 25, "member-7", "72 字节"),
        ("x" * 65, "member-7", "最多允许 64"),
    ],
)
def test_rejects_short_common_contextual_and_out_of_range_passwords(password, username, message) -> None:
    with pytest.raises(BadRequestError, match=message):
        validate_new_password(password, username=username)


def test_password_hashing_rejects_bcrypt_byte_truncation_and_verification_fails_closed() -> None:
    too_many_bytes = "界" * 25
    with pytest.raises(ValueError, match="72 字节"):
        hash_password(too_many_bytes)
    assert verify_password(too_many_bytes, "not-a-hash") is False
