"""AC2 端到端验证测试样本

包含 4 个已知漏洞,用于验证 agent_label 是否正确落库:
1. SQL 注入 (CWE-89) — f-string 拼接 SQL,静态规则可命中
2. 硬编码密码 (CWE-259) — password = "admin123456",静态规则可命中
3. 命令注入 (CWE-78) — os.system(user_input),LLM 可识别
4. 路径遍历 (CWE-22) — open(user_input),静态规则可命中
"""
import os
import sqlite3

# 漏洞1:硬编码密码 (CWE-259)
# 正则规则要求前缀非字母数字下划线,password= 形式可命中
password = "admin123456"
db_url = "mysql://root:secret_pass@localhost/db"


# 漏洞2:SQL 注入 (CWE-89)
# f-string 拼接 SQL,静态规则 sql_string_concat 可命中
def get_user(username):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE username = '{username}'"
    cursor.execute(query)
    return cursor.fetchone()


# 漏洞3:命令注入 (CWE-78)
# os.system 直接拼接用户输入,LLM 可识别
def list_files(user_dir):
    os.system(f"ls {user_dir}")
    return True


# 漏洞4:路径遍历 (CWE-22)
# open 用户输入拼接路径,静态规则 path_traversal_user_input 可命中
def read_config(filename):
    with open(f"/etc/app/{filename}", "r") as f:
        return f.read()


if __name__ == "__main__":
    user = get_user("admin")
    print(user)
