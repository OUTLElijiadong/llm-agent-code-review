# DESIGN 前端 UX 审计修复

## 方案

```mermaid
flowchart LR
  Audit[UX审计条目] --> Trace[路由与组件追踪]
  Trace --> Contract[前端状态与API契约]
  Contract --> RBAC[角色范围与统计口径核验]
  RBAC -->|有证据的问题| Fix[复用现有组件修复]
  RBAC -->|证据不足/合理差异| Boundary[记录待核与数据范围]
  Fix --> Tests[组件回归与窄屏场景]
  Tests --> Build[Lint 类型检查 构建]
  Build --> Acceptance[逐项验收证据]
  Acceptance --> Gate{生产门禁解除?}
  Gate -->|否| Candidate[候选完成，保留发布阻断]
  Gate -->|是且另有发布指令| Release[独立生产发布流程]
```

## 模块设计

- **入口与职责**：管理路由使用现有 `AdminUnifiedCenter` 分区；任何聚合都只是导航/呈现，不改变后端 workflow、审批处理器或权限。
- **数据契约**：定位各页面 composable/API/service 的角色范围、筛选参数、状态映射和时间窗；不能共享同口径时给统计加标签/辅助说明。
- **术语与引导**：统一中文文案映射；关闭的指南泡泡按当前账号记录，提供设置或帮助入口重新打开。
- **响应式**：复用 `AppSidebar`、`AppHeader` 现有移动抽屉模式和 design tokens；375px 验证遮罩、焦点、关闭、滚动锁及页头溢出。
- **加载状态**：沿用当前 Skeleton/状态组件，在仪表盘首屏只有一个加载表达，错误/空态仍可区分。

## 异常处理

- API 失败保留可见错误和重试路径，不把错误改为空列表或“正常”。
- 无权限与无数据状态分别展示 403/空态，不向角色提供越权的汇总记录。
- 不能从静态代码确定生产账号中的数字归属时，记录待生产复核，不猜测。

## 模块依赖

```mermaid
graph TD
  Routes[Vue Router/RBAC] --> Views[业务页面]
  Views --> ExistingAPI[现有 API 层]
  ExistingAPI --> Backend[既有服务和权限]
  Views --> SharedUI[共享布局/状态/设计系统]
  Tests[前端测试] --> Views
  Tests --> Routes
```
