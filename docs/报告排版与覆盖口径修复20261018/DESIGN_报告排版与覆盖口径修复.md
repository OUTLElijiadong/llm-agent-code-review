# 报告排版与覆盖口径修复设计

## 数据与呈现路径

```mermaid
flowchart LR
  A[已有报告 Markdown] --> B[结构化块解析]
  B --> C[网页详情]
  B --> D[原生 PDF 表格]
  B --> E[原生 Word 表格]
  F[任务 coverage 回执] --> G[阶段标签与请求模式]
  G --> C
  G --> D
  G --> E
  H[Token 图表容器] --> I[ResizeObserver]
  I --> J[ECharts resize]
  K[页面卸载] --> L[断开 Observer 并 dispose 图表]
  M[渗透委托列表/详情] --> N[批量检查报告权限与有效关联]
  N --> O{当前账号可读}
  O -->|是| P[返回 can_view_report 与 report_task_id]
  O -->|否| Q[返回 false 与空 report_task_id]
  P --> R[显示报告入口]
  Q --> S[隐藏报告入口]
```

## 模块职责

- `report_markdown.py`：识别 GFM 管道表格，同时保护围栏代码；输出统一 `table` 块。
- `report_pdf_exporter.py`、`report_word_exporter.py`：将表格块转为原生布局，提供表头样式、边框、长文本换行与分页支持。
- `domain_report_exporter.py`：输出请求模式、各验证阶段状态与覆盖边界；未记录具体未扫描项时不宣称“无遗漏”。
- `ReportDetail.vue`：展示阶段明细，区分未纳入与通过；限制 Markdown 内容宽度并折行。
- `AdminOverview.vue`：观察图表自身容器，按变化重排，卸载时释放 observer。
- `report_service.py`：按报告查看权限、来源权限、项目范围、发起人/管理员对象范围和有效报告快照批量判定渗透委托报告可见性。
- `pentest_service.py` 与 `PentestEngagementOut`：序列化 `can_view_report`；无权时不向响应返回 `report_task_id`。
- `PentestList.vue`、`PentestDetail.vue`：只有服务端确认当前账号可读时才渲染报告入口。

## 失败处理

- 非表格 Markdown 保持原解析流程；代码围栏内看似表格的内容不转为表格。
- 未知 coverage 状态显示原状态或“未记录”，不映射为通过。
- ResizeObserver 不存在或图表已销毁时安全跳过重排。
- 发布事务失败时由正式部署脚本按上一应用镜像尝试回滚；数据库不自动降级。
