-- ============================================================
-- Prism 审查规则种子数据 v2.2
-- 128 条规则: 通用 28 + Python 22 + Java 18 + JavaScript/TS 20 +
--            Go 14 + SQL 12 + 文档 14
-- ============================================================

-- 清理旧内置规则
DELETE FROM review_rule WHERE is_builtin = 1;

-- ================== 通用规则 (28条, language=*) ==================

INSERT INTO review_rule (user_id, rule_code, rule_name, rule_type, rule_content, language, severity, enabled, is_builtin, sort_order) VALUES
-- 安全
(NULL, 'sec_sqli',         'SQL注入防护',              'security',       '检查是否存在SQL语句字符串拼接,要求使用参数化查询或ORM的安全API。动态表名/列名必须使用白名单校验。', '*', '严重', 1, 1, 1),
(NULL, 'sec_xss',          'XSS跨站脚本防护',            'security',       '检查输出到HTML/JS/URL上下文时是否正确转义。HTML输出使用实体编码,JS使用\\xHH编码,URL使用encodeURIComponent。前端框架使用v-text而非v-html。', '*', '严重', 1, 1, 2),
(NULL, 'sec_injection',    '命令注入防护',             'security',       '检查是否对用户输入执行了系统命令(os.system/subprocess/os.exec/exec)。必须使用数组形式传参并对每个参数进行白名单校验。', '*', '严重', 1, 1, 3),
(NULL, 'sec_auth',         '认证与授权校验',            'security',       '检查敏感接口是否缺少身份认证,角色/权限校验是否在服务端执行。JWT需要验证签名+过期时间,禁止硬编码密钥。', '*', '严重', 1, 1, 4),
(NULL, 'sec_secret',       '密钥与凭证泄露',            'security',       '检查代码中是否硬编码了密码/API Key/Token/私钥/AccessKey等敏感凭证。密钥必须从环境变量或配置中心读取,不在日志中打印。', '*', '严重', 1, 1, 5),
(NULL, 'sec_deserialize',  '不安全反序列化',            'security',       '检查是否使用了pickle.loads/yaml.load/ooxml等不安全反序列化。Python使用yaml.safe_load,Java避免ObjectInputStream接受外部数据。', '*', '高', 1, 1, 6),
(NULL, 'sec_path',         '路径遍历防护',             'security',       '检查文件操作中是否直接拼接用户输入作为路径,可能导致任意文件读取。使用os.path.realpath校验规范化路径在允许目录内。', '*', '高', 1, 1, 7),
(NULL, 'sec_ssl',          'HTTPS与传输安全',            'security',       '检查API调用是否强制HTTPS,SSL证书验证是否关闭(verify=False)。生产环境禁止关闭证书验证。敏感数据传输必须加密。', '*', '高', 1, 1, 8),

-- 潜在Bug
(NULL, 'bug_null',         '空值/None引用检查',          'correctness',    '检查访问对象属性/调用方法前是否判空。Optional类型的字段在使用前必须验证is not None。使用Optional Chaining(?.)或空值合并运算符(??)。', '*', '高', 1, 1, 10),
(NULL, 'bug_boundary',     '边界条件与越界检查',          'correctness',    '检查循环/数组/列表访问是否存在off-by-one错误。空集合/空字符串/零值的边界情况是否处理。索引是否为负或超界。', '*', '高', 1, 1, 11),
(NULL, 'bug_type',         '类型转换与精度丢失',          'correctness',    '检查隐式类型转换是否可能导致精度丢失(int→float,long→int)。数值溢出检查。时间戳精度(秒/毫秒)一致性。', '*', '高', 1, 1, 12),
(NULL, 'bug_regex',        '正则表达式陷阱',            'correctness',    '检查正则是否存在ReDoS风险(嵌套量词/回溯爆炸)。正则是否使用了^和$边界(Catastrophic Backtracking)。email/URL正则是否过于宽松。', '*', '中', 1, 1, 13),
(NULL, 'bug_dead',         '死代码与不可达分支',          'correctness',    '检查是否存在永远不会执行的分支(if False/条件互斥)、永远不会抛出的except、永远为空的循环体、return后的代码。', '*', '低', 1, 1, 14),

-- 性能
(NULL, 'perf_n1',          'N+1查询问题',             'performance',     '检查循环内是否执行了数据库查询或API调用,导致N+1问题。使用批量查询(eager loading/join/batch API)一次性获取关联数据。ORM中使用selectinload或joinedload。', '*', '高', 1, 1, 16),
(NULL, 'perf_cache',       '缺少缓存导致重复计算',        'performance',     '检查计算密集型/IO密集型结果是否被缓存。频繁调用的API结果应设置合理的缓存策略(内存cache/Redis/CDN)。避免在循环内重复执行不变的查询。', '*', '中', 1, 1, 17),
(NULL, 'perf_loop',        '低效循环与算法复杂度',         'performance',     '检查嵌套循环O(n²)是否可优化为O(n)。字典/Set查找替代列表线性搜索。使用列表推导式/生成器替代显式循环累加。排序前检查数据是否已有序。', '*', '中', 1, 1, 18),

