# CONSENSUS · AgentSkill 自进化与总调度升级

> 在 ALIGNMENT 基础上锁定范围、分层取舍、技术约束与验收口径。
> 所有不确定性已通过与用户交互解决, 本文档为进入 Architect 阶段的最终共识。

---

## 1. 一句话范围

把「Anthropic skill 模块化能力设计」移植到本项目: 在 `backend/app/agents/skills/` 新建 Skill 抽象层, 给 14 个 Agent 各挂载 1 个专属 `SelfImprovementSkill` + 1 个专属 `ProactiveSkill`(共 28 个子类); 把现有 `EvolutionAgent` 的闭环基础设施下沉为 `SelfImprovementSkill` 基类方法; 把 `ChatAssistantAgent` 升级为双层总调度(意图分类 + LLM 动态规划调用链), 可操控任意 Agent 的任意 Skill; 触发机制支持手动/定时/事件三种; 前端同步展示 skills 与调用链; 本地全栈验证 + 服务器同步部署 + `.claude/skills/` 文档同步。

---

## 2. 进化分层与本期取舍

按「可解释性 / 可回滚性 / ROI」从高到低排, 本期取舍:

| 层 | 进化对象 | 本期 | 说明 |
|---|---|---|---|
| **L0 遥测** | Skill 调用日志 | ✅ 做 | 新增 `agent_skill_record` 表, 记录每次 Skill 调用 |
| **L1 经验记忆** | per-Agent 经验库 | 🟡 复用 | 复用现有 `review_experience`, 其它 Agent 暂不新增独立经验表, 用 `agent_skill_record` 兜底 |
| **L2 规则蒸馏** | per-Agent 进化对象 | ✅ 做(核心) | 14 个 Agent 各自蒸馏自己的进化对象(规则/指纹/模板/策略), 复用 `evolution_proposal` 表(加 `agent_name`) |
| **L3 Prompt/few-shot 优化** | system prompt、示例 | 🟡 部分 | 仅 `chat_assistant` / `ai_prompt` / `reporter` 做 prompt 片段进化, 其它 Agent 不动 prompt |
| **L4 工具/技能合成** | Agent 自写检查器 | ⏳ 远期 | 本期不做 |
| **L5 权重级** | 模型参数 | ❌ 非目标 | 不做 DPO/RFT |

**取舍原则**: 能用「可读规则 + 模板片段」进化, 就不用「改权重」进化; 能复用现有表, 就不新建表。

---

## 3. Skill 体系设计共识

### 3.1 Skill 类层级

```
BaseSkill (抽象基类)
├── SelfImprovementSkill (自进化闭环基类, 下沉 EvolutionAgent 闭环)
│   ├── CodeReviewerSelfImprovementSkill
│   ├── SecuritySentinelSelfImprovementSkill
│   ├── LanguageDetectorSelfImprovementSkill
│   ├── ProjectAnalyzerSelfImprovementSkill
│   ├── CodeFileManagerSelfImprovementSkill
│   ├── DashboardSelfImprovementSkill
│   ├── RuleManagerSelfImprovementSkill
│   ├── ReporterSelfImprovementSkill
│   ├── AiPromptSelfImprovementSkill
│   ├── ProjectManagerSelfImprovementSkill
│   ├── ReviewOrchestratorSelfImprovementSkill
│   ├── EvolutionSelfImprovementSkill (元进化)
│   ├── ChatAssistantSelfImprovementSkill
│   └── OrchestratorSelfImprovementSkill
└── ProactiveSkill (主动行为基类, 4 类行为: 进化触发/提问/巡检/学习)
    ├── CodeReviewerProactiveSkill
    ├── ... (14 个对应子类)
```

### 3.2 Skill 基类接口契约(锁定)

**BaseSkill**:
- `name: str` — Skill 唯一标识(如 `code_reviewer.self_improve`)
- `description: str` — Skill 描述(供 LLM 工具列表与前端展示)
- `agent_name: str` — 所属 Agent name
- `invocable: bool` — 是否可被用户/ChatAgent 主动调用(True=可手动触发, False=仅系统自动触发)
- `run(params: dict, ctx: Optional[AgentContext]) -> SkillResult` — 统一调用入口
- `to_tool_schema() -> dict` — 转为 LLM function calling 工具描述(OpenAI tools 格式)

