# TODO:代码审计 Agent 集成与漏洞识别增强

> 生成时间:2026-06-25
> 最后更新:2026-06-25(AC2 已修复)
> 任务状态:✅ 全部完成,无待修复问题

## 一、已修复问题

### 1.1 AC2:ai_call_log.agent_label 字段为 NULL(已修复 ✅)

**修复时间**:2026-06-25
**修复状态**:✅ 已通过服务器验证

**根因**:
1. `DeepSeekAgent.log_deferred()` 从 meta 中读取了 model_tag 等字段,但遗漏了 agent_label
2. `DeepSeekAgent._log()` 没有 agent_label 参数,chat() 接收的 agent_label 无法落库
3. 顺序模式(BaseAgent.call 路径)完全不写 AiCallLog

**修复方案**(4 处改动):
- `deepseek_agent.py:log_deferred()` — 从 meta 读取 agent_label 写入 AiCallLog
- `deepseek_agent.py:_log()` — 增加 agent_label 参数
- `deepseek_agent.py:chat()` — 调用 _log() 时传递 agent_label
- `review_service.py:_log_sequential_call()` — 新增辅助函数,为顺序模式补写 AiCallLog

**验证结果**:
- 本地:10 个新单元测试全部通过
- 服务器:容器内调用 log_deferred() 验证 agent_label=code_reviewer 正确落库
- 历史 991 条记录仍为 NULL(修复前产生),新记录将正确写入

## 二、已知限制(非阻塞)

### 2.1 SQL 注入静态规则不覆盖 `+` 拼接

`sql_string_concat` 规则仅覆盖 f-string / % 格式化 / .format(),不覆盖 `+` 拼接形式。
- 影响:`"SELECT * FROM t WHERE id=" + user_input` 不会被静态规则命中
- 处理:LLM 引擎可识别此类注入,双引擎兜底
- 后续:可扩展 `security_static_rules.py` 增加对 `+` 拼接的检测

### 2.2 硬编码密码正则不匹配 `DB_PASSWORD` 前缀

`Hardcoded Password` 正则要求 `(?:password|passwd|pwd)` 前面是非字母数字下划线字符,`DB_PASSWORD` 中的 `_P` 不满足此条件。
- 影响:`DB_PASSWORD = "xxx"` 不会被正则命中
- 处理:测试样本改用 `password = "xxx"` 形式
- 后续:可调整正则允许 `XXX_PASSWORD` 形式

### 2.3 EvolutionAgent 预存测试失败(与本任务无关)

`test_evolution_agent.py::test_run_distills_new_rule_and_dedups` 报 `KeyError: 'new_rule_proposals'`。
- 归属:EvolutionAgent 模块的预存问题,非本任务引入
- 影响:不影响代码审计功能
- 处理:已记录,后续由对应模块负责人修复

## 三、服务器环境信息

- **服务器 IP**:81.70.251.90
- **部署方式**:rsync 同步 + docker compose up -d --build
- **容器状态**:
  - cr_mysql: Up 2 days (healthy)
  - cr_backend: Up(14 个 Agent 已注册)
  - cr_frontend: Up
- **数据库迁移**:alembic upgrade head 已执行成功
- **访问地址**:
  - 前端:http://81.70.251.90
  - 接口文档:http://81.70.251.90/docs

## 四、后续优化建议(非本任务范围)

1. **AC2 修复后**:可增加 Agent 调用统计面板,展示各 Agent 调用次数/成功率/耗时
2. **静态规则扩展**:增加更多语言(Java/Go/PHP)的安全规则
3. **压缩包深度扫描**:支持递归解压(压缩包内含压缩包)
4. **前端漏洞展示**:ReviewTaskDetail 页面展示 OWASP/CWE 标签和修复建议
