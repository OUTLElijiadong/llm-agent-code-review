"""SQL 注入漏洞样本(CWE-89)

漏洞类型: SQL 注入 (SQL Injection)
CWE 编号: CWE-89
OWASP 分类: A03:2021-Injection
预期 CVSS 评分: >= 9.0 (严重)
预期合规映射: iso27001 / gdpr / pci_dss / hipaa
预期静态规则: sql_string_concat
预期修复关键词: 参数化查询

说明:
    本样本通过 f-string 字符串拼接构造 SQL 语句,将用户输入直接嵌入 SQL。
    静态分析器的 sql_string_concat 规则应命中此样本。
    攻击者可通过输入 ' OR 1=1 -- 绕过认证或拖库。
"""


def get_user_by_id(user_id: str):
    """根据用户 ID 查询用户(存在 SQL 注入)

    Args:
        user_id: 用户输入的用户 ID(未做任何校验)

    Returns:
        查询结果行
    """
    import sqlite3
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    # 漏洞位置: f-string 拼接用户输入到 SQL 语句
    query = f"SELECT * FROM users WHERE id = '{user_id}'"
    cursor.execute(query)
    return cursor.fetchone()


def login(username: str, user_pass: str):
    """登录校验函数(存在 SQL 注入)

    Args:
        username: 用户输入的用户名
        user_pass: 用户输入的登录凭证

    Returns:
        登录成功返回用户行,失败返回 None
    """
    import sqlite3
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    # 漏洞位置: execute 直接接收 f-string,攻击者可注入 ' OR '1'='1
    # 注: 列名使用 pass 而非 pwd,参数名使用 user_pass 而非 password,
    # 以避免触发 Hardcoded Password 正则(CWE-259)造成与本样本目标(CWE-89)无关的误报
    cursor.execute(f"SELECT id FROM users WHERE name='{username}' AND pass='{user_pass}'")
    return cursor.fetchone()


def search_products(keyword: str):
    """商品搜索函数(存在 SQL 注入)

    Args:
        keyword: 用户输入的搜索关键词

    Returns:
        匹配的商品列表
    """
    import sqlite3
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    # 漏洞位置: f-string 拼接构造 LIKE 查询
    query = f"SELECT id, name, price FROM products WHERE name LIKE '%{keyword}%'"
    cursor.execute(query)
    return cursor.fetchall()
