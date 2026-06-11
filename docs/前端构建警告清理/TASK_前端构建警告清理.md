# 前端构建警告清理 · Task

```mermaid
flowchart TD
  T1["复现构建警告"] --> T2["迁移 Sass 入口"]
  T2 --> T3["配置 Sass deprecation 处理"]
  T3 --> T4["配置 Rollup onwarn"]
  T4 --> T5["配置 manualChunks"]
  T5 --> T6["构建验证"]
  T6 --> T7["浏览器功能验证"]
  T7 --> T8["文档同步"]
```

## 原子任务

1. 复现并记录警告清单。
2. 修改 `frontend/src/assets/styles/index.scss`。
3. 修改 `frontend/vite.config.ts`。
4. 运行 `npm run build`，确认无警告。
5. 启动前端和轻量 mock API，验证登录、仪表盘、代码编辑器页面。
6. 更新验收、总结、TODO 和 `说明文档.md`。