-- 异常处理
(NULL, 'exc_bare',         '裸except/吞异常',           'robustness',     '检查是否使用了裸except:或except Exception后pass,导致异常被完全吞掉。至少应记录日志。明确捕获预期的异常类型,未预期的异常应向上传播或告警。', '*', '高', 1, 1, 20),
(NULL, 'exc_finally',      '资源未正确释放',            'robustness',     '检查文件/数据库连接/网络套接字/锁等资源是否在finally中或使用with/try-with-resources确保释放。Python使用with语句,Java使用try-with-resources。', '*', '高', 1, 1, 21),
(NULL, 'exc_retry',        '临时故障缺少重试机制',         'robustness',     '检查网络请求/数据库操作等可能临时失败的操作是否添加了重试。重试应带指数退避,避免雪崩。幂等操作可安全重试,非幂等操作需确认。', '*', '中', 1, 1, 22),
(NULL, 'exc_msg',          '异常信息泄露',             'robustness',     '检查异常信息是否直接返回给前端/API响应,可能泄露堆栈/路径/数据库结构。用户侧返回通用错误信息,详细错误仅记录服务端日志。', '*', '中', 1, 1, 23),

-- 可维护性
(NULL, 'mnt_func_len',     '函数/方法过长',             'maintainability','检查单个函数/方法是否超过50行(不含空行和注释)。长函数应拆分为职责单一的小函数。每个函数只做一件事,提升可读性和可测试性。', '*', '中', 1, 1, 25),
(NULL, 'mnt_duplicate',    '代码重复(DRY原则)',           'maintainability','检查是否存在明显的代码复制粘贴。重复逻辑应抽取为公共函数/工具类/混入(Mixin)。相似结构的代码考虑使用配置驱动。', '*', '中', 1, 1, 26),
(NULL, 'mnt_magic',        '魔法数字与硬编码',           'maintainability','检查代码中是否直接出现无注释的数字字面量(如60/86400/404)或字符串(如URL/文件路径)。应提取为命名常量或配置项。', '*', '中', 1, 1, 27),
(NULL, 'mnt_dep',          '过深嵌套与复杂度',           'maintainability','检查是否存在超过4层的条件嵌套(if/for/while)。使用卫语句(Guard Clause)提前return减少缩进。表达式中循环嵌套不超过2层。', '*', '低', 1, 1, 28);

-- ================== Python (22条, language=python) ==================

