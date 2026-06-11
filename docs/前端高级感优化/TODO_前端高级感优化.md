# 前端高级感优化 · TODO

## 待办

1. 全站页面继续统一：ProjectList、ReviewTaskDetail、SecurityCenter、ReportDetail 等页面仍有各自独立卡片和标题样式，可继续接入 `prism-page-shell` / `prism-surface`。
2. Sass 迁移：将 `@import './variables.scss'` 迁移到 Sass `@use`，同时处理 Dart Sass legacy API warning。
3. 打包优化：Monaco、ECharts、Element Plus 当前 chunk 较大，可通过 `manualChunks` 或按需拆分优化首屏加载。
4. 图标体系继续收敛：部分页面仍有文本符号或局部硬编码颜色，建议后续按页面逐步替换为 Element Plus 图标和 Prism token。

## 需要支持

- 如需做第二阶段全站视觉统一，需要确认优先页面顺序：建议 `ProjectList → ReviewTaskDetail → SecurityCenter → ReportDetail`。
