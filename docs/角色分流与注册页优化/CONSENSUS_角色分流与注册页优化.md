# 角色分流与注册页优化共识文档

## 明确需求

1. 登录成功后根据角色进入不同页面。
2. 已登录用户访问 `/login`、`/register` 时，自动回到自己的角色首页。
3. 访问根路径 `/` 时，根据当前登录角色分流。
4. 侧栏和全局搜索入口按角色展示，避免所有角色看到同一套入口。
5. 注册页使用与登录页一致的 Prism 品牌视觉、表单控件和响应式布局。

## 验收标准

- `admin` 登录后默认进入 `/admin/users`。
- `reviewer` 登录后默认进入 `/reviews`。
- `user` 登录后默认进入 `/projects`。
- 非管理员登录时不会跳入 `/admin/*` 重定向目标。
- 注册页视觉与登录页同属 Prism 棱镜风格，移动端无明显遮挡和溢出。
- `npm run build` 通过。

## 技术方案

- 新增 `frontend/src/utils/roleHome.ts` 统一维护角色归一化、默认首页和登录后重定向解析。
- 修改 `Login.vue` 登录成功跳转逻辑，使用角色首页替代固定 `/dashboard`。
- 修改 `router/guards.ts`，处理已登录用户访问登录/注册页与根路径的角色分流。
- 修改 `AppSidebar.vue` 和 `AppHeader.vue`，按角色裁剪侧栏与全局搜索入口。
- 重做 `Register.vue`，复用 Prism 暗色品牌区、光谱强调色、统一表单交互和移动端单栏布局。

## 技术约束

- 保持现有 Vue3 Composition API 与 Element Plus 写法。
- 保持所有新增函数的函数级注释。
- 不引入新依赖。