INSERT INTO review_rule (user_id, rule_code, rule_name, rule_type, rule_content, language, severity, enabled, is_builtin, sort_order) VALUES
(NULL, 'py_list_mutate',   '遍历时修改集合',            'correctness',    '检查是否在遍历列表/字典时增删元素,会导致RuntimeError或跳过元素。使用列表推导式生成新列表,或遍历list(container)的副本。', 'python', '高', 1, 1, 30),
(NULL, 'py_default_arg',   '可变默认参数陷阱',           'correctness',    '检查函数默认参数是否使用了可变对象(def f(x=[])/ def f(d={})),每次调用共享同一对象会导致意外行为。默认参数使用None,函数体内判断is None再初始化。', 'python', '高', 1, 1, 31),
(NULL, 'py_late_binding',  '闭包延迟绑定',             'correctness',    '检查循环中创建的lambda/闭包是否捕获了循环变量,导致所有闭包共享最终值。使用functools.partial或默认参数(i=i)冻结当前值。', 'python', '中', 1, 1, 32),
(NULL, 'py_global',        '全局变量滥用',             'maintainability','检查是否过度使用global/nonlocal关键字修改外部变量。优先使用返回值传递状态,或封装为类属性。模块级全局变量应考虑线程安全。', 'python', '中', 1, 1, 33),
(NULL, 'py_type_hint',     '缺少类型注解',             'style',          '检查公开函数/方法是否缺少类型注解(参数和返回值)。类型注解显著提升IDE提示和代码审查效率。推荐使用typing模块的List/Dict/Optional等泛型。', 'python', '低', 1, 1, 34),
(NULL, 'py_comprehension', '列表推导式滥用导致可读性差',    'style',          '检查列表/字典推导式是否超过2个for或包含复杂条件,导致可读性下降。复杂的推导式改用传统for循环,或拆分为多步中间变量。', 'python', '低', 1, 1, 35),
(NULL, 'py_context_mgr',   '未使用上下文管理器',          'robustness',     '检查文件/锁/数据库连接等是否未使用with语句,导致资源泄漏。自定义资源类应实现__enter__/__exit__以支持with。', 'python', '高', 1, 1, 36),
(NULL, 'py_import_star',   '星号导入污染命名空间',        'style',          '检查是否使用了 from module import *,会污染当前命名空间且难以追踪来源。显式导入需要的名称,或使用import module as m的别名方式。', 'python', '中', 1, 1, 37),
(NULL, 'py_thread_safety', '线程安全问题',             'correctness',    '检查多线程代码中的共享状态访问是否加锁。使用threading.Lock保护临界区,或使用queue.Queue在线程间安全传递数据。全局变量/类变量在多线程下需加锁。', 'python', '高', 1, 1, 38),
(NULL, 'py_async_await',   'async/await误用',          'correctness',    '检查async函数中是否错误调用了同步阻塞方法(time.sleep/requests.get),导致事件循环阻塞。使用asyncio.sleep和aiohttp。async函数中的CPU密集操作应使用run_in_executor。', 'python', '高', 1, 1, 39),
(NULL, 'py_fstring_log',   'f-string日志格式化',         'performance',    '检查日志语句中是否使用了f-string(f"{var}"),即使日志级别不匹配仍会执行格式化。使用%s占位符延迟格式化: logger.debug("%s", var)。', 'python', '中', 1, 1, 40),
(NULL, 'py_slots',         '未使用__slots__优化内存',     'performance',    '检查数量大量实例化的类是否定义了__slots__,可节省约40-50%内存。对于数据容器类(dataclass/简单DTO)强烈建议添加__slots__。', 'python', '低', 1, 1, 41),
(NULL, 'py_generator',     '大列表未使用生成器',          'performance',    '检查是否对大数据集调用list()/返回完整列表,可能导致内存溢出。使用生成器(yield)或迭代器延迟计算。range替代xrange(Python3已统一),map/filter返回迭代器。', 'python', '中', 1, 1, 42),
(NULL, 'py_except_spec',   '捕获过于宽泛的异常',          'robustness',     '检查except Exception是否过于宽泛,可能意外捕获SystemExit/KeyboardInterrupt。至少区分Exception和BaseException。预计的异常明确类型如ValueError/KeyError。', 'python', '中', 1, 1, 43),
(NULL, 'py_mutable_class', '类属性可变对象共享',          'correctness',    '检查类属性(非实例属性)是否使用了可变默认值(列表/字典),所有实例共享该对象。应在__init__中为每个实例单独初始化。', 'python', '高', 1, 1, 44),
(NULL, 'py_sql_inject',    'Python SQL注入(字符串拼接)',    'security',       '检查cursor.execute(f"SELECT ... WHERE name={user_input}")等字符串拼接。必须使用参数化查询:cursor.execute("... WHERE name=%s", (name,))。使用SQLAlchemy的ORM或参数绑定。', 'python', '严重', 1, 1, 45),
(NULL, 'py_pickle',        'pickle反序列化风险',         'security',       '检查是否对不可信数据使用pickle.loads/unpickle。pickle可执行任意代码,绝不用于网络数据/用户上传。使用json/msgpack/protobuf等安全序列化格式。', 'python', '严重', 1, 1, 46),
(NULL, 'py_eval_exec',     'eval/exec动态执行风险',        'security',       '检查是否使用eval()/exec()/compile()执行用户可控字符串。eval可执行任意代码,极度危险。必须使用时需要严格的AST白名单过滤,优先使用ast.literal_eval。', 'python', '严重', 1, 1, 47),
(NULL, 'py_subprocess',    'subprocess注入风险',         'security',       '检查subprocess调用是否使用了shell=True且拼接用户输入。必须使用shell=False并传参列表: subprocess.run(["cmd", arg], shell=False)。禁止拼接用户输入到命令字符串。', 'python', '严重', 1, 1, 48),
(NULL, 'py_venv',          '依赖管理缺失(requirements.txt)', 'maintainability','检查项目根目录是否缺少requirements.txt/Pipfile/pyproject.toml。依赖应锁定版本号,区分生产与开发依赖。建议使用虚拟环境隔离。', 'python', '中', 1, 1, 49),
(NULL, 'py_logging',       '使用print替代logging',        'style',          '检查是否使用print()输出调试信息而非logging模块。日志应使用logging并设置合适的level(DEBUG/INFO/WARNING/ERROR)。避免在循环中打印大量信息。', 'python', '低', 1, 1, 50),
(NULL, 'py_main_guard',    '缺少if __name__ == "__main__"保护', 'style',     '检查脚本文件中顶层代码是否未用if __name__ == "__main__":保护,导致被import时也会执行。将执行逻辑放入main()函数,在保护块中调用。', 'python', '低', 1, 1, 51);