**SelfImprovementSkill**(继承 BaseSkill):
- `evolve_target(db: Session, window_days: int) -> list[dict]` — 抽象方法, 子类实现: 聚合信号 → 产出候选提案(纯函数, 便于单测)
- `apply_proposal(db: Session, proposal: dict) -> int` — 应用提案到进化对象(写表/改配置), 返回 affected_id
- `rollback_proposal(db: Session, proposal_id: int) -> bool` — 回滚提案
- `evaluate_gate(db: Session, proposal: dict) -> dict` — 评估闸门(复用 eval_gate, 或子类自定义基准集)
- `evolve(db: Session, window_days: int = 90, ctx: Optional[AgentContext] = None) -> AgentResult` — 模板方法: aggregate → reflect → gate → persist(默认 pending, 不直接生效)

**ProactiveSkill**(继承 BaseSkill):
- `check_proactive(db: Session, ctx: Optional[AgentContext]) -> list[ProactiveAction]` — 抽象方法, 子类实现: 扫描自身领域, 返回建议行动列表
- 4 类行为子方法(子类按需 override):
  - `should_trigger_evolution(stats) -> bool` — 主动进化触发判定
  - `build_clarify_question(stats) -> Optional[dict]` — 主动提问/建议(复用 clarify_store)
  - `scan_domain(db) -> list[dict]` — 主动巡检/发疑(如 security_sentinel 扫新文件)
  - `reflect_from_logs(db, window_days) -> list[dict]` — 主动学习/反思(从 ai_call_log 挖趋势)
- `run(params: dict, ctx) -> SkillResult` — 模板方法: 根据参数 `action_type` 路由到上述方法

### 3.3 Skill 挂载方式

`BaseAgent` 新增 `_skills: list[BaseSkill]` 实例属性, 通过 `attach_skill(skill)` 方法挂载。`AgentRegistry.list_runtime()` 返回时把 skills 转为结构化对象:

```python
{
    "code": "code_reviewer",
    "skills": [
        {"name": "code_reviewer.self_improve", "description": "...", "invocable": True, "type": "self_improvement"},
        {"name": "code_reviewer.proactive", "description": "...", "invocable": True, "type": "proactive"},
    ]
}
```

### 3.4 SkillRegistry

新建 `backend/app/agents/skills/registry.py`:
- `SkillRegistry.instance()` 单例
- `register(agent_name, skill)` — 注册 Skill
- `get(agent_name, skill_name)` — 获取 Skill
- `list_for_agent(agent_name)` — 列出 Agent 挂载的所有 Skill
- `list_all()` — 列出所有 Skill(供 ChatAgent 工具化)
- `list_tools(agent_name_filter: Optional[str] = None)` — 转为 LLM tools 列表(OpenAI function calling 格式)

---

## 4. 双层总调度共识

### 4.1 第一层: 意图分类(保留并扩展)

保留 `_INTENT_SYSTEM` prompt 与 14 种 intent, **新增 3 种 intent**:
- `evolution_trigger`: 用户想触发某 Agent 自进化("让 code_reviewer 跑一轮自进化")
- `agent_skill_invoke`: 用户想调用某 Agent 的某 Skill("让 security_sentinel 主动巡检")
- `agent_status`: 用户想看 Agent 状态/Skill 列表("code_reviewer 有哪些 skill")

第一层输出仍是 `{intent, reason, payload}`, 但 payload 可包含 `agent_name` / `skill_name` / `action_type` 等字段供第二层使用。

### 4.2 第二层: LLM 动态规划(新增)

