# Prism 部署与运维指南

> 基线日期：2026-07-10。生产拓扑为 Docker Compose 管理的 **MySQL + FastAPI Backend + Nginx Frontend + ClamAV** 四个容器。完整发布步骤与人工确认项见 [`RELEASE_CHECKLIST.md`](./RELEASE_CHECKLIST.md)。

> **当前发布状态**：本地生产级全量门禁已通过，包括 Backend 双阶段镜像、Alembic `001 → 009`、真实 MySQL 备份恢复、ClamAV/YARA/fail-closed、Nginx HTTPS 与暴露面检查；但当前工作区不干净，尚无承载全部改动的精确审查 SHA，生产发布未获授权。

## 1. 安全边界与发布原则

- 只发布**已通过 CI 的精确完整 Git SHA**，不从脏工作区构建生产镜像。
- `deploy.sh` 不执行 `git pull`、`reset` 或 checkout；版本同步必须在脚本外完成并人工核对。
- Backend 或全量发布会先创建 MySQL 一致性备份，再构建、迁移、切换和健康验证。
- 自动回滚只切换应用镜像，**不会**执行 Alembic downgrade，也不会自动恢复数据库。
- 生产秘密只保存在权限为 `600` 的 `deploy/.env`，不得写入 Git、命令参数、工单或日志。
- 数据库恢复、证书私钥、Docker volume 和系统级 prune 均属于高风险操作，必须有维护窗口和明确确认。

### 1.1 本地生产级门禁基线

- Backend 生产镜像采用 builder/runtime 双阶段构建，Docker CLI 体积由 `964 MB` 降至 `494 MB`，最终镜像不含编译器和开发头文件。
- 全新隔离 MySQL 已完成 Alembic `001 → 009`，最终为 52 张表（51 ORM + `alembic_version`）。
- Backend 容器 `/healthz`、`/readyz`、`/metrics` 返回 200，生产 `/docs`、`/redoc`、`/openapi.json` 返回 404。
- ClamAV/YARA 已覆盖干净、EICAR、YARA WebShell、引擎不可达降级和生产 fail-closed；ClamAV 未发布宿主端口。
- Nginx 已通过 `nginx -t`、HTTP 308、HTTPS 首页和公网敏感运维路径拒绝验证。
- 真实隔离 MySQL 备份已通过 gzip、SHA-256、元数据、恢复和临时库清理验证。

以上证据用于证明代码和制品具备发布前条件，**不替代干净 SHA、远端 CI、生产备份、维护窗口和发布后观察**。

## 2. 服务拓扑

| 服务 | 容器 | 宿主机暴露 | 说明 |
| --- | --- | --- | --- |
| MySQL 8 | `cr_mysql` | `127.0.0.1:3307`（默认） | 数据保存在 `mysql_data` 命名卷 |
| Backend | `cr_backend` | `127.0.0.1:8000`（默认） | 公网仅通过 Nginx 同源反向代理 |
| Frontend/Nginx | `cr_frontend` | `0.0.0.0:80/443` | HTTP 除 ACME 外 308 跳转 HTTPS |
| ClamAV | `cr_clamav` | 不发布宿主端口 | 仅 Compose 私网 `3310`，病毒库保存在 `clamav_data` |

四个服务均配置健康检查、资源上限和日志轮转。生产 `OPENAPI_ENABLED=false` 时，FastAPI 不生成文档路由，Nginx 也拒绝 `/docs`、`/redoc`、`/openapi.json` 和公网 `/metrics`。

## 3. 环境准备

要求：

- Linux、Docker Engine 24+、Docker Compose v2、Git、curl；
- 80/443 对公网开放，3307/8000/3310 不对公网开放；
- 域名 DNS 已指向服务器，证书目录可用；
- 建议至少 4 GiB 内存；低内存服务器必须持续观察 ClamAV、Swap 和 OOM。
- Apple Silicon 本地联调可临时让 ClamAV 使用 `linux/amd64` 仿真；生产应按目标 Linux 主机架构选择并验证原生镜像，不把仿真资源数据当作生产容量基线。

创建配置：

```bash
cd /opt/code-review/deploy
cp .env.example .env
chmod 600 .env
vim .env

docker compose --env-file .env config --quiet
```

至少替换 MySQL 密码、`JWT_SECRET`、独立的 `API_KEY_ENCRYPTION_KEYS`、模型 API 配置、`APP_DOMAIN`、CORS 和发布资源上限。不要直接沿用 `.env.example` 中的占位值。

## 4. 首次部署

首次部署需要先准备代码、环境和 TLS 证书。证书脚本会短暂停止 Frontend 以占用 80 端口，必须显式传入域名和邮箱：

```bash
cd /opt/code-review
release_sha="$(git rev-parse HEAD)"
test -z "$(git status --porcelain -- backend frontend deploy/docker-compose.yml deploy/lib deploy/*.sh)"

cd deploy
./issue-cert.sh example.com ops@example.com
./deploy.sh all --revision "$release_sha"
```

首次部署没有可验证的上一应用镜像时，自动回滚能力有限；应预留维护窗口并在完成后逐项确认：

```bash
docker compose ps
docker compose run --rm --no-deps backend alembic current
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
./ops-check.sh
```

Alembic 必须只有一个 head，且 `current=head`。不要把 `deploy/mysql/init.sql` 当作完整迁移替代品。

## 5. 日常精确 SHA 发布

先在仓库层完成安全的 fetch/checkout，并确认当前 HEAD 就是计划版本；`deploy.sh` 本身不会修改 Git：

