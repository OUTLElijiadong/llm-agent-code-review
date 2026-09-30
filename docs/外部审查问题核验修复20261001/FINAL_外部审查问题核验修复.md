# 最终报告：外部审查问题核验与修复

## 结果

Prism 已于 2026-10-01 03:59:43 CST 正式发布 `v4.0.35`，源码 SHA 为 `310ea1a8095428d670c0404a05b4dc643ad0027e`。本次发布将 systemd 运维执行器、服务单元和巡检任务统一绑定到活动 release 目录，并在发布流程中重启执行器、验证进程工作目录与发布账本，防止周期巡检继续读取旧 checkout。

AutoSurface 的公网无认证暴露已通过主机 systemd drop-in 修复为 loopback 监听。它没有可用 Git 仓库，因此该改动没有伪装成源码提交。外网连接复测被拒绝，本机健康检查仍正常；本机响应仍带 `Access-Control-Allow-Origin: *`，应用级 CORS 没有修改。

## 验证

- 发布前备份生成并在隔离库恢复成功：103 张表，Alembic `058_roundtable_sessions`。
- 发布后 Backend、Frontend、MySQL、Redis、ClamAV 均 healthy；公网 `/healthz`、`/readyz`、首页各重复 3 次均为 HTTP 200，健康接口版本和 SHA 与目标一致。
- systemd 运维执行器的 `WorkingDirectory`、`ExecStart`、`EnvironmentFile`、`/proc/<pid>/cwd` 均指向新发布目录。部署即时巡检和发布后连续两次周期巡检通过发布账本检查；两次周期服务退出码均为 0。
- 强制 CSP 已生效，Report-Only 头消失；公网 `Server` 只显示 `nginx`，未暴露版本。TLS 证书有效至 2026-12-29，Certbot timer enabled/active。
- AutoSurface `127.0.0.1:8621` 仍能本机健康访问；外网连接连续 3 次拒绝。未调用其破坏性端点。
- 前端 Vitest 129 文件/1483 项、ESLint、Vue 类型检查、Vite 构建、部署 Shell/运维测试均通过；桌面与移动审批表 Playwright 4/4 通过。后端全量此前复测为 5496 passed、5 skipped、5 warnings。
- 普通 Safari 管理员会话真实打开自进化中心并点击到审批中心。全局 LLM 危急审批与历史高风险审批没有被改动。

## 未完成范围

生产根盘使用率仍为 87%，ops-check 因超过 85% 告警线保持 `degraded`，但未到 95% 临界线；发布账本、服务、备份、Alembic 和 HTTPS 检查均通过。已生成 cleanup dry-run 候选清单，没有执行 `--apply` 或删除镜像/缓存。

生产真实浏览器本轮只验收管理员自进化与审批中心。R2–R10 的所有页面和审查员/普通账号逐项点击矩阵没有全部重跑；本地模拟 API 的 Playwright 结果不等于生产全角色验收。详见 [验收记录](./ACCEPTANCE_外部审查问题核验修复.md) 和 [待办](./TODO_外部审查问题核验修复.md)。
