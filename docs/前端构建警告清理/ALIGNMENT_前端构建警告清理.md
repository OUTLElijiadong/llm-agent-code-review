# 前端构建警告清理 · Align

## 原始需求

用户要求消除前端构建中的警告，确保无报错，且功能都正常使用。

## 项目上下文

- 前端使用 Vite 5、Vue3、TypeScript、Element Plus、ECharts、Monaco Editor、Sass。
- 上一轮构建通过但存在警告：
  - Sass `@import` deprecation。
  - Dart Sass legacy JS API deprecation。
  - Rollup 对 Element Plus 内部 `@vueuse/core` pure annotation 的第三方警告。
  - Monaco 相关 chunk 超过 Vite 默认大小阈值。

## 任务边界

- 仅处理前端构建警告和关键功能验证。
- 不改变后端接口、数据库和业务逻辑。
- 避免触碰当前工作树中与本任务无关的后端改动。

## 验收标准

- `npm run build` 通过。
- 构建输出不再出现 warning/deprecation/chunk size warning。
- 登录页、仪表盘、代码编辑器相关路由可正常加载。
