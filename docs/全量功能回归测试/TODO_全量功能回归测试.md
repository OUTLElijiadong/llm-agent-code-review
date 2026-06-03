# 全量功能回归测试 · 待办

## 需要支持

1. 恢复应用内 Browser 路由能力，避免后续全量测试依赖 Safari Computer Use 兜底。
2. 如需覆盖 Agent 自进化最终审批/回滚、真实账号禁用/重置密码，请提供隔离账号或隔离库。

## 非阻塞优化

1. 迁移 Sass `@import` 和 legacy JS API。
2. 拆分 Monaco Editor 等大体积构建 chunk。
3. 增加 API 路由集成测试，提高当前 `38%` 覆盖率。
