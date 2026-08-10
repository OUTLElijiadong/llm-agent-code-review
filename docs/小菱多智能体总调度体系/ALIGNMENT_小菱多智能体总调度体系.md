# ALIGNMENT - 小菱多智能体总调度体系

> 阶段：Align
> 日期：2026-08-10
> 状态：已对齐
> 服务器：`81.70.251.90:/opt/code-review`

## 1. 原始需求

将小菱建设为同一账户范围内的全局总调度智能体，统一发现和调度项目中的全部运行时 Agent、服务型 Agent、已发布自定义 Agent与并行会话；实现结构化消息、跨会话投递、回执重试、追踪审计、冲突协调和结果收敛，并在小菱界面默认折叠展示 Agent 间对话。

## 2. 已确认边界

- 直接完成设计、开发、测试、生产部署和真实验收。
- 同一账户下的不同用户端/管理端对话可以发现和通信，不跨账户、跨租户投递。
- 小菱可使用最高管理员已有能力，但继续经过项目现有 RBAC、工具白名单、审批、审计和运维执行器；不开放模型任意 Shell、SQL、文件路径或服务访问。
- 全部 Agent 共享项目级公共知识能力；用户私有数据仍受现有用户、项目成员和管理员隔离规则约束。
- DeepSeek 使用项目现有 `.env`；生产已核实 `DEEPSEEK_BASE_URL=https://api.deepseek.com`、`DEEPSEEK_MODEL=deepseek-v4-flash`，密钥只做存在性检查。
- 任务目录固定为 `docs/小菱多智能体总调度体系/`。

## 3. 项目事实

### 3.1 技术与运行基线

- 后端：FastAPI、SQLAlchemy、Alembic、MySQL 8、Redis。
- 前端：Vue 3、TypeScript、Element Plus、SSE。
- Agent 运行：DeepSeek Responses 兼容运行时、工具循环、审批恢复、请求级 Orchestrator。
- 生产：五个容器 `cr_backend/cr_frontend/cr_mysql/cr_redis/cr_clamav` 均健康，Alembic 为 `030 (head)`。
- 风险：生产源码分支、release 指针与容器 release 标识不一致；磁盘使用率 80%。发布必须使用精确文件清单、数据库备份和可回滚镜像，不得以本地工作树覆盖生产仓库。

### 3.2 现有能力

- `AgentRegistry`：注册表包含 17 个类级定义；按 `contracts.py` 去重并以最终可寻址类型归类后，生产 `ListAgents` 返回 14 个 runtime。
- `contracts.py`：32 个 Agent 职责契约，最终归类为 14 个 runtime 与 18 个确定性 service。
- `MetaGPT Environment/Role/Message`：具备进程内定向消息、协作白名单和消息字段校验。
- `AgentResponseRun`：保存同一用户、surface、session 的 Responses 检查点。
- `AgentEventBus`：支持 Redis relay 和按用户隔离的 SSE。
- 管理/普通用户工具网关：支持固定工具、动态 Skill、已发布自定义 Agent、管理员能力、受控运维和 MCP。

### 3.3 当前缺口

1. MetaGPT 消息只存在单次进程内 Environment，不是跨会话持久化消息总线。
2. 会话清单存于浏览器 `localStorage`，服务器无法列出同账户的其他对话或判断其在线状态。
3. 现有 `list_agents` 只返回运行时注册表，不返回服务型 Agent、自定义 Agent和会话。
4. 没有 `SendMessage` 固定工具、收件箱、ACK、幂等、超时、重试和状态流转。
5. Agent 间消息没有统一持久化追踪页，不能在刷新后还原对话链。
6. 32 份职责契约已经覆盖用户举例中的运维、监控告警、数据一致性、事件响应、报告汇总等能力，不应重复创建功能重叠的伪 Agent。

## 4. 全量 Agent 纳管口径

| 层级 | 数量 | 纳管方式 |
|---|---:|---|
| 小菱入口 | 2 个 surface | `chat_assistant`（用户端）与 `manager`（管理端）共用小菱人格、会话与消息协议 |
| 核心编排 | 1 | `orchestrator` 负责路由、依赖、结果归并 |
| 真实运行时专业 Agent | 14 | `chat_assistant`、`orchestrator`、语言、项目、审查、安全、文件、报表、规则、进化、沙箱等现有实例 |
| 确定性服务型 Agent | 18 | `manager`、运维、审批、策略、调度、记忆、知识蒸馏、监控、反思、告警、测试验证、沙箱部署、质量/模型/报告/数据校验、成本和事件响应 |
| 动态成员 | 运行时变化 | 已发布自定义 Agent与同账户已注册会话 |

生产实测 `ListAgents` 返回 `runtime=14/service=18/custom=2/session=5`，合计 39 个可寻址对象；其中内置 Agent 为 32 个（14 runtime + 18 service），自定义 Agent 和会话按账户动态变化。用户示例能力按真实项目复用：页面操作由用户/管理员能力网关承担；报错处理由告警与事件响应链承担；数据分析由 Dashboard 与 DataIntegrity 承担；预警预判由 Monitor、Alert、Reflection、Evolution 闭环承担；运维由 Operations 承担；信息汇总由 Orchestrator 与 Reporter 承担。

## 5. 不纳入范围

- 不伪造 DeepSeek 原生不存在的 Responses、1M 或跨会话能力；继续使用项目内已实现的兼容层和配置值，并以真实接口验证为准。
- 不实现跨账户消息、公共互联网 Agent 发现、任意主机命令或绕过审批的高危操作。
- 不将预测结果未经真实故障标签校准就宣传为机器学习准确率；本次实现可追溯反馈闭环和数据入口，不编造准确率。

## 6. 已解决问题

用户已确认实施目标、服务器授权、同账户边界、最高管理员能力边界、共享知识粒度、模型配置来源和任务名。剩余技术细节均可由现有架构和测试契约确定，无需继续中断。
