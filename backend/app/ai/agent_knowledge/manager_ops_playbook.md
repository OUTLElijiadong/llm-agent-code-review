# Prism 管理员 Agent · 运维操作知识库(完整版)

> 这是你的运维手册。回答"系统状态/怎么排查/什么流程/批量操作"时优先引用本库并注明"按运维手册"，给管理页站内链接。**凡涉及实时数值，必须调用对应实时能力取最新值，不要凭记忆报数**；执行页面操作前先 `admin_describe_capabilities` 查精确参数，再 `admin_execute_capability`。

## 一、实时数据怎么取(接实时变化数据)

要当前快照就调工具，标注"以下为实时数据"并给来源页链接：
- 系统运行状态(CPU/内存/磁盘/容器) → `overview.system`(或固定工具 admin_system_status)。
- 安全态势 → `overview.security`；登录来源地理分布 → `overview.geo`；Agent 活跃 → `overview.agent_activity`。都在 [总览大屏](/admin/overview)。
- 用户/项目/审查/问题 计数与列表 → 列表类能力(如 users.list)，注意翻页取全并核对总数。
- Agent 治理状态 → `governance.overview`、`governance.agents.list`。
- 沙箱 worker 占用/健康 → `sandbox.workers.list`、`sandbox.workers.health`。
- Agent 告警 → `observability.alerts.list`、`observability.overview`。
- AI 调用记录 → `ai_logs.list`、`ai_logs.get`。
- 工具调用记录 → `tools.calls.list`；策略决策记录 → `policies.decisions.list`。

## 二、各管理页职责(25 页)

- 总览大屏 /admin/overview：系统状态、安全态势、Agent 活跃、地理分布。
- Agent 管理 /admin/agents：治理档案(状态/预算/优先级/自动审批阈值)，governance.*。
- 审批中心 /admin/approvals：统一处理待审批(approvals.list/approve/reject)。
- Agent 发布审批 /admin/agent-releases：批准/驳回/停用/回滚已发布 Agent(agent_releases.*)。
- 内测码 /admin/beta-codes：生成/撤销一次性内测码。
- 策略中心 /admin/policies：治理策略增改、试算、决策记录。
- 工具权限 /admin/tools：Agent 工具调用记录与权限配置。
- 知识与记忆 /admin/knowledge：Agent 知识文档/记忆/知识来源治理，激活待审批知识(knowledge.docs.activate)。
- 任务调度 /admin/jobs：Agent 定时任务管理与触发。
- 监控告警 /admin/observability：Agent 可观测总览与告警处理。
- 奖惩趋势 /admin/rewards：Agent 奖惩事件记录。
- 回滚中心 /admin/rollback：治理制品版本与回滚。
- 用户管理 /admin/users：查询/启停/重置密码/软删除/设历史角色(users.*)。
- RBAC /admin/rbac/{roles,permissions,users}：角色、权限点、用户角色与数据范围。
- AI 调用日志 /admin/ai-logs。
- 报告模板 /admin/report-templates。
- 审计 /admin/audit：系统操作审计日志(audit.list)。
- 自进化 /admin/evolution：反馈信号、进化提案审批/评测/回滚、触发进化。
- Skill 管理 /admin/skills：Skill 清单与调用记录、手动调用。
- RAG 嵌入配置 /admin/embedding：嵌入模型配置(embedding.config.*)。
- MCP 与沙箱节点 /admin/mcp-workers：MCP Server/工具/绑定、沙箱 Worker 管理。
- 大模型配置 /admin/llm：全局 LLM 配置查看/测试/更新(llm.config.*)。

## 三、故障排查流程

### 沙箱「没有可用的隔离 worker」(50301)
1. `sandbox.workers.list`/`health` 查 worker 是否 healthy、并发上限。
2. 查是否有「就绪」deploy 占着单并发槽 → 关闭释放。
3. 查 executor 服务(prism-sandbox-executor)是否在跑。
4. 报「镜像 digest 校验失败」：executor 与后端可能是两套 release，profiles.json 各一份，需在 executor 所在 release 重新固化 digest(pin-profiles)。

### LLM 503 / 上游过载
平台已有限重试。持续失败：`llm.config.test` 测连通；查上游配额；容器内直连测 503 频率。配置在 [大模型配置](/admin/llm)。

### 审查/Agent 卡住
`ai_logs.get` 看该 run 检查点与最近事件；`observability.alerts.list` 看告警；必要时 `jobs.run` 重跑或回收。

### 权限/越权投诉
- `rbac.users.permissions.get` 查该用户有效权限，`rbac.users.roles.get` 查角色。
- 内置规则启停、生产 JWT_SECRET 等安全红线见 [策略中心](/admin/policies)。

## 四、批量操作流程(固化流程)

处理"所有/批量/全部/这些"类请求，严格按此：
1. 用列表类能力查清完整候选(翻页取全，page_size 取大，核对总数)。
2. `ask_user` 展示统计口径与候选数量，等确认。
3. 逐条或分页执行；每条记录成功/失败/跳过原因。
4. 汇总成功/失败/跳过条数与原因，给结果页链接。

## 五、发布审批流程(固化流程)
1. 查完整详情：修改前后内容、依赖、测试证据、风险(agent_releases.approvals.list + 详情)。
2. 展示影响面给管理员。
3. 申请执行决策(agent_releases.approve/reject)，不擅自发布。

## 六、Agent 知识治理流程(固化流程)
1. 新增/抓取知识(knowledge.docs.create / sources.crawl)。
2. 高风险或低置信知识进入待审批。
3. 审批通过后 knowledge.docs.activate 激活生效。

## 七、危险操作红线(必须先审批)
- users.delete(软删除)、users.reset_password、批量删除、生产运维命令、agent_releases.* 的批准/驳回/停用/回滚、governance.agents.update、policies.upsert、llm.config.update、evolution.run/proposals 审批、rbac 角色/权限/数据范围变更、sandbox.workers.* 变更、mcp.servers.* 变更。
- **项目归属**：管理员对话中可查看全部项目（监管），但**写操作仅限管理员自有项目**；用户项目只读，不要把用户项目当作管理员自己的项目来改/删。
- 用户说"第几条/序号"时，不得猜为用户 ID：必须 `ask_user` 区分序号与 ID，拿到精确 user_ids 再执行批量工具。
- 写操作会被系统暂停展示审批；用户批准后系统自动把结果交还，不要让用户重复发指令。

## 八、交互规范(务必遵守)
- **结束语**：每完成一项任务，用一句话收尾——做了什么、结果在哪个页面、下一步建议。
- **跳前先问**：需要引导去某管理页时，给导航按钮或站内链接，**由管理员点击后才跳转**，不自动跳、不关闭面板。
