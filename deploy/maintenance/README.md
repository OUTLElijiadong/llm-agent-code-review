# 生产定时清理

`cleanup.sh` 默认只盘点，只有显式传入 `--apply` 才清理。允许的范围：

- `/tmp`、`/var/tmp` 顶层超过 14 天的 `prism-v数字*.bundle` / `prism-数字*.bundle` 源码传输包：仅当 fuser 确认未占用、包可读取、生产仓库并非浅克隆且所有引用提交都可从当前 HEAD 历史到达时清理；删除前再次核对文件身份、大小、修改时间和占用状态；任何检查不确定就保留。
- 超过 168 小时、Docker 判定可回收的 builder 缓存。
- 超过 7 天的归档 systemd journal 和本脚本维护日志；当前运行日志始终排除。

脚本不清理镜像、容器、卷、数据库、备份、发布状态或环境文件。它使用独占 flock 和发布/备份/回滚共用的维护锁，并在发现活跃部署、构建或备份进程时退出。清理不会中止服务，也不执行 swapoff 或 drop_caches。

服务器独立定时器为每天北京时间 04:30，允许最多 5 分钟随机延迟；`Persistent=true` 会补偿停机错过的执行。模板时区表达式在生产安装前使用 systemd-analyze 核验。

正式发布后安装 `prism-maintenance.service` 和 `prism-maintenance.timer` 到 `/etc/systemd/system/`，执行 `systemctl daemon-reload` 和 `systemctl enable --now prism-maintenance.timer`。日志位于 `/var/log/prism-maintenance/`，保留 7 天。过期报告与应用内存缓存由后端内部定时任务独立清理，无需 Codex 或浏览器保持在线。


## 应用内部维护

后端生命周期自动启动两个维护任务，不依赖桌面客户端、浏览器或外部调度：

- 每300秒回收过期仪表盘统计、沙箱测试文件缓存、结束超过1小时且无订阅/运行任务的圆桌内存副本。测试文件缓存同时限制为128项；统计计算锁使用弱引用，最后一个使用者退出后释放。
- 启动60秒后首次检查普通审查/圆桌报告，之后每小时最多处理25项。成功报告默认30天，失败/取消/过期/部分完成/阻塞默认7天；以结束时间且最近更新时间均已到期为准。活动或最近更新的圆桌、源码和审计记录保留。
- 到期报告清除正文、问题和文件快照，并把任务标记为已删除，保存原终态、清理时间和任务元数据；清理前逐条锁定并复核，迟到的执行回写会被新租约挡住。
- `REPORT_SUCCESS_RETENTION_DAYS` / `REPORT_FAILED_RETENTION_DAYS` 为对应天数，`0`关闭；`REPORT_RETENTION_BATCH_SIZE`范围1至100，默认25。
- 仅覆盖 `quick/standard/full/security/discuss/repo_discuss`。团队关联任务、可恢复的中断报告和与渗透委托有任何关联的任务跳过；渗透委托继续使用既有90天策略。沙箱、远程评估、未知类型不套用本轮普通报告策略，避免报告被父业务重建。
- 日志前缀分别为 `[runtime-cache]` 与 `[report-retention]`，记录实际回收条数。维护失败记录后下轮继续；不会重启数据库或强制清系统缓存。