-- ================== Java (18条, language=java) ==================

INSERT INTO review_rule (user_id, rule_code, rule_name, rule_type, rule_content, language, severity, enabled, is_builtin, sort_order) VALUES
(NULL, 'java_null',        'NullPointerException风险',    'correctness',    '检查方法返回值/参数是否可能为null而未检查。使用Optional<T>替代null返回值,使用@Nullable/@NonNull注解明确约定。Objects.requireNonNull进行参数校验。', 'java', '高', 1, 1, 80),
(NULL, 'java_stream',      'Stream API 误用',           'performance',    '检查Stream是否在关闭后重用,或并行Stream使用非线程安全集合。collect(Collectors.toList())优于forEach手动add。大集合避免频繁boxing/unboxing。', 'java', '中', 1, 1, 81),
(NULL, 'java_resource',    '资源未使用try-with-resources',  'robustness',     '检查InputStream/OutputStream/Connection/Statement/ResultSet等是否未使用try-with-resources自动关闭。Java 7+的AutoCloseable实现类必须用此语法防止资源泄漏。', 'java', '高', 1, 1, 82),
(NULL, 'java_equals',      '==与equals混用',            'correctness',    '检查对象比较是否误用==而非equals。==比较引用地址,equals比较值。字符串/包装类型必须使用equals。枚举常量可以使用==。Objects.equals(a,b)安全处理null。', 'java', '高', 1, 1, 83),
(NULL, 'java_cme',         '遍历中修改集合(ConcurrentModification)', 'correctness', '检查for-each循环中是否对集合进行add/remove操作,会抛出ConcurrentModificationException。使用Iterator.remove()或CopyOnWriteArrayList或Stream.filter收集。', 'java', '高', 1, 1, 84),
(NULL, 'java_serial',      'Serializable版本管理缺失',     'maintainability','检查实现Serializable的类是否定义了private static final long serialVersionUID字段。未定义会在类结构变化时导致InvalidClassException,无法反序列化旧数据。', 'java', '中', 1, 1, 85),
(NULL, 'java_lombok',      'Lombok误用风险',            'correctness',    '检查@Entity类是否使用@Data导致生成equals/hashCode包含所有字段,可能造成JPA代理对象比较异常。JPA Entity使用@Getter/@Setter而非@Data。', 'java', '中', 1, 1, 86),
(NULL, 'java_thread',      '线程安全问题',             'correctness',    '检查SimpleDateFormat/HashMap等非线程安全类是否在多线程环境共享。使用DateTimeFormatter/ConcurrentHashMap替代。共享可变状态使用synchronized或ReentrantLock保护。', 'java', '高', 1, 1, 87),
(NULL, 'java_sqli',        'Java SQL注入(JDBC拼接)',       'security',       '检查是否使用Statement.executeQuery("SELECT ... WHERE name=" + input)拼接SQL。必须使用PreparedStatement预编译,参数用?占位符绑定。MyBatis中使用#{param}而非${param}。', 'java', '严重', 1, 1, 88),
(NULL, 'java_xss',         'Java Web XSS防护',           'security',       '检查Servlet/Spring Controller中是否将用户输入直接写入response。使用ESAPI.encoder()进行输出编码。Spring默认开启HTML转义,但@ResponseBody返回的JSON也需要注意。', 'java', '严重', 1, 1, 89),
(NULL, 'java_log_inject',  '日志注入(Log Injection)',       'security',       '检查日志中是否直接打印用户输入,可能包含换行符伪造日志条目。使用ESAPI日志接口或对用户输入做清洗(过滤\\r\\n)。', 'java', '中', 1, 1, 90),
(NULL, 'java_sync_lock',   '同步锁范围不当',            'performance',    '检查synchronized块是否锁范围过大,包含非共享状态操作。缩小同步块范围,仅保护临界区。使用ReentrantReadWriteLock区分读写锁提高并发度。ConcurrentHashMap替代全量加锁。', 'java', '中', 1, 1, 91),
(NULL, 'java_enum_switch', '枚举switch缺少default',       'correctness',    '检查枚举类型的switch语句是否遗漏新增枚举值时的default处理,可能导致逻辑分支缺失。建议为switch添加default并记录日志或抛异常,避免静默跳过。', 'java', '中', 1, 1, 92),
(NULL, 'java_bigdecimal',  'BigDecimal精度问题',          'correctness',    '检查BigDecimal构造是否使用new BigDecimal(double),会引入浮点精度误差。必须使用new BigDecimal(String)或BigDecimal.valueOf(double)。金融计算使用BigDecimal而非double/float。', 'java', '高', 1, 1, 93),
(NULL, 'java_spring_scope','Spring Bean作用域错误',        'correctness',    '检查有状态的Spring Bean是否误用了默认单例(Singleton)作用域,导致多线程/多用户数据串用。有状态Bean应使用prototype/request/session作用域,或改为无状态设计。', 'java', '高', 1, 1, 94),
(NULL, 'java_to_string',   '数组直接toString',            'correctness',    '检查数组是否直接调用toString(),默认输出类型标识和哈希码(info@xxxyyy),而非数组内容。使用Arrays.toString(arr)或Arrays.deepToString(嵌套数组)。', 'java', '中', 1, 1, 95),
(NULL, 'java_exception_lost','异常链丢失',               'robustness',     '检查catch块中再次抛出异常时是否传入了原始异常作为cause(new Exception(msg, e)),导致异常链断裂难以排查根因。', 'java', '中', 1, 1, 96),
(NULL, 'java_string_loop',  '循环内字符串+拼接',           'performance',    '检查循环内是否使用+或String.concat拼接字符串,每次创建新String副本。使用StringBuilder(非线程安全)或StringBuffer(线程安全)。Java 9+ JVM已优化简单+拼接但显式用StringBuilder更清晰。', 'java', '中', 1, 1, 97);

