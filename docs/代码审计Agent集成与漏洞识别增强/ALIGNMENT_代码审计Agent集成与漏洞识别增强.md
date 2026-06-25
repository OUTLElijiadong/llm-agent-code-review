# ALIGNMENT · 代码审计 Agent 集成与漏洞识别增强

> 任务名: `代码审计Agent集成与漏洞识别增强`
> 创建时间: 2026-06-25
> 阶段: Align(对齐)
> 状态: 已完成需求理解与边界确认,进入 Architect 阶段

---

## 一、原始需求

用户提出三个核心问题需要解决:

1. **代码审计功能未调用专用 Agent 执行审计任务**:Agent 模块与核心功能处于分离状态,未实现有效集成。
2. **代码审计核心功能不完善**:审计结果较为抽象,缺乏精准的漏洞识别能力。
3. **编辑器存在功能缺陷**:会将压缩包文件直接以 base64 编码格式打开,需要修复。

附加要求:
- 选取若干包含已知漏洞的测试代码样本,验证当前审计功能是否能准确识别并报告这些漏洞,确认系统是否真正调用了 Agent 进行代码检查。
- 所有操作内容需要同步更新并保持本地环境与服务器环境(81.70.251.90)的一致性。

---

## 二、项目上下文分析

### 2.1 项目技术栈

- 后端: Python 3.9 + FastAPI + SQLAlchemy + Alembic + Pydantic
- 前端: Vue3 + TypeScript + Element Plus + Pinia + Monaco Editor
- AI: DeepSeek API(默认 `deepseek-v4-flash`)+ 多 Agent 审查编排
- 数据库: MySQL 8.0(Docker 容器 `cr_mysql`,宿主机 3307 → 容器 3306)
- 部署: Docker Compose(MySQL + Backend + Frontend + Caddy/HTTPS),线上域名 `lijiadong.cn`

### 2.2 现有"代码审计"相关模块梳理

项目里 "audit" / "review" / "security" 三个词被分别用于不同语义,需先消除歧义:

| 模块 | 路径 | 实际职责 | 与本次需求关系 |
|------|------|---------|--------------|
| `audit_service` | `backend/app/services/audit_service.py` | 管理员操作日志(登录/规则/项目等行为审计) | **无关**,仅是行为审计 |
| `review_service` | `backend/app/services/review_service.py` | 代码审查主流程(`/api/review/start`),多 Agent 协同 | **核心相关** |
| `SecuritySentinelAgent` | `backend/app/agents/security_sentinel_agent.py` | 网络安全深度扫描 Agent(`/api/security/scan*`),含 20 条静态规则 + 20 类正则秘钥 + LLM 审查 + 跨文件数据流 | **核心相关** |
| `CodeReviewerAgent` | `backend/app/agents/review_agent.py` | 代码审查 Agent(注册但**未被调用**) | **核心相关**,需激活 |
| `ReviewOrchestratorAgent` | `backend/app/agents/review_orchestrator_agent.py` | 审查调度 Agent(注册但**未被主流程调用**) | **核心相关**,需激活 |

### 2.3 Agent 模块与核心功能分离的具体证据

1. **`CodeReviewerAgent` 类从未被实例化调用**:`review_service._execute_review()` 直接调用 `DeepSeekAgent.chat()`,绕过 `BaseAgent.call()` 体系。
2. **`_PROFILE_TO_AGENT_CODE` 映射只是事件标签**:`reliability/performance/maintainability` 都映射到 `code_reviewer`,但这只是给 EventBus 发事件时用的 agent_code 字符串,并非真正调用对应 Agent。
3. **`_emit_review_event` 只是手动发事件**:不是实际 Agent 调用,只是给 SSE 频道推送状态文案。
4. **`SecuritySentinelAgent` 未接入主流程**:20 条静态规则、20 类正则秘钥扫描、跨文件数据流分析等能力只在 `/api/security/scan*` 路径生效,`/api/review/start` 主流程完全不调用。
5. **`AiCallLog` 归因失真**:LLM 调用日志的 `agent_label` 字段是 `general/security/...` 画像 code,而非真实注册的 Agent name(`code_reviewer/security_sentinel/...`),导致 Agent 中心统计与真实调用脱钩。

