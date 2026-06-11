# 前端构建警告清理 · Consensus

## 需求描述

清理当前前端构建中的已知警告，并通过构建和浏览器验证证明关键功能可正常使用。

## 技术方案

- Sass：将项目内 `@import './variables.scss'` 迁移为 `@use './variables.scss'`，并通过 Vite Sass 选项消除 legacy JS API deprecation。
- Rollup：仅过滤 Element Plus 依赖中已知的 pure annotation 第三方无害警告，不屏蔽其他警告。
- Chunk：通过 `manualChunks` 拆分 Vue、Element Plus、ECharts、Monaco 编辑器和语言模块，降低单个 chunk 尺寸。
- Monaco：继续保留现有 Worker 配置和编辑器功能，不改变组件 API。

## 技术约束

- 不新增依赖。
- 不牺牲功能来换取无警告。
- 不扩大到无关重构。

## 验收标准

- 前端构建命令退出码为 0。
- 构建输出无 `WARNING`、`warning`、`DEPRECATION WARNING`、`Some chunks are larger`。
- 浏览器验证登录页、仪表盘、代码编辑器页面无明显运行错误。