```bash
cd /opt/code-review
git fetch origin --tags
# 按组织流程 checkout 已审查的 release tag 或完整 commit SHA
release_sha="$(git rev-parse HEAD)"
git status --short

cd deploy
./backup.sh --reason manual_preflight
./verify-backup.sh

# 优先小步发布；只有确有需要时才使用 all
./deploy.sh backend --revision "$release_sha"
./deploy.sh frontend --revision "$release_sha"
```

可用目标为 `all`、`backend`、`frontend`。Backend/all 流程会再次执行强制发布前备份，并由目标 Backend 镜像执行 `alembic upgrade head`。构建、迁移、容器健康、`/healthz`、`/readyz` 或 HTTPS 冒烟失败时，发布立即停止并在条件允许时切回上一应用镜像。

发布状态保存在 `deploy/.releases/`：

- `current.env`：当前发布；
- `previous.env`：上一发布；
- `pending.env`：尚未完成的发布；
- `rollback-from-*.env`：历史回滚状态。

这些状态文件只保存 release/SHA/备份路径等非秘密信息，但仍不应提交 Git。

## 6. 备份、验证与恢复

### 6.1 创建备份

```bash
cd /opt/code-review/deploy
./backup.sh --reason scheduled
```

默认输出到仓库上级的 `backups/`，生成：

- `code_review_*.sql.gz`；
- `.sha256` 校验和；
- `.meta`（创建时间、Git SHA、Alembic revision、表数等）。

默认保留 14 天，可通过 `BACKUP_DIR`、`BACKUP_RETENTION_DAYS` 或命令参数调整。备份目录必须限制权限，并复制到服务器之外的加密存储；本机备份不等于灾难恢复。

### 6.2 隔离恢复验证

```bash
./verify-backup.sh
# 或指定文件
./verify-backup.sh /secure/backups/code_review_YYYYMMDD_HHMMSS.sql.gz
```

脚本会创建随机临时数据库、导入备份、验证表数和 Alembic revision，随后自动删除临时库，不覆盖生产数据库。

### 6.3 生产数据库恢复

```bash
./restore.sh /secure/backups/code_review_YYYYMMDD_HHMMSS.sql.gz \
  --confirm RESTORE_PRODUCTION
```

该操作会停止 Backend、重建应用数据库、导入备份、执行当前代码的 Alembic 并重新启动 Backend。默认还会先做一次安全备份；只有经负责人确认才可使用 `--skip-safety-backup`。

## 7. 应用层回滚

```bash
cd /opt/code-review/deploy
./rollback.sh all --confirm ROLLBACK_APPLICATION
./ops-check.sh
```

也可只回滚 `backend` 或 `frontend`。回滚前必须确认上一镜像仍存在，并评估当前数据库 schema 是否与上一应用兼容。数据库不兼容时，应保持停写并按独立恢复方案处理，禁止机械执行 Alembic downgrade。

## 8. 运维巡检与定时任务

`ops-check.sh` 是只读巡检入口，stdout 只输出 JSON：

```bash
./ops-check.sh > /tmp/prism-ops.json
status=$?
cat /tmp/prism-ops.json
```

它检查 Compose、四个容器、磁盘、内存、最近备份的年龄/gzip/SHA、Alembic `current=head`、HTTP 308 和 HTTPS 健康。任一必需项失败时退出码为 `1`，参数/调用错误为 `2`。

systemd 模板提供：

- 每日 MySQL 备份；
- 每周隔离恢复验证；
- 每 5 分钟运行状态巡检。

安装脚本默认 dry-run：

```bash
cd /opt/code-review/deploy/systemd
./install.sh --deploy-dir /opt/code-review/deploy
sudo ./install.sh --apply --deploy-dir /opt/code-review/deploy
systemctl list-timers --all 'prism-*'
journalctl -u prism-ops-check.service -n 100 --no-pager
```

systemd timer 失败必须接入云监控或日志告警；仅写入 journal 不等于已经有人收到告警。

## 9. 受控清理

`cleanup.sh` 默认只预览，不删除数据库卷、备份、证书或发布核心状态：

```bash
./cleanup.sh
./cleanup.sh --apply --keep-release-images 2 --keep-release-states 10 --cache-until 168h
```

脚本保护 `current.env`、`previous.env` 和运行容器引用的镜像，只删除超出保留数的旧 release tag、旧回滚历史、指定年龄前的 dangling image 与 builder cache。禁止用 `docker system prune`、`docker compose down -v` 或手工删除 `mysql_data` 代替该流程。

## 10. HTTPS 与证书续期

- 首次签发：`./issue-cert.sh <domain> <email>`；
- 续期检查：`./renew-cert.sh`；
- 证书：`deploy/certbot/conf`；
- ACME webroot：`deploy/certbot/www`；
- Nginx 模板：`frontend/nginx.conf.template`。

续期后应核对证书有效期、Nginx reload、HTTP 308、HTTPS 首页与同源 `/healthz`。证书私钥不得复制到 Git、备份日志或工单。

## 11. 常用只读检查

```bash
docker compose --env-file .env config --quiet
docker compose ps
docker compose logs --tail=200 backend
docker stats --no-stream
docker system df
git -C .. rev-parse HEAD
./ops-check.sh
```

生产发布与回滚应始终按 [`RELEASE_CHECKLIST.md`](./RELEASE_CHECKLIST.md) 记录精确 SHA、备份、恢复验证、迁移 revision、业务冒烟、观察窗口和最终结论。
