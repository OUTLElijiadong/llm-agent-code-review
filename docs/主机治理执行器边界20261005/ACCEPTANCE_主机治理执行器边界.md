# 主机治理与执行器边界验收

## 当前状态

生产已运行 `4.0.47`，完整提交为 `efd620d3b1cd1e989996385085b197fa888a84fb`。发布、备份恢复验证、HTTPS、容器健康和主机执行器正反身份检查均完成。未运行利用脚本，未改腾讯云安全组，未删除生产数据或备份。

## 修复范围

- 从后端动作目录与 root 执行器移除任意文件写、通用 systemd、Docker 容器、软件包、防火墙、系统账号和 SSH 公钥变更动作。
- Unix socket 仅接受 Linux `SO_PEERCRED` 的后端 UID `10001` / GID `991`；后端不再发送静态 token。启动时核对 socket 目录属主/权限 `root:prism-ops 0710` 和 socket `root:prism-ops 0660`。
- 限制文本读取到 `/var/log`，目录元数据浏览限制到 `/var/log` 与当前配置的持久备份目录，journal 查询限制到固定 systemd 单元白名单；路径穿越、路径段符号链接、异常 runtime 目录均拒绝。
- 两个 root systemd 单元均不加载整份应用 `.env`。自动封禁程序只按白名单读取 3 个配置键，并拒绝松散文件权限、重复键和错误引号。
- Backend 容器固定为 UID/GID `10001:991`，`cap_drop: ALL`，启用 `no-new-privileges`。
- root 执行器从当前发布 dotenv 读取配置时兼容单双引号；错误配对引号 fail closed。生产 `BACKUP_DIR` 实测为未加引号，且 Python 读取路径与部署 Shell 共享解析器一致。
- 权限路由快照包含 3 个既有自动封禁 API，均断言要求 `require_super_admin`；总路由 346，匿名/无权限场景分别为 332/265。

## 本机验证

| 检查 | 结果 |
| --- | --- |
| 后端完整测试 | 6,091 passed、5 skipped；另有 123 项相关权限/Agent/运维测试通过 |
| 后端 lint 与编译 | `ruff check app`、`compileall app tests` 通过 |
| 前端 | ESLint 通过；139 个测试文件、1,741 项通过；生产 `vue-tsc && vite build` 通过 |
| 部署执行器/安装回滚 | 107 passed、2 skipped；包含单双引号 dotenv、错引号拒绝、目录相邻路径拒绝 |
| 发布 Shell 门禁 | release binding 33/33；发布失败、回滚矩阵通过 |
| Docker Compose | 配置解析通过；渲染身份 `10001:991`、drop ALL、no-new-privileges、空执行器令牌 |
| 依赖审计 | npm 全依赖及生产依赖均为 0 条已知漏洞；Python 生产锁无已知漏洞 |
| Alembic | 唯一 head 为 `062_audit_log_action_length`，生产 current 与 head 相同 |
| 差异/语法 | `git diff --check`、相关 Ruff 与 compileall 通过 |

## 生产发布和数据保护

- 从原 active `4.0.45`（`70dfb9b08f8a950a40d8682a791ca882037b80b4`）发布 `4.0.46`，再从 `4.0.46` 发布最终 `4.0.47`。生产源码树均由已验证 Git bundle 精确克隆，未把旧 release 中未跟踪的 `.env` 备份并入新树。
- 最终 release 路径为 `/opt/prism-releases/efd620d3b1cd1e989996385085b197fa888a84fb`，后端与前端镜像标签均为同一完整 SHA。
- 两次发布前都新建备份，并在独立 `cr_testdb` 中通过 gzip/SHA 与完整恢复校验；每次恢复均核对 104 张表和 Alembic revision `062_audit_log_action_length`。第二次生产备份为 `/opt/prism-backups/code_review_20261005T071358Z_efd620d3b1cd.sql.gz`，保留未删除。
- `deploy.sh all` 两次均完成 backend/frontend 健康、HTTPS 同源 health/ready、静态资源同步、发布账本和运维巡检门禁；Alembic 无待迁移变更。

## 最终生产复核

- `https://lijiadong.cn/healthz` 返回 `ok`，`/readyz` 返回 `ready`，版本与 release SHA 均为 `4.0.47` / `efd620d3b1cd1e989996385085b197fa888a84fb`。
- 执行器实际部署目录指向最终 release；`prism-ops-executor.service` active，`ProtectHome=yes`、`ProtectSystem=full`，EnvironmentFiles 为空。`prism-security-block.service` 的 EnvironmentFiles 为空、Result 为 success，`prism-security-block.timer` active。
- socket 目录实测 `root:prism-ops 0710`，socket `root:prism-ops 0660`。后端身份 `10001:991`、`CapEff=0`、`OPS_EXECUTOR_TOKEN` 为空。
- 后端容器发起真实只读 `status` 请求得到 HTTP 200 且 `ok=true`；同一 socket 上使用错误 UID `10002` 得到 HTTP 403。成功探针产生的只读运维审计记录保留。
- 最终部署的 Python dotenv 解析结果与共享 Shell 解析器一致，且目标备份目录存在。与此前的合成配置测试合并，覆盖当前无引号配置及单双引号配置。
- Prism 后端、前端、MySQL、Redis、ClamAV、Embedding、隔离恢复数据库均运行；`auto-surface-mm-local`、`lijiadong-portfolio`、`momentum-radar`、`site_total`、`postfix` 和 `bt` systemd 单元均 inactive。
- TCP 监听中公网地址为 SSH `22` 与 Prism `80/443`；后端 `8000`、MySQL `3307`、containerd `35913` 均为 `127.0.0.1` 回环监听。未发现本机 `8888` 监听；腾讯云安全组保持未改。
- 根分区使用率 `80%`，可用 `39,016,404 KiB`（约 37 GiB）。两个发布备份均保留，无清理或删除动作。

## 范围边界

- 未执行攻击、利用脚本、压力测试或数据破坏；生产验证只覆盖发布门禁、健康接口、运维只读请求和 socket 身份拒绝。
- `SO_PEERCRED` 证明调用进程 UID/GID，不证明具体用户或审批。若后端自身失陷，仍可直接调用 root 执行器保留的固定高权限动作；这不是任意 shell，但仍需后续架构治理。
- `/opt/code-review` 仍是显示 `4.0.31` 的旧源码目录，不是 active release；本次未删除或改写它。
