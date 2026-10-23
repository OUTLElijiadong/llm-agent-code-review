# 生产定时清理

`cleanup.sh` 默认只盘点，只有显式传入 `--apply` 才清理。允许的范围：

- `/tmp`、`/var/tmp` 顶层名称为 `prism-maintenance-*.tmp`、单硬链接、超过 7 天未修改且未访问的普通文件。
- `/tmp`、`/var/tmp` 顶层超过 14 天的 `prism-v数字*.bundle` / `prism-数字*.bundle` 源码传输包：仅当 fuser 确认未占用、包可读取、生产仓库并非浅克隆且所有引用提交都可从当前 HEAD 历史到达时清理；任何检查不确定就保留。
- 超过 168 小时、Docker 判定可回收的 builder 缓存。
- 超过 7 天的归档 systemd journal 和本脚本维护日志；当前运行日志始终排除。

脚本不清理镜像、容器、卷、数据库、备份、发布状态或环境文件。它使用独占 flock 和发布/备份/回滚共用的维护锁，并在发现活跃部署、构建或备份进程时退出。清理不会中止服务，也不执行 swapoff 或 drop_caches。

服务器独立定时器为每天北京时间 04:30，允许最多 5 分钟随机延迟；`Persistent=true` 会补偿停机错过的执行。模板时区表达式在生产安装前使用 systemd-analyze 核验。

正式发布后安装 `prism-maintenance.service` 和 `prism-maintenance.timer` 到 `/etc/systemd/system/`，执行 `systemctl daemon-reload` 和 `systemctl enable --now prism-maintenance.timer`。日志位于 `/var/log/prism-maintenance/`，保留 7 天。Codex 每日 05:00 巡检定时器和清理结果，状态正常时不打扰。
