# TODO:代码审计 Agent 集成与漏洞识别增强

> 生成时间:2026-06-25
> 最后更新:2026-06-25(R1-R8 全项目 schema 字段遗漏风险扫描与修复完成,服务器同步完成)
> 任务状态:✅ 全部完成(含 R1-R8 schema 字段遗漏风险扫描修复 + 服务器同步)

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

### 1.2 端到端验证发现的 3 个新问题(已完整修复 ✅)

**修复时间**:2026-06-25 14:00-14:20(UTC+8)
**修复状态**:✅ 已通过端到端验证(任务 #54,7 个漏洞,4/4 agent_label 正确落库)

#### 问题 1:review_issue.owasp 列长度不足导致写入失败

**根因**:`owasp` 列定义为 `String(32)`,但 OWASP Top10 完整标题如
`"A07:2021-Identification and Authentication Failures"` 长度 46,超出上限触发
`DataError(1406, "Data too long for column 'owasp'")`,导致审查结果无法落库。

**修复**:
- `backend/app/models/review_issue.py` — owasp 列 `String(32)` → `String(128)`,cwe 列 `String(32)` → `String(64)`
- `backend/alembic/versions/009_enlarge_review_issue_owasp_cwe.py` — 新增 Alembic 009 迁移(已执行)

#### 问题 2:AiLogOut/AiLogDetailOut schema 缺少 agent_label 字段

**根因**:Pydantic schema `AiLogOut` 和 `AiLogDetailOut` 未定义 `agent_label` 字段,
即使 ORM 模型和数据库表有该字段,API 响应序列化时也会丢失,前端拿到的 `agent_label` 始终为 `null`。

**修复**:
- `backend/app/schemas/ai_log.py` — `AiLogOut` 和 `AiLogDetailOut` 各添加 `agent_label: Optional[str] = None`

#### 问题 3:ai_log_service._to_traceable_dict 遗漏 agent_label

**根因**:`ai_log_service._to_traceable_dict()` 手动构造响应 dict 时,遗漏了 `agent_label` 键,
即使 schema 有该字段,实际返回的 dict 中该键不存在,Pydantic 填充默认值 None,API 仍返回 `null`。

**修复**:
- `backend/app/services/ai_log_service.py` — `_to_traceable_dict` 返回的 dict 中添加 `"agent_label": log.agent_label`

#### 完整修复验证

| 验证项 | 结果 |
|--------|------|
| 本地单元测试(19 个) | ✅ 全部通过 |
| ruff 代码规范检查 | ✅ 通过(1 个已有非本次引入) |
| Docker 镜像重建 | ✅ 镜像已固化(替代临时 docker cp) |
| 服务器迁移(alembic 009) | ✅ 已执行,版本 009 (head) |
| 端到端验证(任务 #54) | ✅ 4/4 agent_label 正确落库 |
| 漏洞识别 | ✅ 7 个漏洞,5 种 CWE,4 个已知漏洞全命中 |

**新增测试文件**:
- `backend/tests/unit/test_ac2_e2e_fixes.py` — 16 个单元测试(4 个测试类)
- `backend/tests/e2e/ac2_e2e_verify.py` — 7 步端到端验证脚本
- `backend/tests/fixtures/vulnerable_ac2_e2e.py` — 含 4 个已知漏洞的测试样本

### 1.3 T17 契约测试失败:后端缺少 /code-files/{id}/meta 端点(已修复 ✅)

**修复时间**:2026-06-25
**修复状态**:✅ 契约测试 2/2 通过,后端 723 测试全过

**根因**:T13 前端子任务在 `api/codeFile.ts` 添加了 `getFileMetadata()` 调用 `GET /code-files/{id}/meta`,但后端未实现该端点,导致契约测试 `test_frontend_http_api_calls_match_backend_routes` 失败。

**修复方案**(3 处改动):
- `backend/app/schemas/code_file.py` — 新增 `CodeFileMetaOut` schema(14 个字段)
- `backend/app/services/code_file_service.py` — 新增 `get_file_meta()` 函数(实时计算 MD5/SHA-256,推断 MIME 类型)
- `backend/app/api/v1/code_files.py` — 新增 `GET /code-files/{file_id}/meta` 路由(权限 FILE_VIEW)

### 1.4 T17 前端构建失败:ReportTemplateManage.vue Jinja2 模板转义(已修复 ✅)

**修复时间**:2026-06-25
**修复状态**:✅ npm run build 成功,vue-tsc 零错误

**根因**:`ReportTemplateManage.vue:150` 使用 `{{ '{{ project }}' }}` 转义 Jinja2 模板变量,Vue 解析器在字符串内 `}}` 处提前终止插值,报 `Unterminated string constant`。

**修复方案**:改用 `v-pre` 指令跳过 Vue 编译,让 `{{ project }}` 原样显示为 Jinja2 模板语法提示。

## 二、已知限制(非阻塞)

### 2.1 SQL 注入静态规则不覆盖 `+` 拼接(已修复 ✅)

**修复时间**:2026-06-25
**修复状态**:✅ 全量测试 785 passed,服务器验证通过

**历史记录**:`sql_string_concat` 规则曾仅覆盖 f-string / % 格式化 / .format(),不覆盖 `+` 拼接形式。
- **修复方案**:在 `security_static_rules.py` 的 `sql_string_concat` 正则中新增 3 个分支:
  - 分支 A:`cursor.execute("SELECT..." + var)` 形式
  - 分支 B:`query = "SELECT..." + var` 赋值拼接形式
  - 分支 C:`query = var + "...WHERE..."` 变量在前的拼接形式
- **避免误报**:要求拼接的字符串字面量必须包含 SQL 关键字(SELECT/INSERT/UPDATE/DELETE/REPLACE/CREATE/ALTER/DROP,或 WHERE/VALUES/SET/AND/OR/FROM/JOIN)
- **验证**:新增 4 个测试用例(3 正例 + 1 负例),服务器容器内验证 `cursor.execute("SELECT * FROM t WHERE id=" + user_id)` 命中 ✅

### 2.2 硬编码密码正则不匹配 `DB_PASSWORD` 前缀(已修复 ✅)

**修复时间**:2026-06-25
**修复状态**:✅ 全量测试 785 passed,服务器验证通过

**历史记录**:`Hardcoded Password` 正则要求 `(?:password|passwd|pwd)` 前面是 `[^A-Za-z0-9_]`(非字母数字下划线),`DB_PASSWORD` 中的 `_P` 不满足,导致漏报。
- **修复方案**:将 `security_patterns.py` 中 `Hardcoded Password` 正则的前缀字符类从 `[^A-Za-z0-9_]` 改为 `[^A-Za-z0-9]`,允许 `_` 前缀。
- **覆盖场景**:`DB_PASSWORD = "xxx"` / `USER_PASSWORD = "xxx"` / `REDIS_PASSWORD = "xxx"` 等大写/小写蛇形命名
- **避免误报**:`mypassword = "xxx"` 中 `password` 前面是字母 `p`,仍不匹配(字母不属于 `[^A-Za-z0-9]`)
- **验证**:新增 4 个测试用例(2 正例 + 1 边界 + 1 负例),服务器容器内验证 `DB_PASSWORD = "mysecret123"` 命中 ✅,`mypassword = "notmatched"` 不误报 ✅

### 2.3 EvolutionAgent 预存测试失败(已修复 ✅)

**修复时间**:2026-06-25
**修复状态**:✅ 全量测试 778 passed in 9.25s,零失败

**历史记录**:`test_evolution_agent.py::test_run_distills_new_rule_and_dedups` 曾报 `KeyError: 'new_rule_proposals'`。
- 归属:EvolutionAgent 模块的预存问题,非本任务(代码审计 Agent 集成)引入
- **实际修复路径**:在后续的 `AgentSkill 自进化与总调度升级` 任务(P0-P3 Skill 抽象层)中,
  `EvolutionAgent.run()` 已重构为委托给 `SelfImprovementSkill.evolve()`(七步闭环模板方法),
  闭环逻辑下沉到 Skill 层,`evolve_target()` 钩子统一返回 `fp_proposals + new_rule_proposals`,
  不再有 KeyError 风险。
- **验证**:`pytest tests/unit/agents/test_evolution_agent.py -v` → 8 passed
- **全量回归**:`pytest -q` → 778 passed in 9.25s,零失败

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

## 五、全项目 schema 字段遗漏风险扫描(2026-06-25)

> 触发原因:AC2 端到端验证发现「schema 有字段 / service dict 没字段」导致 API 返回 null 的同类风险。
> 扫描范围:14 个 ORM 模型 × 对应 Pydantic schema × service 层 dict 构造点(73 处)三层交叉对比。
> 扫描结论:发现 8 个潜在风险点(1 高/4 中/3 低),其中 1 个与 agent_label 同类(高风险)。

### 5.1 风险清单(按优先级排序)

#### 🔴 高风险(与 agent_label 同类,建议立即修复)

**风险 R1:issue_service.list_issues dict 遗漏全部漏洞元数据字段**

- **文件**:[issue_service.py:152-174](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/issue_service.py#L152-L174)
- **模式**:`list_issues` 手动构造 dict,只填了基础字段,遗漏了 v2/v3 漏洞元数据
- **遗漏字段**(11 个):`owasp`, `cwe`, `evidence`, `exploit_scenario`, `references_json`, `confidence`, `source`, `cvss_score`, `cvss_vector`, `compliance_mapping`, `remediation`, `static_rule_hits`
- **影响**:`IssueListItemOut` schema 已声明这些字段(继承自 `IssueOut`),但 list_issues 返回的 dict 不含这些键,Pydantic 填充为 `None`,前端 IssuesList 页面拿不到 OWASP/CWE/CVSS 等漏洞元数据
- **同类证据**:与 AC2 中 `_to_traceable_dict` 遗漏 `agent_label` 完全同模式(schema 有/dict 没有 → API 返回 null)

#### 🟡 中风险(建议修复)

**风险 R2:IssueOut schema 缺少 handled_by / handled_at 字段**

- **文件**:[review.py:85-122](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/schemas/review.py#L85-L122)
- **遗漏字段**:`handled_by`, `handled_at`, `update_time`
- **影响**:ORM 有字段、`issue_service.update_status` 会写入,但 schema 没声明,前端无法显示「处理人/处理时间」

**风险 R3:CodeFileOut / CodeFileDetailOut schema 缺少 status / raw_size 字段**

- **文件**:[code_file.py:31-58](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/schemas/code_file.py#L31-L58)
- **遗漏字段**:`status`, `raw_size`
- **影响**:`CodeFileMetaOut` 已声明 `raw_size`,但 `CodeFileOut`/`CodeFileDetailOut` 没有 → 文件列表/详情接口拿不到真实大小

**风险 R4:TaskDetailOut schema 缺少 error_message 字段**

- **文件**:[review.py:50-74](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/schemas/review.py#L50-L74)
- **遗漏字段**:`error_message`
- **影响**:审查任务失败时,ORM 有 `error_message` 字段,但 schema 没声明 → 前端无法显示失败原因

**风险 R5:AgentProfileOut schema 缺少 config_json 字段**

- **文件**:[agent_governance.py:45-67](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/schemas/agent_governance.py#L45-L67)
- **遗漏字段**:`config_json`
- **影响**:`AgentProfileUpdateIn` 接受 `config_json` 更新,但 `AgentProfileOut` 不返回 → 管理员无法查看当前扩展配置

#### 🟢 低风险(可选修复)

**风险 R6:多个 schema 缺少 update_time 字段**

- 涉及 schema:`ProjectOut`, `UserListItem`, `DocOut`, `ReplyOut`, `MemberOut`
- 影响:前端无法显示最后更新时间(部分场景需要)

**风险 R7:ForumPost.status / KnowledgeDoc.status 在对应 schema 缺失**

- 文件:[forum.py:38-50](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/schemas/forum.py#L38-L50)、[knowledge.py:16-25](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/schemas/knowledge.py#L16-L25)
- 影响:列表查询已过滤 `status="normal"`/`"active"`,前端拿不拿 status 影响小

**风险 R8:ProjectMember.update_time 在 MemberOut schema 缺失**

- 文件:[project_member.py:23-32](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/schemas/project_member.py#L23-L32)
- 影响:无法显示成员加入后的角色变更时间

### 5.2 扫描方法学

1. **三层模型**:
   - ORM Model(数据库列定义)→ Pydantic Schema(API 响应字段)→ Service dict(手动构造响应字典)
   - 任一层遗漏都会导致前端拿不到数据
2. **Pydantic v2 `from_attributes=True`**:
   - 开启后 schema 可自动从 ORM 对象属性映射字段(覆盖 `pagination.to_dict(orm_list)` 路径)
   - **但**:手动 dict 构造路径不受益于此特性(这是 AC2 agent_label 遗漏的根因)
3. **扫描范围**:
   - 14 个 ORM 模型(覆盖全部业务表)
   - 28 个 schema 文件(81 个响应 schema)
   - 24 个 service 文件(9 个 `_to_*_dict` 函数 + 64 个 `return {}` 形式)

### 5.3 验证一致性的代码模式

| 路径 | 示例 | 风险 |
|------|------|------|
| `pagination.to_dict(orm_list)` + schema `from_attributes=True` | `code_file_service.list_files` | ✅ 自动映射,只受 schema 字段声明限制 |
| `pagination.to_dict([_to_dict(orm) for orm in rows])` | `maintenance_service.list_tickets` | ⚠️ dict 字段需与 schema 字段手动对齐 |
| `pagination.to_dict([{...} for row in rows])` | `issue_service.list_issues` | 🔴 dict 字段最易遗漏(无函数封装) |

## 六、T18 服务器同步待办(2026-06-25)

### 6.1 已完成项(全部完成 ✅)

- ✅ 后端代码 rsync 同步(排除 .venv/__pycache__/.pytest_cache)
- ✅ 前端代码 rsync 同步(排除 node_modules/dist)
- ✅ deploy 目录 rsync 同步(排除 .env,服务器保留自己的版本)
- ✅ docs 目录 rsync 同步
- ✅ 后端容器镜像重建(docker compose build backend)
- ✅ 后端容器重启(docker compose up -d backend)
- ✅ 后端 alembic 迁移验证(009 head)
- ✅ 服务器重启后 4 容器全部 Up(cr_backend/cr_frontend/cr_clamav/cr_mysql)
- ✅ /healthz 返回 200,/docs 返回 200
- ✅ 14 个 Agent 已注册,31 个调度任务已启动
- ✅ R1-R8 修复代码已同步到服务器并重建后端容器

### 6.2 待完成项

无。所有任务已完成。

### 6.3 操作指引

如需重新同步,执行:
```bash
# 1. 同步后端代码
rsync -avz --delete --exclude='.venv/' --exclude='__pycache__/' --exclude='.pytest_cache/' --exclude='*.pyc' \
  /Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/ \
  root@81.70.251.90:/opt/code-review/backend/app/

# 2. 重建后端容器
ssh root@81.70.251.90 "cd /opt/code-review/deploy && docker compose build backend && docker compose up -d backend"

# 3. 验证
ssh root@81.70.251.90 "docker ps --format '{{.Names}} {{.Status}}' | grep cr_"
ssh root@81.70.251.90 "docker exec cr_backend curl -sf http://localhost:8000/healthz"
```

### 6.4 已知限制:前端容器镜像未重建

**问题**:服务器内存仅 1.9GB,`docker compose build frontend`(npm ci + vite build)会 OOM 导致 SSH 无响应。
当前前端容器通过 `docker cp` 方式更新 dist 目录 + `nginx -s reload`,不是镜像重建。

**影响**:如果前端容器被删除重建(如 `docker compose down && docker compose up -d`),dist 更新会丢失,
回退到旧镜像的前端文件。

**解决方案**(后续执行):
```bash
# 在服务器资源充足时(如升级内存后)执行镜像重建
ssh root@81.70.251.90 "cd /opt/code-review/deploy && docker compose build frontend && docker compose up -d frontend"
```
