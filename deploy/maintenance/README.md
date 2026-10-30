# 生产定时清理

`cleanup.sh` 默认只盘点，只有显式传入 `--apply` 才清理。允许的范围：

- `/tmp`、`/var/tmp` 顶层超过 14 天的 `prism-v数字*.bundle` / `prism-数字*.bundle` 源码传输包：仅当 fuser 确认未占用、包可读取、生产仓库并非浅克隆且所有引用提交都可从当前 HEAD 历史到达时清理；删除前再次核对文件身份、大小、修改时间和占用状态；任何检查不确定就保留。
- 超过 168 小时、Docker 判定可回收的 builder 缓存。
- 超过 7 天的归档 systemd journal 和本脚本维护日志；当前运行日志始终排除。

脚本不清理镜像、容器、卷、数据库、备份、发布状态或环境文件。它使用独占 flock 和发布/备份/回滚共用的维护锁，并在发现活跃部署、构建或备份进程时退出。清理不会中止服务，也不执行 swapoff 或 drop_caches。

服务器独立定时器为每天北京时间 04:30，允许最多 5 分钟随机延迟；`Persistent=true` 会补偿停机错过的执行。模板时区表达式在生产安装前使用 systemd-analyze 核验。

正式发布后安装 `prism-maintenance.service` 和 `prism-maintenance.timer` 到 `/etc/systemd/system/`，执行 `systemctl daemon-reload` 和 `systemctl enable --now prism-maintenance.timer`。日志位于 `/var/log/prism-maintenance/`，保留 7 天。过期报告与应用内存缓存由后端内部定时任务独立清理，无需 Codex 或浏览器保持在线。


## 执行语义与故障排查

- **阶段互相独立**：过期传输包、Docker 构建缓存、journal、维护日志四个阶段各自运行。某一阶段失败只记录“阶段名、退出码、错误摘要”，继续执行后面的阶段，最后写 `SUMMARY ok/failed/skipped`；只要有失败阶段，脚本以非 0 退出，`systemctl is-failed prism-maintenance.service` 与巡检都能看到。错误原文只在 `/var/log/prism-maintenance/cleanup-*.log` 里，journal 只有 `status=1/FAILURE`，排查时看这个目录最新的日志。
- **Docker 配置目录**：unit 带 `ProtectHome=true`，`/root` 对进程只读，Docker CLI 在 `$HOME/.docker` 建状态目录会报 `mkdir /root/.docker: read-only file system`（2026-10-24 至 10-30 连续 7 次因此失败，已在生产用 `systemd-run` 复现；去掉 `ProtectHome` 或指定可写 `DOCKER_CONFIG` 都能成功）。unit 现在用 `RuntimeDirectory=prism-maintenance` 加 `Environment=DOCKER_CONFIG=%t/prism-maintenance/docker`，不放宽 `ProtectHome`。手工运行未设置 `DOCKER_CONFIG` 时，脚本用私有临时目录兜底并在退出时清理。根因结论来自复现，不是对所有环境的保证；日志的 `diagnostics:` 行会记录 HOME、DOCKER_CONFIG 和 Docker 客户端版本。
- **git 属主检查**：生产 `/opt/code-review` 属主是 uid 501，而 `ProtectHome=true` 使 root 的 `~/.gitconfig`（里面的 `safe.directory`）不可见，git 会报 `detected dubious ownership`。旧脚本把这个报错当成“仓库历史不完整”，传输包清理因此一直被跳过。现在所有读取仓库的 git 调用统一走 `repo_git`（带 `-c safe.directory=<仓库>`），读取失败时日志写 `cannot read repository state` 和原因，与真正的浅克隆（`shallow clone`）区分。清理传输包的其余保守条件不变：`fuser` 确认未占用、包可读取、所有引用提交都可从当前 HEAD 到达。
- **共享维护锁**：与发布、备份、恢复、回滚共用 `deploy/.releases/.maintenance.lock`，持有者的 pid、启动时间、命令名记录在旁边的 `.maintenance.lock.owner`。持有者仍在运行：正常跳过。持有者已不存在，或无法确认持有者且锁已存在超过 6 小时（`PRISM_LOCK_STALE_SECONDS`）：任务失败并在日志里给出 `rmdir` 命令，**不自动删除**，请先确认没有发布、备份、恢复或回滚在运行。
- **备份恢复验证拿锁**：`verify-backup.sh` 等脚本默认拿不到锁就立即失败；设置 `PRISM_LOCK_WAIT_SECONDS`（秒）可在持有者仍存活时有限等待。持有者已不存在时不会等待，也不会抢锁。

升级后需要在服务器重新安装 unit 并重载：

```bash
sudo install -m 0644 deploy/maintenance/prism-maintenance.service /etc/systemd/system/prism-maintenance.service
sudo systemctl daemon-reload
sudo systemctl reset-failed prism-maintenance.service
sudo systemctl start prism-maintenance.service   # 手动跑一次，随后查看最新的 /var/log/prism-maintenance/cleanup-*.log
```

## 应用内部维护

后端生命周期自动启动两个维护任务，不依赖桌面客户端、浏览器或外部调度：

- 每300秒回收过期仪表盘统计、沙箱测试文件缓存、结束超过1小时且无订阅/运行任务的圆桌内存副本。测试文件缓存同时限制为128项；统计计算锁使用弱引用，最后一个使用者退出后释放。
- 启动60秒后首次检查普通审查/圆桌报告，之后每小时最多处理25项。成功报告默认30天，失败/取消/过期/部分完成/阻塞默认7天；以结束时间且最近更新时间均已到期为准。活动或最近更新的圆桌、源码和审计记录保留。
- 到期报告清除正文、问题和文件快照，并把任务标记为已删除，保存原终态、清理时间和任务元数据；清理前逐条锁定并复核，迟到的执行回写会被新租约挡住。
- `REPORT_SUCCESS_RETENTION_DAYS` / `REPORT_FAILED_RETENTION_DAYS` 为对应天数，`0`关闭；`REPORT_RETENTION_BATCH_SIZE`范围1至100，默认25。
- 仅覆盖 `quick/standard/full/security/discuss/repo_discuss`。团队关联任务、可恢复的中断报告和与渗透委托有任何关联的任务跳过；渗透委托继续使用既有90天策略。沙箱、远程评估、未知类型不套用本轮普通报告策略，避免报告被父业务重建。
- 日志前缀分别为 `[runtime-cache]` 与 `[report-retention]`，记录实际回收条数。维护失败记录后下轮继续；不会重启数据库或强制清系统缓存。
