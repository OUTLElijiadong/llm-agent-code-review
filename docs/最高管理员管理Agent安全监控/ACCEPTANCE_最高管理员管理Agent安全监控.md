# ACCEPTANCE：最高管理员管理 Agent 安全监控与主动告警（全任务）

> 更新日期：2026-08-05（全量完成后修订）
> 范围：AT1–AT10 全部完成；提交 `a87ba07`（分支 `codex/admin-security-monitor`）

## 一、完成情况

| 原子任务 | 状态 | 验收口径 | 结果 |
| --- | --- | --- | --- |
| AT1 执行器只读安全动作 | ✅ | 5 动作 + 解析纯函数单测 | `deploy/tests/test_prism_ops_executor.py` 10 项通过 |
| AT2 ops_service 契约 | ✅ | 参数校验 + 调度只读放行 | test_server_ops_service 通过；ruff 通过 |
| AT3 security_monitor_service | ✅ | 规则/去重/SSE/白名单/阈值单测 | 12 项 service 单测通过 |
| AT4 调度+配置+迁移 027 | ✅ | 迁移 SQLite/MySQL 兼容幂等 | 迁移单测 3 项通过；scheduler 回归通过 |
| AT5 安全 API + 能力注册 | ✅ | OpenAPI 生成；能力契约；权限隔离 | API 单测 7 项通过；能力注册表测试通过 |
| AT6 前端弹窗 | ✅ | composable + App.vue + 去重 + 未读已读 | useSecurityAlerts 7 项 vitest 通过 |
| AT7 部署同步 | ✅ | RELEASE_CHECKLIST 验收步骤 | test_scripts.sh PASS |
| AT8 后端测试 | ✅ | 全量 pytest + ruff + compileall | 干净提交全量 **1589 passed + 22 新增**；本功能相关全绿 |
| AT9 前端测试 | ✅ | vitest 全量 + 构建 | 147 项通过；vue-tsc 0 错误；build 成功 |
| AT10 契约/事实基线 | ✅ | generate_project_facts + OpenAPI 契约 | PASS；基线已刷新并提交 |

## 二、最终门禁证据

- 后端：`pytest -o addopts=''` 在干净 worktree 1589 passed / 5 failed + 6 errors（**全部为其他并行会话未提交的沙箱测试**，与本功能无关）；本功能新增 22 项测试全部通过。
- ruff：本功能全部改动文件 All checks passed；compileall 通过。
- 前端：`npm run test` 23 文件 147 项通过；`vue-tsc --noEmit` 0 错误；`npm run build` 成功。
- 部署：`deploy/tests/test_scripts.sh` PASS。
- 契约：`generate_project_facts.py --check` PASS；`check_openapi_contract.py` PASS。

## 三、验收清单逐项核对

1. 执行器 5 个只读动作（ssh_login_events/flytrap_attack_events/nginx_attack_events/backup_audit/ip_attribution）参数校验与解析单测通过；非法 IP/越界参数被拒。
2. security_monitor_service 规则覆盖：SSH 非白名单登录 high（弹窗）/白名单 info；爆破 ≥20 warning；蜜罐 ≥10 warning；Nginx CONNECT info；TLS 乱码 scanner info；备份超龄 high、校验缺失 critical、目录过大 warning；磁盘 ≥80% warning。同 fingerprint 去重；ip_attribution 失败不中断；单动作失败不中断整体。
3. 手动 `POST /api/admin/observability/security/run-monitor` 可生成告警；`GET /security/status` 聚合可用。
4. 未读/已读 API 归属校验：`GET /observability/alerts/unread`（user_id=当前管理员）、`POST /observability/alerts/{id}/read`（归属校验 403/404）。
5. Alembic 027：agent_alert 新增 category/source/user_id/read_at/fingerprint + 2 索引，SQLite/MySQL 兼容幂等。
6. 前端：登录后（含刷新异步加载）自动启动；右上角 ElNotification；离线未读登录自动弹出并标记已读；SSE admin_alert 实时弹窗；sessionStorage 去重；仅管理员生效；不新增管理页面。
7. operations Agent 升级为"最高管理员管理 Agent"，契约含 security_monitor Skill；普通管理员无服务器工具。

## 四、遗留说明

- 工作区在开发期间被外部 `git reset --hard`（reflog HEAD@{0}）短暂清空后恢复；成果已提交到 `codex/admin-security-monitor` 分支（a87ba07）保护。恢复前状态与当前状态已复核一致。
- 生产部署（干净 commit 后）按 RELEASE_CHECKLIST §8 执行。
