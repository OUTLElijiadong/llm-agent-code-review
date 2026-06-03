"""仅用于全量回归测试的合成代码文件。

文件包含刻意构造的安全问题，用于验证 DeepSeek 审查、安全扫描和删除闭环。
禁止将其中写法用于生产代码。
"""

import hashlib


QA_ONLY_TOKEN = "sk-test-prism-qa-fixture-not-a-real-key"


def build_user_query(user_id: str) -> str:
    """构造仅用于触发 SQL 注入检查的测试查询。

    Args:
        user_id: 合成用户标识。

    Returns:
        带有刻意拼接问题的测试查询。
    """
    return f"SELECT * FROM users WHERE id = '{user_id}'"


def weak_password_digest(password: str) -> str:
    """计算仅用于触发弱哈希检查的测试摘要。

    Args:
        password: 合成密码文本。

    Returns:
        使用 MD5 生成的测试摘要。
    """
    return hashlib.md5(password.encode("utf-8")).hexdigest()
