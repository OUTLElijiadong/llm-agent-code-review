# FINAL - 小菱多智能体总调度体系

## 1. 交付结论

小菱已纳管现有项目 Agent 能力并上线持久化 Agent Mesh：可发现、可寻址、可投递、可回执、可重试、可追踪、可在同账户不同会话间自动唤醒，且用户端和管理端均可在调用链中查看子 Agent 对话，默认折叠。

本次没有重复创建用户需求中举例的虚拟 Agent。页面操作、报错、数据分析、预警、运维、汇总等能力均映射到项目现有 runtime/service Agent、工具网关和审计链；生产实际内置 32 个 Agent（14 runtime + 18 service），动态发现 2 个 custom 和 5 个 session。

## 2. 核心实现

- 新增 `agent_mesh_conversation`、`agent_mesh_message`、`agent_mesh_message_event` 三张账本表和 Alembic `030`。
- 新增严格 Pydantic 消息体、地址解析、同账户隔离、幂等键、ACK 状态机、过期/重试/dead-letter、trace 恢复和会话心跳。
- 扩展 Responses 工具循环的 `ListAgents`、`SendMessage`，协作上下文走隐藏 system input，禁止伪造用户消息。
- 普通用户端 `AgentChatDrawer`、管理端 `AdminCopilot` 共用 Mesh 时间线恢复和去重逻辑。
- 修复本次构建暴露的 sandbox 类型与管理员路由测试契约，未扩大业务范围。

## 3. 发布与回滚

生产发布使用精确源码和镜像 SHA，先保留备份 tag `backup/pre-xiaoling-agent-mesh-20260810` 与两份 MySQL gzip 备份，再执行 029→030 迁移和前端切换。新表为旁路新增，旧版本可继续运行；回滚目标为发布前 backend/frontend 镜像及备份文件，未执行 destructive downgrade。

## 4. 证据索引

- 需求与边界：`ALIGNMENT_小菱多智能体总调度体系.md`、`CONSENSUS_小菱多智能体总调度体系.md`
- 架构与接口：`DESIGN_小菱多智能体总调度体系.md`
- 原子任务与依赖：`TASK_小菱多智能体总调度体系.md`
- 审批记录：`APPROVAL_小菱多智能体总调度体系.md`
- 逐项实测：`ACCEPTANCE_小菱多智能体总调度体系.md`

## 5. 后续建议

当前生产配置无本任务阻塞项。建议下一维护窗口处理项目既有全仓 Ruff 债务，并为离线会话的待投递消息增加定时过期巡检指标；两项均不影响当前小菱功能闭环。