在第一层路由后, 调用 `_plan_with_llm(intent, payload, ctx) -> list[ToolCall]`:
- 把 `SkillRegistry.list_tools()` + 该 intent 相关的固定 handler 作为 tools 列表
- 用 LLM function calling 让模型规划调用链(可串联多步: 如 `list_projects` → `start_review` → `audit_security_for_task`)
- LLM 输出 `list[ToolCall]`, 每个 ToolCall = `{tool_name, arguments}`
- 限制: 最多 5 步, 防止无限循环
- 超时: 默认 10s, 超时降级到第一层 handler 单步执行
- 校验: ToolCall 的 tool_name 必须在 tools 列表中, 否则拒绝

### 4.3 调用执行

`_execute_plan(plan: list[ToolCall], ctx) -> AgentResult`:
- 顺序执行每个 ToolCall, 把上一步输出作为下一步输入的上下文
- 每个 ToolCall 调用 `Orchestrator.invoke_tool(tool_name, arguments, ctx)`
- 累积结果, 最终汇总返回给用户
- 失败任一步: 记录错误, 终止后续步骤, 返回已完成步骤的结果

### 4.4 Orchestrator 通用调度接口(新增)

```python
def invoke_tool(self, tool_name: str, arguments: dict,
                ctx: Optional[AgentContext] = None) -> AgentResult:
    """通用工具调用入口, 支持调用任意 Agent 的任意 Skill 或固定方法"""

def invoke_skill(self, agent_name: str, skill_name: str,
                 params: dict, ctx: Optional[AgentContext] = None) -> AgentResult:
    """调用指定 Agent 的指定 Skill"""

def list_agent_skills(self, agent_name: str) -> list[dict]:
    """列出 Agent 挂载的所有 Skill 元数据"""

def trigger_evolution(self, agent_name: str = "evolution",
                      window_days: int = 90,
                      ctx: Optional[AgentContext] = None) -> AgentResult:
    """触发指定 Agent 的自进化(默认 evolution Agent)"""
```

### 4.5 兼容性

- 环境变量 `CHAT_DOUBLE_LAYER_ENABLED=true` (默认) 控制双层调度开关
- 关闭时回退到原有单层 intent handler 模式
- 旧 `/api/ai_chat` 端点签名不变, 仅内部行为升级

---

## 5. 数据模型共识

### 5.1 `evolution_proposal` 表扩展(已有表加字段)

新增字段:
- `agent_name: String(50), nullable=False, default="evolution", index=True` — 提案来源 Agent name

Alembic 迁移: `evolution_proposal.agent_name` 加默认值 `evolution` 兼容旧数据。

### 5.2 `agent_skill_record` 表(新增)

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | BigInteger PK | 主键 |
| `agent_name` | String(50), index | 哪个 Agent |
| `skill_name` | String(100), index | 哪个 Skill |
| `trigger_type` | String(20) | `manual` / `scheduled` / `event` / `proactive` |
| `trigger_source` | String(100) | 触发来源(如 `event:REVIEW_ISSUE_STATUS_CHANGED` / `scheduler:cron:0 3 * * *` / `user:admin#1`) |
| `input_params` | Text(JSON) | 输入参数(脱敏) |
| `output_summary` | Text | 输出摘要(≤500字, 防止表膨胀) |
| `effect` | String(20) | `success` / `failed` / `no_op` / `proposal_created` |
| `duration_ms` | Integer | 执行耗时 |
| `created_at` | DateTime, index | 创建时间 |
| `created_by_user_id` | BigInteger nullable | 触发用户(manual 模式) |

继承 `IdMixin + TimestampMixin`。索引: `(agent_name, created_at)`、`(skill_name, effect)`。

### 5.3 不新增表

- per-Agent 经验记忆本期不新建表, 用 `agent_skill_record` 兜底记录历史
- `eval_case` 黄金集复用, per-Agent 自进化闸门可在 `eval_case.tags` 里标记所属 Agent

---

## 6. 触发机制共识

