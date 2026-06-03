#!/usr/bin/env python3
"""Agent 自进化 · 黄金回归集种子数据

这些是人工锚点用例(每条代码都含确定可检出的真问题),作为评估闸门的基准
与防回声室的固定锚点。进化提案 promote 前必须在这些用例上不退化召回。

DB 无关:走 ORM + SessionLocal,SQLite/MySQL 通用;按 name 幂等。
用法:  python seed_eval_cases.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.models.eval_case import EvalCase  # noqa: E402

# (name, language, code, expected_issues, tags)
CASES = [
    (
        "SQL注入-字符串拼接",
        "python",
        'def get_user(conn, name):\n'
        '    sql = "SELECT * FROM users WHERE name = \'" + name + "\'"\n'
        '    return conn.execute(sql).fetchall()\n',
        [{"issue_type": "安全漏洞", "keyword": "注入"}],
        "security",
    ),
    (
        "硬编码密钥",
        "python",
        'import requests\n'
        'API_KEY = "sk-1234567890abcdef1234567890abcdef"\n'
        'def call():\n'
        '    return requests.get("https://api.example.com", headers={"Authorization": API_KEY})\n',
        [{"issue_type": "安全漏洞", "keyword": ""}],
        "security",
    ),
    (
        "裸except吞异常",
        "python",
        'def load(path):\n'
        '    try:\n'
        '        with open(path) as f:\n'
        '            return f.read()\n'
        '    except:\n'
        '        pass\n',
        [{"issue_type": "异常处理", "keyword": ""}],
        "robustness",
    ),
    (
        "可变默认参数",
        "python",
        'def append_item(item, bucket=[]):\n'
        '    bucket.append(item)\n'
        '    return bucket\n',
        [{"issue_type": "潜在Bug", "keyword": ""}],
        "correctness",
    ),
    (
        "循环内查询N+1",
        "python",
        'def load_orders(db, user_ids):\n'
        '    result = []\n'
        '    for uid in user_ids:\n'
        '        orders = db.query("SELECT * FROM orders WHERE user_id = %s", uid)\n'
        '        result.append(orders)\n'
        '    return result\n',
        [{"issue_type": "性能问题", "keyword": ""}],
        "performance",
    ),
    (
        "命令注入-shell拼接",
        "python",
        'import os\n'
        'def ping(host):\n'
        '    os.system("ping -c 1 " + host)\n',
        [{"issue_type": "安全漏洞", "keyword": ""}],
        "security",
    ),
]


def main():
    print("Ensuring evolution tables exist...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    created = 0
    try:
        for name, language, code, expected, tags in CASES:
            if db.query(EvalCase.id).filter(EvalCase.name == name).first():
                print(f"  skip (exists): {name}")
                continue
            db.add(EvalCase(
                name=name, language=language, code=code,
                expected_issues=json.dumps(expected, ensure_ascii=False),
                tags=tags, enabled=1, source="seed",
            ))
            created += 1
            print(f"  + {name}")
        db.commit()
        print(f"Done. {created} eval cases seeded, "
              f"{db.query(EvalCase).count()} total.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
