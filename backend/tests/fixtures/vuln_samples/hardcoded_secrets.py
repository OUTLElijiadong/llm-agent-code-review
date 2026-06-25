"""硬编码密钥漏洞样本(CWE-798)

演示多种硬编码凭据:AWS Key、数据库密码、连接串。
正则秘钥扫描应命中此样本(至少 3 处)。
"""

# 漏洞:硬编码 AWS Access Key
AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"

# 漏洞:硬编码数据库密码
DB_PASSWORD = "SuperSecret123!"

# 漏洞:含明文账号密码的数据库连接串
DATABASE_URL = "postgresql://admin:admin123@10.0.0.1:5432/prod_db"

# 漏洞:硬编码通用 API Key
API_KEY = "sk-1234567890abcdef1234567890abcdef"


def connect_db():
    """使用硬编码凭据连接数据库"""
    import psycopg2
    return psycopg2.connect(
        host="10.0.0.1",
        database="prod_db",
        user="admin",
        password=DB_PASSWORD,
    )
