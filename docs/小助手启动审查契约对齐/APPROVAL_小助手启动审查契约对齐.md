# 小助手启动审查契约对齐 - 审批清单

- [x] 完整性：覆盖测试失败、真实生产查询缺陷和 Planner 动态引用降级。
- [x] 一致性：状态值与 `CodeFile` ORM、其他服务统一使用 `"active"`。
- [x] 可控性：改动限定在 ChatAssistant、ChatPlanner、Orchestrator 和固定工具契约，不调整 ReviewService。
- [x] 可测性：显式、自动、空数据、动态引用和非法字符串路径可独立验证。
- [x] 可部署性：无迁移，仅需重建 Backend。

结论：允许进入 Automate。
