# 角色分流与注册页优化设计文档

## 架构图

```mermaid
flowchart TD
  Login["Login.vue 登录成功"] --> RoleHome["roleHome.ts 解析角色首页"]
  Guard["router/guards.ts 路由守卫"] --> RoleHome
  Sidebar["AppSidebar.vue 侧栏菜单"] --> RoleHome
  Header["AppHeader.vue 全局搜索"] --> RoleHome
  RoleHome --> Admin["admin -> /admin/users"]
  RoleHome --> Reviewer["reviewer -> /reviews"]
  RoleHome --> User["user -> /projects"]
  Register["Register.vue"] --> PrismUI["Prism 品牌视觉体系"]
```

## 模块设计

| 模块 | 职责 |
|------|------|
| `roleHome.ts` | 统一角色归一化、默认首页、路径访问判断和登录重定向解析 |
| `Login.vue` | 登录成功后调用角色分流逻辑 |
| `guards.ts` | 根路径、登录页、注册页的登录态分流 |
| `AppSidebar.vue` | 依据角色展示对应导航入口 |
| `AppHeader.vue` | 依据角色过滤全局搜索结果 |
| `Register.vue` | 同步 Prism 视觉与交互样式 |

## 数据流

```mermaid
sequenceDiagram
  participant U as 用户
  participant L as Login.vue
  participant S as userStore
  participant R as roleHome.ts
  participant V as Vue Router
  U->>L: 输入账号密码
  L->>S: login()
  S-->>L: profile.role
  L->>R: resolvePostLoginPath(role, redirect)
  R-->>L: 角色首页
  L->>V: replace(path)
```

## 异常处理

- 登录前重定向到管理员路径时，若当前登录用户不是管理员，则回退到该角色默认首页。
- 已登录用户访问 `/login` 或 `/register` 时，若恢复用户信息失败，则清理登录态并允许重新登录。