-- ================== JavaScript / TypeScript (20条, language=typescript) ==================

INSERT INTO review_rule (user_id, rule_code, rule_name, rule_type, rule_content, language, severity, enabled, is_builtin, sort_order) VALUES
(NULL, 'ts_any',           'any类型滥用',              'style',          '检查是否滥用any类型绕过类型检查,失去TypeScript的类型安全优势。使用unknown替代any以强制类型收窄,或定义明确的interface/type。使用@ts-ignore应添加注释说明原因。', 'typescript', '中', 1, 1, 52),
(NULL, 'ts_null_check',    '可选链与空值合并缺失',         'correctness',    '检查对象深层属性访问是否未使用可选链(?.)导致Cannot read properties of undefined。使用??设置默认值: const name = user?.profile?.name ?? "未知"。', 'typescript', '高', 1, 1, 53),
(NULL, 'ts_async_no_await','async函数缺少await误用',       'correctness',    '检查async函数中调用了返回Promise的函数但没有await,导致Promise未被等待直接用于后续逻辑。所有返回Promise的调用在需要结果时必须await。', 'typescript', '高', 1, 1, 54),
(NULL, 'ts_immutable',     '直接修改props/状态',          'correctness',    '检查是否直接修改了传入的props对象或直接从state中赋值后修改。React中props只读,Vue中props不可直接赋值。深拷贝后再修改,或使用不可变更新模式。', 'typescript', '高', 1, 1, 55),
(NULL, 'ts_hook_deps',     'React Hook依赖数组缺失',       'correctness',    '检查useEffect/useMemo/useCallback的依赖数组是否包含所有在闭包内引用的状态/属性。缺少依赖可能导致闭包陷阱(使用过期值)。使用eslint-plugin-react-hooks检查。', 'typescript', '高', 1, 1, 56),
(NULL, 'ts_memo',          '缺少useMemo/useCallback优化',   'performance',    '检查传递给子组件的对象/函数是否每次渲染都创建新引用,导致子组件不必要的重渲染。稳定引用使用useMemo/useCallback包裹,子组件使用React.memo。', 'typescript', '中', 1, 1, 57),
(NULL, 'ts_key_prop',      '列表key使用索引',             'correctness',    '检查v-for/map渲染列表是否使用index作为key。index作为key在列表增删/排序时会导致渲染错误和状态串位。使用唯一且稳定的ID作为key。', 'typescript', '中', 1, 1, 58),
(NULL, 'ts_memory_leak',   '事件监听/定时器未清理',          'robustness',     '检查组件中addEventListener/setInterval/subscribe是否在组件卸载(onUnmounted/useEffect return)时清理,导致内存泄漏。', 'typescript', '高', 1, 1, 59),
(NULL, 'ts_global_style',  '全局样式污染',             'style',          '检查CSS是否使用了过于宽泛的全局选择器(tag/class名过短),可能无意中影响其他组件。使用CSS Modules/scoped样式/BEM命名约定。', 'typescript', '低', 1, 1, 60),
(NULL, 'ts_promise_chain', 'Promise链缺少错误处理',        'robustness',     '检查Promise调用链(.then().then())是否缺少.catch()或在async函数中缺少try-catch。未处理的Promise rejection可能导致静默失败或进程崩溃。', 'typescript', '高', 1, 1, 61),
(NULL, 'ts_eval_js',       'eval/Function动态执行',        'security',       '检查是否使用eval()/new Function()/setTimeout(string)执行动态代码,可导致XSS和代码注入。使用JSON.parse解析数据,动态逻辑使用策略模式/映射表。', 'typescript', '严重', 1, 1, 62),
(NULL, 'ts_xss_inner',     'innerHTML/dangerouslySetInnerHTML', 'security',  '检查是否直接使用innerHTML/outerHTML/document.write或React的dangerouslySetInnerHTML。必须对内容进行DOMPurify.sanitize(),或使用textContent/创建文本节点。', 'typescript', '严重', 1, 1, 63),
(NULL, 'ts_deep_clone',    '深拷贝使用JSON.parse(JSON.stringify)', 'correctness', '检查深拷贝是否使用JSON序列化方式,会丢失Date/Function/undefined/循环引用。使用structuredClone()或lodash.cloneDeep。注意:lodash需要tree-shaking导入。', 'typescript', '中', 1, 1, 64),
(NULL, 'ts_loop_await',    '循环内串行await降低性能',       'performance',    '检查是否在for循环中使用await导致串行执行。无依赖的异步任务使用Promise.all并发执行: await Promise.all(items.map(async i => ...))。', 'typescript', '中', 1, 1, 65),
(NULL, 'ts_api_key',       '前端暴露API密钥',            'security',       '检查前端代码中是否硬编码了API Key/Secret/AccessToken等敏感凭证。前端所有密钥均公开,敏感操作必须在后端代理。使用环境变量也仅适用于编译时,运行时仍暴露。', 'typescript', '严重', 1, 1, 66),
(NULL, 'ts_no_unsanitized','URL参数未校验注入',            'security',       '检查页面是否从URL参数直接取值并用于DOM操作/跳转(如location.href=params.redirect)。redirect参数必须做白名单校验,防止开放重定向钓鱼。', 'typescript', '高', 1, 1, 67),
(NULL, 'ts_obj_ref',       '对象引用比较误用',            'correctness',    '检查是否使用===比较两个对象/数组,===比较引用地址而非内容。使用lodash.isEqual或JSON.stringify比较(注意key顺序)。React/Vue的watcher默认使用引用比较。', 'typescript', '中', 1, 1, 68),
(NULL, 'ts_setstate_async','setState异步特性误用',        'correctness',    '检查setState后是否立即读取state值,React state更新是异步批处理的。使用setState的函数式更新(state=>...),或useEffect监听state变化后执行后续逻辑。', 'typescript', '高', 1, 1, 69),
(NULL, 'ts_large_bundle',  '未使用Tree Shaking/代码分割',    'performance',    '检查大型第三方库是否整包导入(import _ from "lodash"),导致打包体积过大。使用按需导入(import debounce from "lodash/debounce")。路由级组件使用动态import()实现代码分割。', 'typescript', '中', 1, 1, 70),
(NULL, 'ts_race_condition','竞态条件(Request Race)',       'correctness',    '检查异步请求是否可能因为响应顺序不确定导致UI显示旧数据(搜索框/分页切换)。使用AbortController取消前一次请求,或在响应中携带版本号/请求ID对照忽略过期响应。', 'typescript', '高', 1, 1, 71);

