# ALIGNMENT · AgentSkill 自进化与总调度升级

> 任务名: `AgentSkill自进化与总调度升级`
> 创建时间: 2026-06-25
> 阶段: Align(对齐)
> 状态: 已完成需求理解与边界确认, 待生成 CONSENSUS 后进入 Architect 阶段

---

## 一、原始需求

用户提出三点核心诉求:

1. **给每个 Agent 添加自我进化方式的 skill**: 现有项目仅有 1 个全局 `EvolutionAgent` 进化 `review_rule` 一类对象,其余 13 个 Agent 完全没有自进化能力。希望每个 Agent 都具备各自的自我进化机制。
2. **安装 proactive-agent 和 self-improvement 这类 skill**: 参考 Anthropic skill 体系的模块化能力设计,引入 `ProactiveSkill`(主动行动)与 `SelfImprovementSkill`(自我改进)两类核心 skill,挂载到每个 Agent 上。
3. **聊天 Agent 变成总调度 Agent, 可以操控所有的 Agent**: 现有 `ChatAssistantAgent` 通过 14 种写死的 intent handler 调度,无法动态操控任意 Agent 或触发自进化。希望升级为"总调度",能动态调度任意 Agent 的任意 Skill(包括 EvolutionAgent 的进化操作)。

附加要求:
- 三者结合实现 Skill: 项目内 Python Skill 抽象为主体 + `.claude/skills/` 同步说明文档 + `BaseAgent.skills` 元数据扩展。
- 全部 14 个 Agent 都做 per-Agent 专属进化。
- 双层调度: 意图分类(快速路由) + LLM 动态规划(在该意图下规划具体调用哪些 Agent 的哪些 Skill)。
- 保留现有 `EvolutionAgent` 闭环基础设施(反馈聚合/提案/闸门/审批/回滚)并下沉为 Skill 基座,`EvolutionAgent` 自身也挂载 `SelfImprovementSkill`。
- 触发机制: 手动 + 定时 + 事件三种全支持。
- 数据存储: `evolution_proposal` 加 `agent_name` 字段 + 新增 `agent_skill_record` 表。
- 范围: 前端同步改 + 本地全栈验证 + 服务器同步部署 + `.claude/skills/` 文档同步。
- 所有内容同步更新本地与服务器 `81.70.251.90`,保持环境一致。

---

## 二、项目上下文分析

### 2.1 项目技术栈

- 后端: Python 3.9 + FastAPI + SQLAlchemy + Alembic + Pydantic
- 前端: Vue3 + TypeScript + Element Plus + Pinia + Monaco Editor
- AI: DeepSeek API(默认 `deepseek-v4-flash`)+ 多 Agent 审查编排
- 数据库: MySQL 8.0(Docker 容器 `cr_mysql`,宿主机 3307 → 容器 3306)
- 部署: Docker Compose(MySQL + Backend + Frontend + Caddy/HTTPS),线上域名 `lijiadong.cn`
- 服务器: `81.70.251.90` (root),项目路径 `/opt/code-review`,部署脚本 `deploy/deploy.sh`

### 2.2 现有 Agent 注册情况(共 14 个)

`AgentRegistry.list_runtime()` 返回 14 个已注册 Agent, 通过 [orchestrator.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/agents/orchestrator.py) 的 `_init_agents()` 注册:

| Agent name | 类 | category | 现有 skills 元数据 | 是否注入 DB |
|---|---|---|---|---|
| `orchestrator` | `Orchestrator` | meta | Agent 路由/依赖注入/调度编排/执行结果汇总 | 是(self._db) |
| `chat_assistant` | `ChatAssistantAgent` | frontline | 自然语言入口/意图分类/多 Agent 调度/结果整合 | 否(通过 orchestrator) |
| `language_detector` | `LanguageDetectorAgent` | general | (待补) | 否 |
| `project_analyzer` | `ProjectAnalyzerAgent` | general | (待补) | 否 |
| `code_reviewer` | `CodeReviewerAgent` | general | (待补) | 否 |
| `project_manager` | `ProjectManagerAgent` | general | (待补) | 是 |
| `review_orchestrator` | `ReviewOrchestratorAgent` | general | (待补) | 是 |
| `code_file_manager` | `CodeFileManagerAgent` | general | (待补) | 是 |
| `dashboard` | `DashboardAgent` | general | (待补) | 是 |
| `rule_manager` | `RuleManagerAgent` | general | (待补) | 是 |
| `reporter` | `ReportAgent` | general | (待补) | 是 |
| `ai_prompt` | `AiPromptAgent` | general | (待补) | 是 |
| `security_sentinel` | `SecuritySentinelAgent` | general | (待补) | 是 |
| `evolution` | `EvolutionAgent` | meta | 反馈聚合/假阳性抑制/规则蒸馏/提案生成 | 是 |