### 2.4 审计结果抽象、缺乏精准漏洞识别的根因

1. **无静态分析前置过滤**:审查主流程直接全量 LLM 调用,没有先用确定性规则命中,既浪费 token 又漏报确定性问题。
2. **Prompt 设计偏通用**:`prompt_builder.build_prompt()` 使用的模板 `review.zh.md` 只给 LLM 通用规则段+画像段,未强制要求输出 CWE/OWASP/证据/攻击场景/修复建议等结构化字段。
3. **结果解析过粗**:`result_parser.Issue` 只有 `line_number/issue_type/severity/title/description/suggestion/fixed_code`,没有 `owasp/cwe/evidence/exploit_scenario/references/confidence` 等漏洞元数据。
4. **多 Agent 协同只对 security/full 类型生效**:`quick/standard` 走单代理串行,且 security/full 的"协同"也只是在 LLM 文本层面对比,没有用上 `SecuritySentinelAgent` 的静态规则与正则秘钥库。

### 2.5 编辑器压缩包 base64 问题根因

1. **`language_detector.detect_language()`** 将 `.zip/.tar/.gz/.rar/.7z/.bz2` 等压缩包扩展名映射为 `"binary"`。
2. **`encoding_utils.to_utf8()`** 对 binary 文件用 base64 编码并加前缀 `[BINARY:BASE64:]` 存入 `CodeFile.content` 字段。
3. **`CodeEditor.vue`** 直接把 `detail.content` 丢给 Monaco Editor,未检测 binary 语言或前缀,导致压缩包文件显示 `[BINARY:BASE64:]UEsDBBQAAAA...` 这种 base64 字符串。
4. **`CodeFileList.vue`** 同样未过滤 binary 文件,允许用户点击进入编辑器。

### 2.6 现有 Agent 注册情况

- `AgentRegistry.list_runtime()` 返回 14 个已注册 Agent(含 `code_reviewer`、`security_sentinel`、`review_orchestrator`)。
- 注册中心是单例,所有 Agent 启动时通过 `app/agents/__init__.py` 注册。
- `BaseAgent.call()` 已支持 `api_config` 参数注入用户自定义 API 配置,且自动 emit `THINKING/COMPLETE/FAILED` 事件到 EventBus,自带重试与 `AiCallLog` 日志(但 `AiCallLog` 写入在 `DeepSeekAgent` 子类中,`BaseAgent` 本身不写 log)。

---

## 三、需求理解与边界确认

### 3.1 任务边界(已与用户确认)

| 决策点 | 用户选择 | 含义 |
|-------|---------|------|
| 修复范围 | review + security + 编辑器 | 同时修复 review 主流程未调 Agent、SecuritySentinel 能力未接入主流程、编辑器 base64 三个问题 |
| Agent 集成深度 | 深度集成 | `review_service` 通过 `BaseAgent.call()` 调用 `CodeReviewerAgent`/`SecuritySentinelAgent`,统一事件总线/调用日志/AiCallLog;`security`/`full` 类型直接复用 `SecuritySentinelAgent` 的静态规则+正则+LLM+数据流分析 |
| 漏洞识别策略 | 静态规则+LLM 双引擎 | 把 `SecuritySentinelAgent` 的 20 条静态规则 + 20 类正则秘钥扫描接入 review 主流程,先确定性命中再 LLM 深度审查,所有 finding 强制带 CWE/OWASP/证据/攻击场景/修复建议 |
| 测试样本 | 新建多类型漏洞样本 + 现有 fixture | 新建 6-8 个覆盖 SQL 注入/XSS/硬编码密钥/路径遍历/反序列化/SSRF/命令注入 的测试样本,加上现有 `qa_security_fixture.py`,本地启动服务真实跑 review 任务验证 |
| 本地验证 | 启动本地全栈 | 启动 Docker MySQL + 后端(8000)+前端(5173),真实点击 + API 验证 |
| LLM API Key | 用现有 `.env` | `.env` 中已有 `DEEPSEEK_API_KEY=sk-0b4e...`,无需额外配置 |
| 服务器同步 | rsync + deploy.sh 重建 | 本地验证通过后 rsync 同步到 `/opt/code-review`,执行 `deploy/deploy.sh` 重建容器,保留数据库数据 |
| 编辑器压缩包修复 | **自动解压入库** | 后端识别压缩包(zip/tar/gz/rar)时自动解压并作为多个新文件入库,不作为单个 binary 文件存储 |