-- ================== Go (14条, language=go) ==================

INSERT INTO review_rule (user_id, rule_code, rule_name, rule_type, rule_content, language, severity, enabled, is_builtin, sort_order) VALUES
(NULL, 'go_err_check',     '错误未检查',              'robustness',     '检查函数返回的error是否被忽略(使用_丢弃)。Go中必须显式检查每个error,使用if err!=nil处理。defer中的错误也需要检查或显式忽略并注释原因。', 'go', '高', 1, 1, 100),
(NULL, 'go_defer_close',   'defer中资源未关闭',          'robustness',     '检查打开的文件/连接/Body等资源是否在defer中关闭。defer resp.Body.Close()应紧跟在http.Get/open之后,在错误检查之前执行。', 'go', '高', 1, 1, 101),
(NULL, 'go_goroutine_leak','goroutine泄漏',            'correctness',    '检查启动的goroutine是否有退出机制。使用context.Context传递取消信号,select监听ctx.Done()。无缓冲channel的发送方若没有接收方会永久阻塞。', 'go', '高', 1, 1, 102),
(NULL, 'go_channel_close', 'channel未关闭导致死锁',       'correctness',    '检查range遍历channel时是否在发送完成后关闭channel,否则range永久阻塞。原则:发送方关闭channel。使用sync.WaitGroup等待所有goroutine完成。', 'go', '高', 1, 1, 103),
(NULL, 'go_mutex_copy',    'mutex按值传递导致失效',        'correctness',    '检查sync.Mutex是否以值传递而非指针传递,值拷贝后互斥锁失效。Mutex使用指针接收者,且不应嵌入struct后按值传递整个struct。go vet会提示此问题。', 'go', '高', 1, 1, 104),
(NULL, 'go_nil_map',       'nil map写入panic',          'correctness',    '检查是否对nil map进行赋值操作(var m map[string]int; m["key"]=1),会触发panic。使用make(map[K]V)初始化,或使用字面量声明。读取nil map不panic,返回零值。', 'go', '高', 1, 1, 105),
(NULL, 'go_slice_append',  'slice append容量陷阱',        'correctness',    '检查append后的slice是否与原始slice共享底层数组,可能产生副作用。需要独立副本时使用copy(dst,src)或append([]T{},src...)。切片截取s[low:high]也共享底层数组。', 'go', '中', 1, 1, 106),
(NULL, 'go_context',       '未传递context导致超时缺失',     'robustness',     '检查网络/数据库等IO操作是否未传入context,导致无法超时取消。使用context.WithTimeout设置合理超时,HTTP handler使用r.Context()。', 'go', '中', 1, 1, 107),
(NULL, 'go_panic_recover', 'panic未在goroutine中recover',  'robustness',     '检查独立goroutine中是否未recover,goroutine的panic会导致整个进程崩溃。在每个goroutine的顶层defer中recover,并记录日志。HTTP handler框架通常已内置。', 'go', '高', 1, 1, 108),
(NULL, 'go_sql_inject',    'Go SQL注入(字符串拼接)',        'security',       '检查是否使用fmt.Sprintf构造SQL并直接执行。必须使用参数化查询:db.Query("SELECT ... WHERE name=?", name)。禁止拼接用户输入到SQL字符串中。', 'go', '严重', 1, 1, 109),
(NULL, 'go_template_xss',  'html/template误用导致XSS',     'security',       '检查是否使用了text/template生成HTML,该模板不转义HTML特殊字符。必须使用html/template包,它自动根据上下文转义。使用template.HTML类型需确保内容已消毒。', 'go', '严重', 1, 1, 110),
(NULL, 'go_json_omitempty','JSON omitempty与零值冲突',     'correctness',    '检查bool/int零值(false/0)是否被omitempty意外省略。API返回中应使用指针类型*int/*bool来区分未设置和零值。', 'go', '中', 1, 1, 111),
(NULL, 'go_interface_nil', 'interface nil判定陷阱',        'correctness',    '检查interface类型变量nil判断是否有误。interface包含(type,value)双元组,仅当type和value都为nil时才等于nil。返回具体类型的nil指针赋给interface后!=nil。', 'go', '高', 1, 1, 112),
(NULL, 'go_exit_in_lib',   '库代码中调用os.Exit/log.Fatal', 'style',         '检查非main包的代码中是否调用了os.Exit或log.Fatal,导致调用方无法优雅处理。库中应返回error让调用方决定如何处理。', 'go', '中', 1, 1, 113);

