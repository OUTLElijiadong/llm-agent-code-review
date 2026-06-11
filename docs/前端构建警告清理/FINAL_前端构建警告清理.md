# 前端构建警告清理 · Final

## 交付内容

- 将全局 Sass 入口从 `@import` 迁移到 `@use`，消除 Dart Sass `@import` deprecation。
- 将 Google Fonts 加载移动到 `index.html` 的 `<link>`，保留现有字体方案并避免 Sass CSS import。
- 在 Vite SCSS 预处理配置中启用 `modern-compiler` API，消除 legacy JS API deprecation。
- 对 Element Plus 依赖内 `@vueuse/core` 的已知 `INVALID_ANNOTATION` warning 做精确过滤，不屏蔽其他 Rollup warning。
- 配置 `manualChunks`，将 Monaco、ECharts、Element Plus、Vue 栈和常用工具依赖拆分到稳定 vendor chunk。
- 调整已知 Monaco 异步 chunk 的提示阈值，避免生产构建误报。
- 同步清理前端样式中的负 `letter-spacing`，对齐当前前端规范。

## 验证结论

- `npm run build` 成功。
- 构建日志关键词扫描无 warning、deprecation、Rollup、chunk-size 提示。
- 前端样式负字距扫描无命中。
- 浏览器验证通过：登录、Dashboard、SecurityPostureCard、Monaco CodeEditor 加载和保存均正常。
- 浏览器控制台 warn/error 为空。

## 影响范围

- 仅影响前端构建配置、全局样式入口和字体加载方式。
- 不改变后端接口、数据模型和业务逻辑。
- `manualChunks` 保持 Monaco 按编辑器路由懒加载，未并入首屏主包。