### 3.2 任务范围(明确做/不做)

**做**:
- 重构 `review_service._execute_review()`,通过 `BaseAgent.call()` 调用真实 Agent
- 在 review 主流程中接入 `SecuritySentinelAgent` 的静态规则 + 正则秘钥扫描作为前置过滤
- 扩展 `Issue`/`ReviewIssue` 数据结构,新增 `owasp/cwe/evidence/exploit_scenario/confidence` 等字段
- 增强审查 Prompt,强制 LLM 输出结构化漏洞元数据
- 修复编辑器压缩包 base64 问题:后端自动解压压缩包并作为多个文件入库
- 新建 6-8 个漏洞测试样本,本地真实跑 review 任务验证 Agent 调用链路与漏洞识别效果
- 同步代码到服务器 `81.70.251.90` 并重新部署

**不做**:
- 不重构 `audit_service`(操作审计日志),与本次需求无关
- 不重构圆桌讨论审功能(`/api/discuss/*`)
- 不重构 Agent 自进化/治理平台功能
- 不修改数据库表结构(仅在 `ReviewIssue` 表新增字段或新增表,需 Alembic 迁移)
- 不修改前端 Agent 办公室 UI(除非必要)
- 不重构 `/api/security/scan*` 独立安全扫描接口(保留其现有行为)

### 3.3 项目特性规范对齐

- **数据库**: MySQL 8.0,严禁 SQLite;`DB_HOST=127.0.0.1`,`DB_PORT=3307`
- **Python 版本**: 3.9,类型注解用 `Optional[X]` 而非 `X | None`
- **代码规范**: ruff + compileall,所有函数需函数级注释
- **API Key**: `.env` 管理,不提交 git
- **测试**: 测试优先,边界覆盖
- **6A 工作流**: 严格走 Align → Architect → Atomize → Approve → Automate → Assess

---

## 四、疑问澄清(已在交互中解决)

| 疑问 | 用户回答 |
|------|---------|
| "代码审计"具体指哪个模块? | review + security + 编辑器三者 |
| Agent 集成深度? | 深度集成,通过 BaseAgent.call() 统一调用 |
| 漏洞识别策略? | 静态规则 + LLM 双引擎 |
| 测试样本策略? | 新建 6-8 个多类型漏洞样本 + 现有 fixture |
| 本地验证方式? | 启动本地全栈(Docker MySQL + 后端 + 前端) |
| LLM API Key 来源? | 用现有 `.env` 中的 DeepSeek Key |
| 服务器同步方式? | rsync + deploy.sh 重建容器 |
| 编辑器压缩包修复方式? | 自动解压入库(后端识别压缩包并解压成多个文件) |

---

## 五、关键风险与缓解

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| 重构 `review_service` 主流程可能影响现有审查任务 | 高 | 保留旧路径作为 fallback,通过环境变量 `REVIEW_USE_BASE_AGENT` 控制;先单测再集成测试 |
| `ReviewIssue` 新增字段需要数据库迁移 | 中 | 写 Alembic 迁移脚本,本地验证后同步到服务器 |
| 压缩包自动解压可能引入 zip slip 漏洞 | 高 | 解压时严格校验路径,拒绝 `..` 和绝对路径;限制解压后文件数量(≤100)和总大小(≤50MB) |
| LLM 调用成本上升(双引擎) | 中 | 静态规则前置过滤,命中后仍调 LLM 但 prompt 更聚焦;支持环境变量关闭 LLM |
| 服务器重新部署可能中断线上服务 | 中 | 选择业务低峰期部署;deploy.sh 支持滚动重建 |
| DeepSeek API Key 额度限制 | 低 | 现有 Key 可用,单次验证调用量可控 |

---

## 六、进入 Architect 阶段的准备

已完成项目上下文分析、需求理解确认、边界确认、疑问澄清,所有关键决策点已与用户达成共识。

下一步: 进入 Architect 阶段,生成 `DESIGN_代码审计Agent集成与漏洞识别增强.md`,包含整体架构图、分层设计、接口契约、数据流图、异常处理策略。