-- ================== SQL (12条, language=sql) ==================

INSERT INTO review_rule (user_id, rule_code, rule_name, rule_type, rule_content, language, severity, enabled, is_builtin, sort_order) VALUES
(NULL, 'sql_select_star',   'SELECT * 查询',              'performance',    '检查是否使用SELECT * 查询所有列,造成不必要的IO和带宽消耗。显式列出所需字段,避免查询大字段(BLOB/TEXT)除非必要。表结构变更时SELECT *可能导致解析错误。', 'sql', '中', 1, 1, 115),
(NULL, 'sql_no_index',     '缺少索引导致全表扫描',          'performance',    '检查WHERE/JOIN/ORDER BY子句中的列是否建立了索引。经常查询的列/外键列/排序列应创建索引。但避免过度索引降低写入性能。使用EXPLAIN分析执行计划。', 'sql', '高', 1, 1, 116),
(NULL, 'sql_no_transaction','缺少事务保护导致数据不一致',    'robustness',     '检查需要原子性的多步写操作(转账/扣库存)是否未包裹在事务中。使用BEGIN/COMMIT/ROLLBACK确保ACID。事务应尽量短小,避免在事务中进行外部API调用。', 'sql', '高', 1, 1, 117),
(NULL, 'sql_implicit_conv','隐式类型转换导致索引失效',        'performance',    '检查WHERE条件中字符串字段与数字比较,会触发隐式转换导致索引失效。保持WHERE条件左右两侧类型一致。使用CAST/CONVERT显式转换或调整字段类型。', 'sql', '中', 1, 1, 118),
(NULL, 'sql_like_wildcard','LIKE前置通配符导致索引失效',      'performance',    '检查LIKE查询是否使用前置通配符(WHERE name LIKE "%keyword"),无法使用B-tree索引。改为后置通配符(name LIKE "keyword%")或使用全文索引(FULLTEXT)。', 'sql', '高', 1, 1, 119),
(NULL, 'sql_nullable_concat','NULL与字符串拼接结果为NULL',    'correctness',    '检查字符串拼接是否可能包含NULL值,导致整条结果变为NULL。使用COALESCE(col,"")或CONCAT_WS(分隔符, col1, col2)忽略NULL。', 'sql', '中', 1, 1, 120),
(NULL, 'sql_func_on_index','索引列使用函数导致索引失效',       'performance',    '检查WHERE子句中是否在索引列上使用了函数(WHERE DATE(create_time)="2024-01-01"或WHERE UPPER(name)="A"),导致索引失效。使用范围查询替代函数: WHERE create_time>="2024-01-01" AND create_time<"2024-01-02"。', 'sql', '高', 1, 1, 121),
(NULL, 'sql_large_offset', '大偏移量分页性能问题',          'performance',    '检查是否使用LIMIT offset,page_size进行大偏移量分页(off>1000),数据库需要扫描offset+page_size行后丢弃前offset行。使用游标分页(WHERE id>last_id ORDER BY id LIMIT page_size)或延迟关联。', 'sql', '中', 1, 1, 122),
(NULL, 'sql_not_in_null',  'NOT IN子查询中的NULL陷阱',       'correctness',    '检查NOT IN子查询中是否可能返回NULL,NOT IN (1,2,NULL)结果永远为空。使用NOT EXISTS替代,或使用NOT IN (SELECT col FROM t WHERE col IS NOT NULL)。', 'sql', '中', 1, 1, 123),
(NULL, 'sql_drop_truncate','危险DDL操作无确认机制',         'security',       '检查是否直接使用DROP TABLE/TRUNCATE等不可逆操作,应添加确认逻辑或先RENAME备份。生产环境的DDL应走变更工单流程。', 'sql', '高', 1, 1, 124),
(NULL, 'sql_no_limit',     'DELETE/UPDATE无LIMIT/WHERE条件', 'security',       '检查DELETE/UPDATE是否缺少WHERE条件或WHERE条件覆盖全表,可能意外删除/更新所有数据。先SELECT验证影响行数。生产环境建议开启sql_safe_updates。', 'sql', '严重', 1, 1, 125),
(NULL, 'sql_float_compare','浮点数精确比较',            'correctness',    '检查是否使用=/!=比较FLOAT/DOUBLE类型,浮点精度可能导致意外结果。使用范围比较(ABS(a-b)<epsilon)或使用DECIMAL类型存储精确数值。', 'sql', '中', 1, 1, 126);

