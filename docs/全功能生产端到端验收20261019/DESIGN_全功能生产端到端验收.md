# 全功能生产端到端验收：设计

## 总体流程

```mermaid
flowchart TD
  A[报告任务 ReviewTask] --> B[来源与权限过滤 report_service]
  C[领域报告 ReviewReport 快照] --> B
  B --> D[报告列表 API]
  D --> E[前端 ReportList 卡片]
  B --> F[报告详情 API]
  F --> G[在线报告与导出器]
  H[角色权限与项目可见范围] --> B
  I[真实生产 Safari / 用户角色] --> J[逐页点击与滚动验收]
  J --> K[验收矩阵与缺陷记录]
```

## 当前修复设计

- 数据源优先级：`ReviewReport.content_json.coverage.requested_mode`，其次 `ReviewTask.coverage.requested_mode`；兼容 `test_mode` 别名；只接受 `whitebox`、`blackbox`、`combined`。
- 未知、空白、超出允许值的模式不回填，前端继续展示“模式未记录”，不按标题猜模式。
- `source.test_mode` 是列表展示元数据，不改变已有报告内容、状态、权限或模型调用。
- 前端继续复用 `reviewTypeLabel`，避免新建重复映射。
- 沙箱汇总解析识别 `中优先级`、`低优先级` 等严重度分组，但不把分组标题计作发现项；只计入分组里的显式编号发现。`提示`、`提示级`、`待复核`、`未分级` 保持未分级，避免误加到低危。
- Word 领域报告不使用默认大号文档标题样式；长标题居中单段显示为 18pt 宋体，正文 markdown 转换流程不变。

## 依赖与失败处理

- 列表与详情共享已授权报告源；来源快照缺失/不匹配仍由现有后端 fail-closed。
- 生产浏览器发生未登录、权限不足、服务错误或内容不完整，记录为未通过并保留截图/页面证据，不以静态代码推论代替。
- 远程站点测试与代码审查具有不同来源和费用；验收只复用历史数据，不将其互相混作报告类型。
- 官方漏洞源缺少授权接入时，将报告状态标记为“未接入/未知”，不能显示 OWASP 为国内来源匹配。
- 远程评估没有当前账号可读的生产样本时，只记录账号视角的空列表/权限行为；不创建新任务，不发出网络探测或模型调用。

## 模块依赖

```mermaid
flowchart LR
  R[ReportList.vue] --> T[reviewTypeLabel]
  R --> A[report API]
  A --> S[report_service.list_reports]
  S --> Q[ReviewTask.coverage]
  S --> P[ReviewReport.coverage]
  S --> RBAC[对象归属 + 项目可见 + 来源权限]
  S --> D[报告详情/导出]
```