| 触发方式 | 入口 | 权限 | 频率 |
|---|---|---|---|
| 手动 | `POST /api/agents/{name}/skills/{skill}/invoke` | admin only, 写 audit_log | 用户控制 |
| 手动(进化) | `POST /api/evolution/trigger?agent_name=xxx` | admin only | 用户控制 |
| 定时 | `scheduler_service` 注册 per-Agent cron | 系统自动 | 默认每日 03:00 跑 evolution, 每小时跑 proactive_check |
| 事件 | `event_bus` 订阅 | 系统自动 | 去抖 5min, 全局并发限制 N=3 |

事件订阅清单:
- `REVIEW_ISSUE_STATUS_CHANGED` → 触发 `code_reviewer.self_improve`
- `SECURITY_SCAN_COMPLETED` → 触发 `security_sentinel.self_improve`
- `AI_CALL_THRESHOLD_REACHED` → 触发 `orchestrator.self_improve`(路由策略)
- `EVOLUTION_PROPOSAL_PROMOTED` → 触发 `evolution.self_improve`(元进化)

---

## 7. 验收口径

### 7.1 Skill 体系验收

| 编号 | 验收项 | 口径 |
|---|---|---|
| A1 | Skill 基类 | `BaseSkill` / `SelfImprovementSkill` / `ProactiveSkill` 三个基类已实现, 接口契约与 §3.2 一致 |
| A2 | Skill 挂载 | 14 个 Agent 各挂载 1 个 SelfImprovementSkill + 1 个 ProactiveSkill, 共 28 个子类 |
| A3 | SkillRegistry | `list_for_agent` / `list_all` / `list_tools` 三方法可用, `list_tools` 输出符合 OpenAI tools 格式 |
| A4 | 元数据扩展 | `BaseAgent.skills` 元数据扩展为结构化对象, `/api/agents/runtime` 返回每个 Agent 的 skills 列表 |
| A5 | EvolutionAgent 兼容 | `EvolutionAgent.run()` 对外签名不变, 内部委托 `SelfImprovementSkill.evolve()`, 现有 `/api/evolution` 端点测试全通过 |
| A6 | 防翻车 | 28 个 Skill 子类均复用双门槛(min_samples≥20, min_distinct_tasks≥2)与人工闸门, 不自动生效 |

### 7.2 双层调度验收

| 编号 | 验收项 | 口径 |
|---|---|---|
| B1 | 第一层意图分类 | 新增 `evolution_trigger` / `agent_skill_invoke` / `agent_status` 三种 intent, 现有 14 种 intent 行为不变 |
| B2 | 第二层 LLM 规划 | `_plan_with_llm` 能输出 `list[ToolCall]`, 最多 5 步, 超时 10s 降级到单步 handler |
| B3 | 工具调用 | `Orchestrator.invoke_tool` / `invoke_skill` / `list_agent_skills` / `trigger_evolution` 四方法可用 |
| B4 | 链式调用 | 能串联多步(如 list_projects → start_review → audit_security_for_task), 上一步输出作为下一步上下文 |
| B5 | 兼容回退 | `CHAT_DOUBLE_LAYER_ENABLED=false` 时回退到原有单层 handler, 行为与升级前一致 |
| B6 | 幻觉防护 | LLM 输出的 tool_name 必须在 `SkillRegistry.list_tools()` 中, 不在的直接拒绝 |

### 7.3 数据库与触发验收

| 编号 | 验收项 | 口径 |
|---|---|---|
| C1 | 迁移脚本 | `003_agent_skill_evolution.py` 在本地 MySQL 跑通, `evolution_proposal.agent_name` 字段已加且旧数据默认 `evolution` |
| C2 | 新表 | `agent_skill_record` 表结构符合 §5.2, 索引齐全 |
| C3 | 手动触发 | `POST /api/agents/code_reviewer/skills/code_reviewer.self_improve/invoke` 能触发并写入 `agent_skill_record` |
| C4 | 定时触发 | `scheduler_service` 能按 cron 触发, `agent_skill_record.trigger_type='scheduled'` |
| C5 | 事件触发 | `event_bus.emit(REVIEW_ISSUE_STATUS_CHANGED)` 后 5min 内触发 `code_reviewer.self_improve`, `trigger_type='event'` |
| C6 | 并发限制 | 全局同时只跑 N=3 个 Agent 进化, 超出的排队 |

