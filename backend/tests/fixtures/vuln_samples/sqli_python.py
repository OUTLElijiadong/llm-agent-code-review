"""SQL 注入漏洞样本(CWE-89)

演示通过 f-string 字符串拼接构造 SQL 语句的危险写法。
静态规则 sql_string_concat 应命中此样本。
"""


def get_user_by_name(username: str):
    """根据用户名查询用户(存在 SQL 注入)"""
    import sqlite3
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    # 漏洞:f-string 拼接用户输入到 SQL,攻击者可注入 ' OR 1=1 --
    query = f"SELECT * FROM users WHERE username = '{username}'"
    cursor.execute(query)
    return cursor.fetchone()


def login(username: str, password: str):
    """登录校验(存在 SQL 注入)"""
    import sqlite3
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    # 漏洞:execute 直接接收 f-string
    cursor.execute(f"SELECT id FROM users WHERE name='{username}' AND pwd='{password}'")
    return cursor.fetchone()
