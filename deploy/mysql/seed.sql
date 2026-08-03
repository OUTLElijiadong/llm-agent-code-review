-- 种子数据: 管理员账号 + 内置审查规则
-- admin 的占位密码无法登录；022 迁移仅会从私有环境变量初始化新库。
SET NAMES utf8mb4;

-- 管理员账号
INSERT IGNORE INTO user (username, password, email, nickname, role, status)
VALUES ('admin', '!INITIAL_ADMIN_PASSWORD_REQUIRED!', 'admin@local', '管理员', 'admin', 1);

-- 8条内置审查规则
INSERT IGNORE INTO review_rule (user_id, rule_code, rule_name, rule_type, rule_content, enabled, is_builtin, sort_order) VALUES
(NULL, 'code_style',       '代码规范',     'style',        '检查代码缩进、空格、命名约定是否符合语言惯例;长行、重复空白、冗余分号等。', 1, 1, 1),
(NULL, 'potential_bug',    '潜在Bug',     'correctness',  '识别空指针/未定义引用、循环边界错误、数组越界、类型混淆、错用API等可能在运行时出错的问题。',                       1, 1, 2),
(NULL, 'security',         '安全漏洞',    'security',     '识别SQL注入、命令注入、XSS、不安全反序列化、硬编码密钥、明文密码、弱加密、路径穿越等安全问题。',                  1, 1, 3),
(NULL, 'performance',      '性能问题',    'performance',  '识别低效循环、不必要的对象创建、N+1查询、大对象重复拷贝、未使用合适的数据结构等性能问题。',                        1, 1, 4),
(NULL, 'exception',        '异常处理',    'robustness',   '检查异常捕获是否过宽(裸except)、是否吞掉异常、是否缺少必要的资源释放(with/try-finally)。',                    1, 1, 5),
(NULL, 'naming',           '命名规范',    'style',        '变量、函数、类的命名是否表意、是否符合语言惯例(snake_case/camelCase/PascalCase),是否避免缩写歧义。',           1, 1, 6),
(NULL, 'maintainability',  '可维护性',    'maintainability', '函数过长、嵌套过深、参数过多、模块耦合、魔法数字、重复代码等影响维护的问题。',                                    1, 1, 7),
(NULL, 'comment',          '注释完整性',  'documentation','公共函数缺少注释/docstring、TODO未处理、关键算法无说明、注释与代码不一致等。',                                  1, 1, 8);