### 7.4 前端验收

| 编号 | 验收项 | 口径 |
|---|---|---|
| D1 | Agent 办公室 | 每个 Agent 卡片展示 skills 列表(名称+描述+类型+invocable 标识) |
| D2 | 进化中心 | 提案列表按 `agent_name` 分组显示, 支持按 Agent 筛选 |
| D3 | 总调度 UI | ChatAgent 聊天界面动态展示 LLM 规划的调用链(step tree, 含每步 tool_name/状态/耗时) |
| D4 | Skill 管理 | 新增 Skill 管理页面: 查看 per-Agent Skills, admin 可手动触发, 查看调用历史 |

### 7.5 服务器同步验收

| 编号 | 验收项 | 口径 |
|---|---|---|
| E1 | rsync 同步 | 本地代码已 rsync 到 `81.70.251.90:/opt/code-review/` |
| E2 | 容器重建 | `deploy/deploy.sh` 重建成功, 无错误 |
| E3 | 数据库迁移 | 服务器 MySQL 已跑 Alembic 迁移, 数据未丢失 |
| E4 | 健康检查 | `https://lijiadong.cn/api/health` 返回 200, 关键 API(`/api/agents/runtime` / `/api/evolution/proposals`)抽测通过 |

### 7.6 文档同步验收

| 编号 | 验收项 | 口径 |
|---|---|---|
| F1 | `.claude/skills/` | 每个 Skill 一个 SKILL.md(共 28 个 + 1 个总览 README.md), 含设计意图/调用方式/参数说明 |
| F2 | 6A 文档 | ALIGNMENT / CONSENSUS / DESIGN / TASK / ACCEPTANCE / FINAL / TODO 全套文档已生成 |

---

## 8. 关键指标(Metrics)

- **Skill 调用**: 每个 Skill 的调用次数、成功率、平均耗时(`agent_skill_record` 聚合)
- **进化健康**: per-Agent 提案数、通过率、回滚率、promote 后效果(采纳率提升幅度)
- **总调度**: ChatAgent 双层调度命中率、平均规划步数、超时降级率
- **成本**: LLM 调用 token 趋势(per-Agent 进化 + ChatAgent 规划的 token 占比)

---

## 9. 风险与红线

### 9.1 红线(不可越界)

1. **不破坏现有 EvolutionAgent API**: `EvolutionAgent.run()` 签名与行为必须兼容, 现有 `/api/evolution/*` 端点测试全通过
2. **不自动生效**: 所有 28 个 Skill 子类产出的提案默认 `status=pending`, 必须 admin 审批才 promote
3. **不引入新中间件**: 不引入 Celery/Redis/向量数据库, 沿用现有同步技术栈
4. **不切换 LLM Provider**: 沿用 DeepSeek
5. **不做权重级微调**: 不做 DPO/RFT
6. **服务器数据不丢**: 部署前必须手动备份数据库, Alembic 迁移失败立即回滚

### 9.2 风险对策

| 风险 | 对策 |
|---|---|
| 28 个 Skill 子类工作量过大 | 基类充分抽象, 子类只实现 `evolve_target` / `check_proactive` 两个钩子; 按 Agent 优先级分批实现(code_reviewer/security_sentinel/evolution 优先) |
| ChatAgent 双层调度 LLM 规划不稳定 | 第一层 fallback `chat` intent; 第二层超时降级; `CHAT_DOUBLE_LAYER_ENABLED` 环境变量开关 |
| 事件驱动进化风暴 | 全局并发限制 N=3; 事件去抖 5min |
| LLM 成本上升 | 静态规则前置; 经验注入 Top-K=3; `EVOLUTION_LLM_ENABLED=false` 可关 |
| 数据库迁移失败 | 本地验证 3 次; `agent_name` 默认值兼容; 部署前备份 |

---

## 10. 技术约束与集成方案

### 10.1 技术栈约束