-- ================== 文档与注释 (14条, language=*) ==================

INSERT INTO review_rule (user_id, rule_code, rule_name, rule_type, rule_content, language, severity, enabled, is_builtin, sort_order) VALUES
(NULL, 'doc_public_api',   '公开函数缺少文档注释',          'documentation',  '检查公开函数/类/接口是否缺少文档注释描述用途/参数/返回值/异常。Python使用docstring,Java使用Javadoc,TS使用JSDoc。API接口应有清晰的OpenAPI/Swagger注解。', '*', '中', 1, 1, 72),
(NULL, 'doc_hack',         '复杂逻辑缺少解释性注释',        'documentation',  '检查复杂算法/业务规则/hack/workaround是否缺少解释性注释,说明为什么这样做而非做了什么。代码本身应自解释"做什么",注释解释"为什么"。', '*', '中', 1, 1, 73),
(NULL, 'doc_todo',         'TODO/FIXME未关联issue',        'documentation',  '检查TODO/FIXME/HACK注释是否缺少负责人和关联issue,容易成为永久技术债。格式:TODO(zhangsan): [PROJ-123] 描述,需要在何时完成。', '*', '低', 1, 1, 74),
(NULL, 'doc_changelog',    '重要变更缺少变更记录',          'documentation',  '检查修改现有行为/API签名的代码是否缺少变更说明。破坏性变更(Breaking Change)应明确标注并提供迁移指南。使用conventional commits格式( feat: / fix: / BREAKING CHANGE: )。', '*', '低', 1, 1, 75),
(NULL, 'doc_readme',       '项目缺少README',           'documentation',  '检查项目根目录是否缺少README.md,至少包含:项目简介/环境要求/安装步骤/快速开始/API文档链接/贡献指南。新成员应能在15分钟内完成首次启动。', '*', '中', 1, 1, 76),
(NULL, 'doc_env_example',  '.env.example与环境变量文档',      'documentation',  '检查是否缺少.env.example文件,文档化所有环境变量的含义和默认值。敏感信息不应直接写入.env.example,使用占位符: SECRET_KEY=your-secret-here。', '*', '低', 1, 1, 77);
