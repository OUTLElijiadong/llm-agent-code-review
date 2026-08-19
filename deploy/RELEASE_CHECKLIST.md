# Prism 生产发布与回滚清单

> 原则：精确 SHA、先备份再变更、逐项验证、失败立即停止。不得把脏工作区直接同步到生产。

## 1. 发布前（T-30 分钟）

- [ ] 已确认计划发布的完整 Git commit SHA，且构建源目录无未提交变更。
- [ ] 根目录 `VERSION` 为本次语义版本，前后端健康接口/构建均使用同一值。
- [ ] CI 的后端测试/覆盖率/Ruff/compileall、前端 lint/test/build、依赖审计、Alembic、Compose、Shell 与契约门禁全部通过。
- [ ] 已确认变更范围、维护窗口、负责人、观察人和回滚决策人。
- [ ] 当前 `current.env`、`previous.env` 与运行容器镜像可读，上一版本镜像仍存在。
- [ ] 磁盘、内存、Swap 和 Docker 空间有安全余量；不在发布过程中执行未评估的系统级 prune。
- [ ] 未在命令行、工单或日志中粘贴密码、JWT、API Key、证书私钥。

## 2. 数据保护（T-20 分钟）

```bash
cd /path/to/project/deploy
./backup.sh --reason pre_deploy
./verify-backup.sh
```

- [ ] 新备份非空，gzip 与 SHA-256 校验通过。
- [ ] 隔离恢复数据库创建、导入、表数与 Alembic revision 验证通过，并已自动清理。
- [ ] 已记录备份文件名、校验和、当前 Alembic revision 和恢复验证结果。
- [ ] 若任一项失败，停止发布；不得以旧备份冒充本次发布门禁。

## 3. 小步发布（T0）

```bash
./deploy.sh backend --revision <FULL_COMMIT_SHA>
# 验证稳定后，再按计划发布 frontend 或 all
# 注意: 发布 frontend 后必须执行 ./sync-frontend-assets.sh 同步 assets 卷,
#       否则 index.html 引用的新哈希文件 404 导致页面空白。
```

- [ ] 目标 revision 解析为计划中的完整 SHA。
- [ ] 只发布本次必要组件；数据库迁移只执行 `alembic upgrade head`。
- [ ] Backend `/healthz`、`/readyz`、version、release 标识和日志通过后，才继续前端/全量步骤。
- [ ] HTTP 仅保留 ACME challenge，其余返回 308；HTTPS 首页和同源 `/healthz` 通过。
- [ ] 生产 `/docs`、`/redoc`、`/openapi.json` 均不可公开访问。
- [ ] MySQL、ClamAV、Backend、Frontend 四个容器均 healthy，ClamAV 3310 未映射公网。

## 4. 业务冒烟（T+5 分钟）

- [ ] 登录、当前用户、项目列表、代码文件列表可用。
- [ ] 新建或选择测试项目，完成一次干净文件上传与审查。
- [ ] EICAR 样本被拒绝；ClamAV 故障时上传按生产 fail-closed 拒绝。
- [ ] Agent 工具参数缺失、extra、错类型不会进入 handler；合法参数可正常执行。
- [ ] API Key 新写入可解密，旧密钥记录可机会式重加密，坏记录失效且日志不含秘密。
- [ ] Alembic `current` 与唯一 `head` 一致。

## 5. 观察窗口

### T+15 分钟

- [ ] `./ops-check.sh` 返回 `status=ok`。
- [ ] Backend 5xx、延迟、重启次数、OOM、数据库连接与恶意扫描错误无异常上升。

### T+30 分钟

- [ ] 核心业务再次抽查；无持续错误、队列堆积或资源逼近阈值。
- [ ] 保留当前和上一 release 镜像，不执行应用回滚路径以外的破坏性操作。

### T+60 分钟

- [ ] 记录发布结果、最终 SHA、迁移 revision、观察指标与遗留问题。
- [ ] 如需清理，先运行 `./cleanup.sh` 审阅 dry-run，再由负责人显式执行 `--apply`。

## 6. 回滚触发条件

出现任一条件立即停止后续变更并评估回滚：

- 健康/就绪检查连续失败或容器反复重启；
- 登录、项目、审查或报告核心流程不可用；
- 新增 5xx、数据完整性错误、密钥解密错误或越权风险；
- 迁移后应用与数据库不兼容；
- ClamAV 故障未按 fail-closed，或 HTTPS/OpenAPI 安全策略失效；
- 内存、磁盘或负载持续超过门限并影响服务。

## 7. 应用层回滚

```bash
./rollback.sh all --confirm ROLLBACK_APPLICATION
./ops-check.sh
```

- [ ] 仅切回上一 Backend/Frontend 镜像，确认 release 标识正确。
- [ ] **不自动执行 Alembic downgrade，不自动恢复生产数据库。**
- [ ] 若数据库变更不向后兼容，保持停写并由负责人基于已验证备份制定独立恢复方案。
- [ ] 回滚后重新执行健康、HTTPS、业务、ClamAV 和 Alembic 检查，并记录事故时间线。

## 8. 安全监控（最高管理员管理 Agent）发布验收

> 适用于包含 `deploy/prism_ops_executor.py` 新只读安全动作或 `security_monitor` 调度的发布。

- [ ] 发布树中的 `deploy/prism_ops_executor.py` 已同步到生产（如 `/opt/prism-releases/<sha>/deploy/`），且 `prism-ops-executor.service` 的 `WorkingDirectory/ExecStart` 指向该发布树；发布后 `systemctl restart prism-ops-executor.service` 并确认 active。
- [ ] `deploy/.env` 已按需配置 `SECURITY_MONITOR_*`、`SECURITY_SSH_ALLOWLIST_CIDRS`（本人常用 IP 段）与 `THREAT_INTEL_BASE_URL`（默认 http://ip-api.com/json，可覆盖）。
- [ ] Alembic 迁移 027 已执行：`agent_alert` 含 category/source/user_id/read_at/fingerprint 列与索引。
- [ ] `security_monitor` 调度任务已注册（interval@5m）且仅超级管理员可改；`AGENT_GOVERNANCE_SCHEDULER_ENABLED=true`。
- [ ] 手动触发 `POST /api/admin/observability/security/run-monitor` 可生成告警；SSH 成功登录（非白名单 IP）产生 high 告警。
- [ ] 超级管理员前端右上角弹出安全告警；离线期间告警在下次登录自动弹出并标记已读；普通管理员不可调用 run-monitor/status。
- [ ] `ip_attribution` 被动溯源返回归属/ASN；失败时不中断告警流程。
- [ ] 备份审计动作返回最新备份年龄/校验/体积；超阈值告警含清理建议；清理动作仍走 critical 审批。
