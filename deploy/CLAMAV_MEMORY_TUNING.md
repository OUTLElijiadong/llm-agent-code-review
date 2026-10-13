# ClamAV 内存与数据库重载设置

Compose 默认将 ClamAV 容器内存上限设为 `2200m`，并通过官方镜像 `/init` 支持的 `CLAMD_CONF_` 环境变量设置 `ConcurrentDatabaseReload no`。本配置保留官方镜像默认运行的 `freshclam` 数据库更新和数据库下载校验，没有禁用更新或完整性测试。

关闭并发数据库重载可避免更新时同时在内存中保留旧、新两套扫描引擎，降低 `clamd` 重载期间的峰值内存。代价是数据库重载期间新扫描会等待重载完成。部署后应确认 `clamd` ping、数据库 reload 和干净样本/EICAR 扫描均正常，并关注容器 OOM 状态与内存峰值。

默认 `2200m` 是当前目标主机资源条件下的 Compose 基线；可通过 `CLAMAV_MEM_LIMIT` 覆盖。不要把禁用 `freshclam` 数据库测试作为降内存手段，以免失去下载数据库校验。

参考：

- [ClamAV 官方 Docker 内存说明](https://docs.clamav.net/manual/Installing/Docker.html#memory-ram-requirements)：默认并发重载会短暂构建新引擎，`ConcurrentDatabaseReload no` 会减少峰值，但重载期间暂停新扫描。
- [Cisco-Talos 官方 Docker entrypoint](https://github.com/Cisco-Talos/clamav-docker/blob/main/clamav/unstable/debian/scripts/docker-entrypoint.sh)：启动时将 `CLAMD_CONF_` 前缀变量应用到 `clamd.conf`。