### 2.3 现有自进化能力盘点(`docs/Agent自进化/`)

项目已实现一套完整的自进化闭环, 但**仅作用于 `review_rule` 一类对象**:

**已实现组件**:
- [EvolutionAgent](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/agents/evolution_agent.py) — 慢环主体
- [evolution_service](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/evolution_service.py) — 反馈聚合/提案生命周期/闸门调度/回滚
- [experience_service](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/experience_service.py) — 经验记忆库
- [eval_gate](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/eval_gate.py) — 黄金回归集闸门
- [feedback_service](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/feedback_service.py) — 反馈聚合
- API: [api/v1/evolution.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/api/v1/evolution.py)

**已实现数据表**:
- `review_experience` (经验记忆库 L1)
- `evolution_proposal` (进化提案 L2/L3, status: pending/eval_passed/eval_failed/approved/rejected/promoted/rolled_back)
- `eval_case` (黄金回归集)

**已实现提案类型**: `new_rule` / `disable_rule` / `adjust_severity` / `narrow_language` / `new_fewshot`

**闭环七步**: Act → Observe → Aggregate → Reflect → Gate → Promote → Rollback

**关键防翻车设计**:
- 双门槛: `min_samples≥20` AND `min_distinct_tasks≥2` 才触发提案
- `ignored` 不自动删规则, 仅提案降级/收窄
- 闸门: `eval_case` 黄金集跑分不退化才放行
- 人工闸门: 提案默认 `enabled=0`, admin 审批才生效
- 可回滚: `applied_rule_id` 支撑一键回滚
- 时间衰减: `weight = (accepted - λ·rejected) · 0.5^(Δdays / halflife)`, `halflife=30d`

### 2.4 现有 ChatAssistantAgent 调度能力分析

[chat_agent.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/agents/chat_agent.py) 现状:

- 通过 LLM(`_INTENT_SYSTEM` prompt)做意图分类, 输出 `{intent, reason, payload}`
- 14 种预定义 intent: chat / detect_language / analyze_project / review_code / list_agents / list_projects / create_project / delete_project / start_review / list_review_tasks / list_review_issues / list_code_files / dashboard / list_rules / list_reports / generate_ai_prompt / security_audit
- 每个 intent 对应一个写死的 `_handle_xxx` handler, 通过 `self._orchestrator.xxx()` 委派
- v2.0 Clarify 协议: 关键字段缺失时主动追问(`clarify_store`)
- v2.1 scope 动态必填(`generate_ai_prompt` / `security_audit` 按 scope 推导)
- 个性化注入: 个人画像 + 个人知识库 RAG

**关键缺陷**:
1. **无法调度 EvolutionAgent**: 没有 `evolution_intent`, 用户无法通过聊天触发进化
2. **无法调度任意 Agent 的任意方法**: handler 写死, 无法动态调用 `language_detector.update_fingerprint()` 等方法
3. **无法串联多 Agent**: 一次 intent 只能调一个 handler, 无法"先 list_projects 再 start_review 再 audit_security"这种链式规划
4. **无 Skill 概念**: 用户无法通过聊天说"让 code_reviewer 跑一轮自进化"或"让 security_sentinel 主动巡检"
5. **意图分类粒度粗**: 14 种 intent 已不够用, 每加一个 Agent 能力就要改 `_INTENT_SYSTEM` prompt 与 handlers 字典

### 2.5 现有 Orchestrator 调度能力分析

[orchestrator.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/agents/orchestrator.py) 现状:

- 注册全部 14 个 Agent 实例
- 暴露**固定方法集**: `detect_language` / `analyze_project` / `review_code` / `create_project` / `list_projects` / `delete_project` / `start_review` / `list_review_tasks` / `list_review_issues` / `list_code_files` / `dashboard_summary` / `list_rules` / `list_reports` / `generate_ai_prompt_for_*` / `audit_security_for_*` / `chat` / `list_agents` / `get_agent`
- v3.1 支持用户自定义 API 配置注入(`set_api_config`)
- 进程级单例(`get_orchestrator`) + 请求级实例(`get_request_orchestrator`)双模式, 避免并发串号

**关键缺陷**:
1. **没有暴露 EvolutionAgent 的 `run()` 方法**: Orchestrator 注入了 `evolution_agent` 但没有 `trigger_evolution()` 之类的方法
2. **没有通用 "调度任意 Agent 任意 Skill" 接口**: 只有固定方法集, 无法 `orchestrator.invoke_skill(agent_name, skill_name, params)`
3. **没有暴露 Agent 的 Skill 列表查询**: 无法 `orchestrator.list_agent_skills(name)`

### 2.6 现有 BaseAgent 与 skills 元数据

[base.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/agents/base.py) 现状:

```python
class BaseAgent:
    name: str = "base"
    description: str = ""
    icon: str = "base"
    color: str = "#5B58E8"
    category: str = "general"
    skills: tuple = ()  # ← 仅 tuple of string, 仅前端展示用
```

- `skills` 字段是 `tuple` of `str`, 仅在 [registry.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/agents/registry.py) `list_runtime()` 转 list 给前端展示
- **没有 Skill 类抽象**: 没有可挂载、可调用、可进化的 Skill 对象
- **没有 `inject()` 抽象方法**: 各 Agent 自己实现 `inject(db, user)`, 签名不一致
- **没有统一的"自进化"接口**: 各 Agent 没有 `self_improve()` / `proactive_check()` 之类的方法

### 2.7 现有事件总线与调度器

