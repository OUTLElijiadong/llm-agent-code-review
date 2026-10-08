# 沙箱测试阶段标识与提交竞态验收

## 已确认原因

- 历史黑盒任务 `sbx_3b317e05c0fa45128c20e31d` 的样本没有可用 Web 路由；八个探测地址返回 404，因此该黑盒业务断言失败。这个证据不能证明 Prism 公网服务故障。
- v4.0.83 将黑盒与组合启动事件统一记录为 `running_whitebox`，造成 UI 误标。v4.0.84 已修复事件阶段，并于 2026-10-07 23:42:46 UTC 部署主站；独立 Worker 也已切换到同一 release。
- v4.0.84 发布后第一轮无路由负例在 `2026-10-07T23:45:27Z` 被误判失败。追加审计事件顺序为 `validating → preparing → stopping → cleanup_retry → failed`，错误为“执行器重启时任务停留在不可恢复阶段 preparing”。systemd `NRestarts=0`，说明不是服务重启。
- 根因是 30 秒周期 janitor 与同进程 `submit_job()` 竞态：janitor 看到正在提交的 `preparing` 状态，将其当成上次进程遗留并清理。其余两轮无路由负例均按预期快速拒绝（exit 1、无超时）；此处不将扫描前拒绝表述为 HTTP 404。

## v4.0.84 生产复测（部分通过，发现新漏洞）

- 黑盒有效路由：3/3 成功，`/healthz` 返回 200。
- 白盒：3/3 成功；组合：3/3 成功。
- 黑盒无路由：2/3 按预期快速拒绝（exit 1、无 timeout）；1/3 被 janitor 竞态中止，不能计为场景通过。
- 12/12 Worker 任务均回执 `cleanup_confirmed=true`，对应容器与源码目录均不存在。
- 仅删除这 12 个 `codex_20261008_post84_*` Worker 状态 JSON；追加式审计日志保留，行数由 1136 增至 1196。未创建应用数据库任务。

## v4.0.85 候选验证

- 进行中任务保护覆盖 `validating`/`preparing` × 两种预约集合；模拟预约消失后的两种状态均失败关闭并确认清理；另用真实 `submit_job()` 线程分别阻塞在 validating/preparing 时并发调用 `_recover_jobs()`，任务继续运行且阶段序列正确。
- `backend/tests/test_prism_sandbox_executor.py`：86 passed。
- `frontend/src/utils/sandboxPresentation.test.ts`：6 passed。
- Ruff 与执行器 Python 字节码编译通过。
- GitHub Business CI 和 CodeQL 均已通过，详见下方正式生产验收记录。

## v4.0.85 正式生产发布与复测

- 2026-10-08 00:21 UTC 通过正式 `deploy.sh all` 发布；生产发布账本、Backend、Frontend 和独立 Sandbox Worker 均绑定完整 SHA `727d636c22e5ff7e8d7fe887f934867ce591e1f3`，版本 `4.0.85`。构建 bundle 为 106,617,890 字节，SHA-256 `20b1f7bf43f2cf88c2de453ab5fac3eda216a8c491911f3ad6a3d5a524129b9a`。
- [业务 CI](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/37704986523) 的后端全量业务回归、前端测试/静态检查/构建、部署与故障注入均成功；[CodeQL](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/37704986557) 成功。
- 发布备份 `code_review_20261008T001456Z_727d636c22e5.sql.gz`，460,540,511 字节，SHA-256 `6d0557bbeaa8dd1ca9a947eb7b7132145df14aeafba04ac41a48dae9f9d5ca14`；独立容器恢复验证通过，104 张表，Alembic 当前与 head 均为 `062_audit_log_action_length`，本次无数据库迁移。
- Backend、Frontend、MySQL、Redis、ClamAV 与 decoy 容器均 healthy；公网 HTTPS `/healthz` 和 `/readyz` 均返回 `4.0.85` / 目标 SHA。Worker 使用 `runsc`，`/health` 与 browser blackbox readiness 均 ready，systemd `NRestarts=0`。
- Worker 生产验收通过 UDS 直接调用 `/execute`，未调用 Prism 业务沙箱提交 API；因此不覆盖真实账号从 UI 创建任务的完整业务闭环。15 个有效样本的预期结果如下；另有 3 个构造阶段的路由样例未满足扫描器语法，明确排除在 15 个有效样本之外。

| 场景 | 次数 | 结果与判定 |
|---|---:|---|
| 黑盒有效 `/healthz` 路由 | 3 | 3/3 `succeeded`，HTTP 200，阶段 `running_blackbox` |
| 黑盒无可识别路由 | 3 | 3/3 按预期 `failed`，扫描阶段拒绝、未发起 HTTP 请求（`status_code=0`），无超时/OOM |
| 黑盒已识别 `/api/v1/health` 路由返回 404 | 3 | 3/3 按预期 `failed`，回执准确记录该路由和 HTTP 404，无超时/OOM |
| 白盒单元测试 | 3 | 3/3 `succeeded`，阶段 `running_whitebox` |
| 黑白盒组合 | 3 | 3/3 `succeeded`，同时有白盒和黑盒通过回执，阶段 `running_combined` |

- 18 个 Worker 请求（含 3 个排除的样例探针）均为终态；每个结果都报告 `cleanup_confirmed=true`，复核确认对应 Docker 容器和源码目录均不存在。18 个状态 JSON 已删除；Worker 追加式审计日志保留，行数前后均为 1,286。审计日志中这 18 个请求共 90 条事件、每个请求 5 条，时间范围 `00:23:39–00:27:00 UTC`；期间 `NRestarts=0`，无 janitor/recovery failure 记录。
- 使用 Safari 普通窗口真实刷新 `/sandboxes` 后，页面显示 `v4.0.85`，历史任务列表仍可载入；未使用无痕窗口，未从页面创建额外任务。
- `ops-check.sh` 返回 `status=degraded`、`can_continue=true`、无阻断项；唯一降级为磁盘使用率 88%，告警阈值 85%、临界阈值 95%。没有执行需要人工审阅的清理操作；磁盘治理仍列为待办。

## 发布前运行态核对

2026-10-07 23:56 UTC 对 `/opt/code-review/deploy/ops-check.sh` 的检查报 release mismatch，但该目录的 `.env` 和 `current.env` 都停在 4.0.72/`0c5a...`，而当前服务由 `/opt/prism-releases/3d55.../deploy` 管理。对正确的活动 release 目录复查后，发布账本、Compose 环境、运行镜像、`APP_RELEASE` 与 systemd Worker 路径均为 4.0.84/`3d55...` 一致；活动 release 的 `ops-check.sh` 在 23:57 UTC 返回 `status=degraded`、`can_continue=true`、无阻断，唯一告警为磁盘使用率 87%（85% 告警阈值、95% 临界阈值）。`cleanup.sh` dry-run 仅列出拟移除的旧镜像/缓存；未执行删除。发布应继续使用活动 release 路径，不把旧 `/opt/code-review` checkout 的误报当成线上应用镜像漂移。

## 历史证据限制

历史 4.0.78 任务退出前没有进程快照。单 PID 清理缺口与约 120 秒后 exit 124 的现象一致；不能断言当时具体哪个子进程存活。4.0.83 前的原样本失败是路由断言不匹配，不能拿本轮临时样本冒充真实业务路由验收。