- Python 3.9, 类型注解 `Optional[X]` / `List[X]` / `Dict[X, Y]`(沿用现有风格)
- SQLAlchemy 2.x ORM, Pydantic v2 schema
- httpx 同步客户端(不引入 async)
- loguru 日志
- FastAPI 路由, dependency injection
- MySQL 8.0, 严禁 SQLite(测试环境可用 SQLite 兼容)
- ruff + compileall 代码规范
- 所有函数需函数级注释

### 10.2 集成方案

**Skill 层与 Agent 层集成**:
- `BaseAgent.__init__` 末尾调用 `self._init_skills()`(子类 override 挂载专属 Skill)
- `BaseAgent.attach_skill(skill)` 方法注册到 `SkillRegistry`
- `AgentRegistry.list_runtime()` 调用 `SkillRegistry.list_for_agent(name)` 把 skills 元数据合并到 Agent 元数据

**Skill 层与 EvolutionAgent 集成**:
- `SelfImprovementSkill.evolve()` 模板方法封装现有 EvolutionAgent 的七步闭环
- `EvolutionAgent.run()` 内部委托给 `self._self_improve_skill.evolve()`
- 现有 `evolution_service` / `feedback_service` / `eval_gate` 被 `SelfImprovementSkill` 基类调用

**双层调度与现有 ChatAgent 集成**:
- `ChatAssistantAgent.execute()` 在意图识别后, 调用 `_plan_with_llm` 规划
- 旧 `_handle_xxx` 方法保留, 作为单步 ToolCall 暴露给 LLM 规划
- 新增 `_handle_evolution_trigger` / `_handle_agent_skill_invoke` / `_handle_agent_status` 三种 intent handler

**触发机制集成**:
- 手动: 新增 `/api/agents/{name}/skills/{skill}/invoke` 路由, 复用 `get_request_orchestrator`
- 定时: `scheduler_service` 注册新任务, 调用 `Orchestrator.trigger_evolution(agent_name)`
- 事件: `event_bus` 新增订阅入口 `subscribe_event(event_type, agent_name, skill_name)`

**前端集成**:
- `AgentOffice.vue` 渲染 skills 列表(从 `/api/agents/runtime` 取)
- `EvolutionCenter.vue` 提案列表按 `agent_name` 分组
- `ChatAssistant.vue` 新增调用链 step tree 组件
- 新增 `SkillManager.vue` 页面

---

## 11. 任务边界限制

### 11.1 时间边界

- 起始: 2026-06-25
- 完成目标: 严格按 6A 工作流推进, 不允许延期

### 11.2 范围边界

- 严禁扩展到 14 个 Agent 之外(不新增 Agent)
- 严禁修改 `audit_service` / 圆桌讨论 / `multi_agent.py` 旧模块
- 严禁引入 Celery/Redis/向量数据库/新 LLM Provider

### 11.3 资源边界

- 复用现有 `.env` 中的 DeepSeek API Key
- 复用现有 Docker MySQL 容器
- 复用现有 deploy.sh 部署脚本

---

## 12. 确认所有不确定性已解决

| 不确定性 | 解决方式 |
|---|---|
| Skill 是文件还是抽象 | 三者结合, Python 类主体 + .claude/skills/ 文档 + 元数据扩展(已确认) |
| 进化范围 | 14 个 Agent 全做(已确认) |
| 调度升级程度 | 双层(已确认) |
| 与现有 EvolutionAgent 关系 | 保留并下沉为基座(已确认) |
| ProactiveSkill 行为 | 4 类全做(已确认) |
| 触发机制 | 手动+定时+事件(已确认) |
| 数据存储 | evolution_proposal 加字段 + 新表(已确认) |
| 前端 | 同步改(已确认) |
| 服务器同步 | rsync + deploy.sh(已确认) |
| Claude 文档 | .claude/skills/ 同步(已确认) |
| 任务名 | AgentSkill自进化与总调度升级(已确认) |
| 服务器路径 | /opt/code-review(已确认) |
| per-Agent 进化对象 | §3.3 ALIGNMENT 已列(可调整) |

**所有关键决策点已与用户达成共识, 进入 Architect 阶段。**