可复用的基础设施:
- [event_bus.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/agents/event_bus.py) — Agent 事件总线(THINKING/COMPLETE/FAILED/DISPATCH/CLARIFY)
- [scheduler_service.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/scheduler_service.py) — 定时任务服务, 可挂定时触发进化
- [agent_scheduler_runtime.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/agent_scheduler_runtime.py) — Agent 调度运行时
- [policy_engine.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/policy_engine.py) — 策略引擎(可用作 Skill 权限闸门)
- [audit_service.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/audit_service.py) — 行为审计日志
- [clarify_store.py](file:///Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/agents/clarify_store.py) — 主动追问存储(ProactiveSkill 主动提问可复用)

---

## 三、需求理解与边界确认

### 3.1 任务边界(已与用户确认)

| 决策点 | 用户选择 | 含义 |
|---|---|---|
| Skill 实现形式 | 三者结合 | Python Skill 类主体 + `.claude/skills/` 文档 + `BaseAgent.skills` 元数据扩展 |
| 进化范围 | 全部 14 个 Agent | per-Agent 专属进化对象, 每个 Agent 挂载专属 SelfImprovementSkill |
| 总调度能力 | 双层调度 | 意图分类(快速路由) + LLM 动态规划(在该意图下规划调用链) |
| 现有 EvolutionAgent | 保留并下沉为基座 | 闭环基础设施下沉为 Skill 基类, EvolutionAgent 自身也挂载 SelfImprovementSkill |
| ProactiveSkill 行为 | 4 类全做 | 主动进化触发 + 主动提问/建议 + 主动巡检/发疑 + 主动学习/反思 |
| 触发机制 | 手动 + 定时 + 事件 | API 手动 + scheduler_service 定时 + event_bus 事件驱动 |
| 数据存储 | 两者都做 | `evolution_proposal` 加 `agent_name` + 新增 `agent_skill_record` 表 |
| 前端同步 | 是 | Agent 办公室展示 skills + 进化中心按 Agent 分组 + ChatAgent 总调度 UI |
| 本地验证 | 全栈 | Docker MySQL + 后端(8000) + 前端(5173) 真实验证 |
| 服务器同步 | 是 | rsync 到 81.70.251.90:/opt/code-review + deploy.sh 重建 |
| Claude Skill 文档 | 是 | `.claude/skills/` 同步生成 SKILL.md 说明文档 |
| LLM API Key | 用现有 `.env` | `DEEPSEEK_API_KEY` 已有, 无需额外配置 |

### 3.2 任务范围(明确做/不做)

**做**:

1. **Skill 基类与基础设施**:
   - 新建 `backend/app/agents/skills/` 包
   - `BaseSkill` 抽象基类(可挂载、可调用、可进化、可观测)
   - `ProactiveSkill` 基类(主动行为: 进化触发/提问/巡检/学习)
   - `SelfImprovementSkill` 基类(自进化闭环: 反思→提案→闸门→审批→回滚)
   - Skill 注册中心 `SkillRegistry`(per-Agent 挂载)
   - 把现有 EvolutionAgent 闭环基础设施(反馈聚合/提案/闸门/审批/回滚)下沉为 `SelfImprovementSkill` 基类方法

2. **14 个 Agent 专属 Skill 实现**:
   - 每个 Agent 挂载 1 个专属 `SelfImprovementSkill` 子类 + 1 个专属 `ProactiveSkill` 子类
   - 每个 Skill 定义自己的进化对象与策略(详见 §3.3)
   - `BaseAgent.skills` 元数据扩展为结构化对象(含 skill name/description/invocable flag)

3. **ChatAssistantAgent 双层总调度升级**:
   - 第一层: 保留现有意图分类(快速路由到 14 个领域)
   - 第二层: 在每个意图下用 LLM function calling 动态规划调用链(可串联多 Agent、可调任意 Skill)
   - 把所有 Agent 的所有 Skill 暴露为 tools(描述/参数 schema/示例)
   - 新增 `evolution_intent` / `agent_skill_invoke` / `agent_status` 等 intent 覆盖自进化操作
   - 实现 `Orchestrator.invoke_skill(agent_name, skill_name, params)` 通用接口

4. **数据库迁移**:
   - `evolution_proposal` 表加 `agent_name` 字段(标记提案来自哪个 Agent, 默认 `evolution` 兼容旧数据)
   - 新增 `agent_skill_record` 表(记录每次 Skill 调用: who/when/input/output/effect/duration)
   - Alembic 迁移脚本 `003_agent_skill_evolution.py`

5. **触发机制**:
   - 手动: 扩展 `/api/evolution/trigger` 支持 `agent_name` 参数, 新增 `/api/agents/{name}/skills/{skill}/invoke` 端点
   - 定时: `scheduler_service` 注册 per-Agent 定时进化任务(可配置 cron)
   - 事件: `event_bus` 订阅 `REVIEW_ISSUE_STATUS_CHANGED` / `AI_CALL_THRESHOLD_REACHED` 等事件自动触发

6. **前端同步**:
   - Agent 办公室: 每个 Agent 卡片展示 skills 列表(名称+描述+最近调用时间)
   - 进化中心: 提案按 `agent_name` 分组显示
   - ChatAgent 总调度 UI: 动态展示 LLM 规划的调用链(类似 AgentOps 的 step tree)
   - 新增 Skill 管理页面(可选, 查看/触发/审批 per-Agent Skill)

7. **`.claude/skills/` 文档同步**:
   - 生成 `SKILL.md` 说明文档(供 Claude Code 助手查阅)
   - 包含每个 Skill 的设计意图、调用方式、参数说明

8. **本地全栈验证**:
   - 单元测试: 每个 Skill 的纯函数逻辑(降级/提案生成/事件订阅)
   - 集成测试: ChatAgent 动态规划调用链、Skill 闭环(提案→闸门→审批→回滚)
   - 本地启动 Docker MySQL + 后端 + 前端, 真实点击验证

9. **服务器同步部署**:
   - 本地验证通过后 rsync 同步到 `81.70.251.90:/opt/code-review/`
   - 执行 `deploy/deploy.sh` 重建容器
   - 健康检查(`/api/health` + 关键 API 抽测)
   - 保留数据库数据(仅 Alembic 迁移)

**不做**:

- 不重构 `audit_service`(操作审计日志)的现有职责
- 不重构圆桌讨论功能(`/api/discuss/*`)
- 不修改现有 `EvolutionAgent.run()` 的对外行为(仅下沉内部逻辑为 Skill 基类方法, 保持 API 兼容)
- 不引入 Celery/Redis/向量数据库(沿用现有同步技术栈, 向量检索列为可选增强)
- 不切换 LLM Provider(沿用 DeepSeek)
- 不做权重级微调(DPO/RFT), 信任敏感场景优先可读可回滚产物
- 不修改 `/api/security/scan*` 独立安全扫描接口的现有行为
- 不重构 `multi_agent.py` 旧模块(已被 AgentRegistry 取代)

### 3.3 14 个 Agent 的 per-Agent 专属进化对象(本期方案)

> 每个 Agent 挂载 1 个 `XxxSelfImprovementSkill` + 1 个 `XxxProactiveSkill`, 共 28 个 Skill 子类。下表只列自进化对象, ProactiveSkill 行为统一为 4 类(进化触发/提问/巡检/学习)的领域特化。

| Agent | 进化对象 | 信号来源 | 产出物 |
|---|---|---|---|
| `code_reviewer` | 审查规则(复用 review_rule) | review_issue.status | 新规则/降级/收窄语言提案(继承现有 EvolutionAgent) |
| `security_sentinel` | 安全静态规则 + 正则秘钥库 | security_scan 命中率 + 误报反馈 | 新增/调整 `security_static_rules` / `security_patterns` 提案 |
| `language_detector` | 语言指纹库(扩展名/关键字/路径模式) | detect 调用日志 + 用户修正 | 新指纹提案、低置信指纹降级 |
| `project_analyzer` | 项目模板(语言/框架/结构识别模板) | analyze 调用日志 + 用户修正 | 模板更新提案 |
| `code_file_manager` | 文件分类/语言识别策略 | 文件入库日志 + 修正 | 分类规则提案 |
| `dashboard` | 指标阈值(评分等级/告警阈值) | 指标趋势 + admin 修正 | 阈值调整提案 |
| `rule_manager` | 规则元数据(分类/严重度映射) | 规则使用统计 | 元数据调整提案 |
| `reporter` | 报告模板(章节/格式/措辞) | 报告生成日志 + 用户反馈 | 模板片段提案 |
| `ai_prompt` | 提示词模板(target_tool 模板) | 生成日志 + 用户采纳率 | 模板优化提案 |
| `project_manager` | 项目元数据模板(默认语言/描述模板) | 创建日志 + 修正 | 模板提案 |
| `review_orchestrator` | 审查编排策略(quick/standard/full 各类型 Agent 调用顺序) | 审查任务结果 + 评分趋势 | 编排策略提案 |
| `evolution` | 进化策略自身(min_samples/阈值/衰减参数) | 提案通过率 + 回滚率 | 进化参数调整提案(元进化) |
| `chat_assistant` | 意图识别 prompt + 调度策略 | 意图识别准确率 + 用户反馈 | prompt 片段/路由策略提案 |
| `orchestrator` | Agent 路由策略(意图→Agent 映射) | 调度成功率 + 耗时 | 路由策略提案 |

### 3.4 项目特性规范对齐

- **数据库**: MySQL 8.0, 严禁 SQLite;`DB_HOST=127.0.0.1`,`DB_PORT=3307`
- **Python 版本**: 3.9,类型注解用 `Optional[X]` 而非 `X | None`, `list[X]` 而非 `List[X]` 在新文件中可用但保持与现有代码风格一致
- **代码规范**: ruff + compileall, 所有函数需函数级注释(功能描述/参数说明/返回值类型及用途)
- **API Key**: `.env` 管理, 不提交 git
- **测试**: 测试优先, 边界覆盖
- **6A 工作流**: 严格走 Align → Architect → Atomize → Approve → Automate → Assess
- **现有代码风格**: 沿用 loguru 日志、SQLAlchemy 2.x ORM、Pydantic v2 schema、httpx 同步客户端

---

## 四、疑问澄清(已在交互中解决)

| 疑问 | 用户回答 |
|---|---|
| "skill" 是 Claude Code 文件还是项目内抽象? | 三者结合: Python Skill 类主体 + `.claude/skills/` 文档 + 元数据扩展 |
| 每个 Agent 的自进化范围? | 全部 14 个 Agent 都做 per-Agent 专属进化 |
| ChatAgent 总调度升级到什么程度? | 双层: 意图分类 + LLM 动态规划 |
| 与现有 EvolutionAgent 的关系? | 保留并下沉为基座, EvolutionAgent 自身也挂载 SelfImprovementSkill |
| ProactiveSkill 具体做什么? | 主动进化触发 + 主动提问/建议 + 主动巡检/发疑 + 主动学习/反思(4 类全做) |
| 触发机制? | 手动 + 定时 + 事件三种全支持 |
| 数据存储策略? | evolution_proposal 加 agent_name + 新增 agent_skill_record 表(两者都做) |
| 前端是否同步改? | 是(Agent 办公室 skills + 进化中心分组 + 总调度 UI) |
| 本地验证方式? | 启动本地全栈(Docker MySQL + 后端 + 前端) |
| 服务器同步方式? | rsync + deploy.sh 重建容器, 保留数据库数据 |
| 是否同步 Claude Skill 文档? | 是(`.claude/skills/` SKILL.md) |
| LLM API Key 来源? | 用现有 `.env` 中的 DeepSeek Key |
| 任务名? | `AgentSkill自进化与总调度升级` |
| 服务器项目路径? | `/opt/code-review` |

---

## 五、关键风险与缓解

| 风险 | 等级 | 缓解措施 |
|---|---|---|
| 14 个 Agent × 2 个 Skill = 28 个 Skill 子类, 工作量大 | 高 | Skill 基类充分抽象, per-Agent 子类只实现 `evolve_target()` / `check_proactive()` 两个钩子; 复用现有 EvolutionAgent 闭环; 按 Agent 优先级分批实现 |
| 下沉 EvolutionAgent 闭环为 Skill 基类可能破坏现有 `/api/evolution` API | 高 | 保持 `EvolutionAgent.run()` 对外签名不变, 内部委托给 `SelfImprovementSkill.evolve()`; 旧测试不能挂 |
| ChatAgent 双层调度可能因 LLM 规划失败导致无法路由 | 中 | 第一层意图分类保留 fallback `chat` intent; 第二层 LLM 规划失败时降级到原有 handler; 加规划超时(默认 10s) |
| `agent_skill_record` 表可能写入频繁(每次 Skill 调用都写)导致表膨胀 | 中 | 异步写入 + 定期归档(>90 天转储); 仅记录元数据不记录大字段 |
| 事件驱动触发可能形成"进化风暴"(一个事件触发多 Agent 同时进化) | 中 | 加全局进化并发限制(同时只跑 N 个 Agent); 事件去抖(debounce 5min) |
| LLM 调用成本上升(per-Agent 进化 + ChatAgent 动态规划) | 中 | 静态规则前置; 经验注入预算 Top-K=3; 进化任务默认离线低频; 支持 `EVOLUTION_LLM_ENABLED=false` 关闭 |
| 服务器重新部署可能中断线上服务 | 中 | 选择业务低峰期部署; `deploy.sh` 支持滚动重建; 部署后健康检查失败自动回滚 |
| Skill 权限边界不清(普通用户能否触发进化?) | 中 | ProactiveSkill 自动触发不区分用户; 手动触发 `/api/agents/{name}/skills/{skill}/invoke` 仅 admin 可调; 写入 `audit_log` |
| 数据库迁移失败(线上数据丢失) | 高 | Alembic 迁移脚本本地验证 3 次; `evolution_proposal.agent_name` 加默认值 `evolution` 兼容旧数据; 服务器部署前手动备份数据库 |
| ChatAgent 第二层 LLM 规划可能"幻觉"调用不存在的 Skill | 中 | 第二层 LLM 输出必须从 `SkillRegistry.list_tools()` 给定的工具列表中选; 不在列表中的调用直接拒绝 |
| 现有 14 种 intent handler 与新双层调度并存可能导致行为不一致 | 中 | 新双层调度作为默认路径, 旧 handler 作为 fallback; 通过 `CHAT_DOUBLE_LAYER_ENABLED=true/false` 环境变量切换 |

---

## 六、进入 CONSENSUS 阶段的准备

已完成项目上下文分析、需求理解确认、边界确认、疑问澄清, 所有关键决策点已与用户达成共识。

下一步:
1. 生成 `CONSENSUS_AgentSkill自进化与总调度升级.md`, 锁定范围、分层取舍、验收口径
2. 中断等待用户最终确认 CONSENSUS 后, 进入 Architect 阶段
3. Architect 阶段生成 `DESIGN_AgentSkill自进化与总调度升级.md`, 包含整体架构图、Skill 基类设计、14 个 Agent Skill 挂载图、双层调度时序图、数据模型、接口契约、异常处理策略
