import os
import sys

# Ensure backend folder is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.core.database import Base, engine, SessionLocal

# 必须显式导入所有 Model 才能让 Base.metadata.create_all 识别并创建对应的表
from app.models.user import User
from app.models.project import Project
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.review_rule import ReviewRule
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.review_issue import ReviewIssue
from app.models.ai_call_log import AiCallLog
from app.models.review_report import ReviewReport
from app.models.audit_log import AuditLog
# Agent 自进化 (v3.0) 新增表
from app.models.review_experience import ReviewExperience
from app.models.evolution_proposal import EvolutionProposal
from app.models.eval_case import EvalCase

def main():
    # Create tables
    print("Creating all tables in SQLite database...")
    Base.metadata.create_all(bind=engine)
    print("Tables created.")

    # Seed data
    db = SessionLocal()
    try:
        # 1. Admin user
        admin_user = db.query(User).filter(User.username == "admin").first()
        if not admin_user:
            print("Creating admin user...")
            from app.core.security import hash_password
            admin_pwd = os.environ.get("ADMIN_PASSWORD", "admin123")
            if admin_pwd == "admin123":
                print("⚠️  正在使用默认管理员口令 admin123,生产环境请设置 ADMIN_PASSWORD "
                      "环境变量,或首次登录后立即修改密码!")
            admin_user = User(
                username="admin",
                password=hash_password(admin_pwd),
                email="admin@local",
                nickname="管理员",
                role="admin",
                status=1
            )
            db.add(admin_user)
            db.commit()
            print("Admin user created.")
        else:
            print("Admin user already exists.")

        # 2. Builtin Rules
        rules_data = [
            ('code_style',       '代码规范',     'style',        '检查代码缩进、空格、命名约定是否符合语言惯例;长行、重复空白、冗余分号等。', 1, 1, 1),
            ('potential_bug',    '潜在Bug',     'correctness',  '识别空指针/未定义引用、循环边界错误、数组越界、类型混淆、错用API等可能在运行时出错的问题。',                       1, 1, 2),
            ('security',         '安全漏洞',    'security',     '识别SQL注入、命令注入、XSS、不安全反序列化、硬编码密钥、明文密码、弱加密、路径穿越等安全问题。',                  1, 1, 3),
            ('performance',      '性能问题',    'performance',  '识别低效循环、不必要的对象创建、N+1查询、大对象重复拷贝、未使用合适的数据结构等性能问题。',                        1, 1, 4),
            ('exception',        '异常处理',    'robustness',   '检查异常捕获是否过宽(裸except)、是否吞掉异常、是否缺少必要的资源释放(with/try-finally)。',                    1, 1, 5),
            ('naming',           '命名规范',    'style',        '变量、函数、类的命名是否表意、是否符合语言惯例(snake_case/camelCase/PascalCase),是否避免缩写歧义。',           1, 1, 6),
            ('maintainability',  '可维护性',    'maintainability', '函数过长、嵌套过深、参数过多、模块耦合、魔法数字、重复代码等影响维护的问题。',                                    1, 1, 7),
            ('comment',          '注释完整性',  'documentation','公共函数缺少注释/docstring、TODO未处理、关键算法无说明、注释与代码不一致等。',                                  1, 1, 8)
        ]

        for code, name, rule_type, content, enabled, is_builtin, sort_order in rules_data:
            rule = db.query(ReviewRule).filter(ReviewRule.rule_code == code).first()
            if not rule:
                print(f"Creating rule: {name}...")
                rule = ReviewRule(
                    rule_code=code,
                    rule_name=name,
                    rule_type=rule_type,
                    rule_content=content,
                    enabled=enabled,
                    is_builtin=is_builtin,
                    sort_order=sort_order
                )
                db.add(rule)
        db.commit()
        print("Rules seeded successfully.")
    finally:
        db.close()

if __name__ == "__main__":
    main()
