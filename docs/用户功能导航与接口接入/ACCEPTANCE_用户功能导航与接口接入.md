# 用户功能导航与接口接入 - 验收记录

## 进度

- [x] T1 生产复现与后端首屏 API 基线
- [x] T2 失败测试
- [x] T3 导航与路由修复
- [x] T4 本地回归
- [x] T5 生产部署
- [x] T6 六页面真实点击

## 基线证据

- 管理员在 Agent 中心点击“查看审查规则”后最终停在 `/dashboard`，证明守卫拦截。
- 开发者论坛可进入 `/forum`，帖子列表正常显示。
- 申请维修可进入 `/support/maintenance`，工单表格正常显示。
- 生产管理员对 7 个首屏 API 请求均为 HTTP 200。

## 本地验证

- 失败测试：修复前新增用例准确出现 5 个失败。
- 定向前端测试：18 passed。
- 前端全量测试：50 passed。
- 后端全量测试：1040 passed。
- ESLint、Ruff、生产构建、`git diff --check` 全部通过。

## 生产部署

- 部署目录：`81.70.251.90:/opt/code-review`。
- 源码备份：`/opt/code-review/backups/navigation_20260715_164105/source_before.tar.gz`。
- 旧镜像：`sha256:c1bf00452819aa289023ca382ffe2578a661e5a251f058e15d4b700fedbe8540`，标签 `deploy-frontend:navigation-before-20260715-164105`。
- 新镜像：`sha256:31979a7bad723832451df41359c3a8e2adef389f3aa3cbdda1de4d0be84f9fe3`。
- 切换方式：仅执行 Frontend 的 `--no-deps --force-recreate`，Backend、MySQL、ClamAV 未重建。
- 容器状态：`running=true`、`restart=0`、`oom=false`、启动错误为空。
- HTTP 与 HTTPS 首页均返回 200，Nginx 启动日志无错误。

## 六页面点击验收

| 入口 | 最终 URL | 页面证据 | 结果 |
| --- | --- | --- | --- |
| 代码中心 | `/code` | 显示 13 个项目 | 通过 |
| 审查规则 | `/rules` | `审查规则配置`，显示 116 / 116 条 | 通过 |
| 开发者论坛 | `/forum` | `开发者论坛`，共 31 条 | 通过 |
| 个人知识库 | `/knowledge` | `个人知识库`，当前 0 条与 API 一致 | 通过 |
| 个性化画像 | `/profile/personalization` | 显式偏好与隐式画像区域正常显示 | 通过 |
| 申请维修 | `/support/maintenance` | `申请维修`，当前 0 条与 API 一致 | 通过 |

- 管理员点击上述入口后不再跳回 `/dashboard`。
- 全局搜索同时显示上述六个入口。
- 浏览器应用控制台错误和警告为 0。

## 生产 API 复测

| API | HTTP | 业务码 | 数据证据 |
| --- | ---: | ---: | --- |
| `/api/projects` | 200 | 0 | 13 / 13 |
| `/api/rules` | 200 | 0 | 116 条 |
| `/api/forum/posts` | 200 | 0 | 当前页 20 条，共 31 条 |
| `/api/knowledge/docs` | 200 | 0 | 0 / 0 |
| `/api/knowledge/stats` | 200 | 0 | 数据对象存在 |
| `/api/me/profile` | 200 | 0 | 画像对象存在 |
| `/api/maintenance` | 200 | 0 | 0 / 0 |

## 验收结论

本次范围内的导航、路由守卫、全局搜索、前端 API 调用、后端路由契约、生产容器和真实页面数据已全部通过验收，无阻塞遗留项。
