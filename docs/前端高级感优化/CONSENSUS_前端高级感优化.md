# 前端高级感优化 · Consensus

## 需求描述

在不改变业务功能的前提下，提高智能代码审查平台前端的审美一致性、精致度和产品高级感。

## 技术方案

- 扩展全局 Prism 令牌：增加 surface、backdrop、focus 等高频视觉变量。
- 全局样式新增 `prism-page-shell`、`prism-surface`、`prism-page-title` 等轻量复用类。
- 主布局背景改为克制的冷灰层次，顶部栏改为半透明粘性工具栏。
- 登录页移动端增加 compact brand，并优化表单标签和首屏密度。
- Dashboard 统计卡改为 Element Plus icon 组件，弱化装饰光效。
- Security 与 Agent 态势组件去除 emoji/过强光晕，统一成 Prism mark 与 token 色。

## 技术约束

- 不新增依赖。
- 保持 Vue3 + TypeScript + Sass 写法。
- 新增或修改函数需保留函数级注释；本次尽量不新增业务函数。
- 样式以现有 CSS 变量为主，减少硬编码颜色。

## 验收标准

- 前端构建通过。
- 浏览器检查登录页桌面/移动端。
- 关键页面在无后端或接口失败时仍能正常显示加载/错误/空态。
