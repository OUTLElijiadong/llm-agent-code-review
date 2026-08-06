# Agent 项目选择模糊匹配与自定义输入 - 审批清单

- [x] 完整性：覆盖候选不足、项目较多、无匹配和自定义输入。
- [x] 一致性：复用现有 `/api/projects`、Clarify 和后端模糊解析协议。
- [x] 可行性：Element Plus 支持 filterable、remote 和 loading 状态。
- [x] 可控性：仅改 Agent 追问项目控件和纯函数，不改数据库与权限模型。
- [x] 可测性：纯函数可独立单测，生产可通过真实聊天触发追问验收。

结论：批准按 TASK 顺序实施。
