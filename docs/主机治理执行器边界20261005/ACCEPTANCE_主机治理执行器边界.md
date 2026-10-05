# 主机治理与执行器边界验收

## 当前状态

候选版本 `4.0.46` 已完成本机测试，等待精确 SHA 生产发布及发布后复核。生产基线来自 2026-10-05 直接只读检查；未运行利用脚本或触发破坏性主机动作。

## 修复范围

- 移除 root 执行器和后端 Agent 工具中的任意文件写、通用 systemd、Docker 容器、软件包、防火墙、系统账号和 SSH 公钥动作。
- Unix socket 仅信任 Linux `SO_PEERCRED` 的后端 UID `10001` / GID `991`；移除后端静态令牌校验与传送。启动时要求 socket 目录属主为 `root:prism-ops`、权限为 `0710`，socket 属主/权限为 `root:prism-ops 0660`。
- 限定主机日志/目录/journal 查询范围；损坏或不匹配的 runtime 路径拒绝启动。
- 目录浏览只允许 `/var/log` 与当前部署配置的持久备份目录；不再依赖旧 `/opt/code-review/backups` 硬编码路径，并验证相邻目录仍被拒绝。
- 两个 root 单元都不加载整份应用 `.env`。安全封禁程序只解析状态目录、保护网段、SSH 端口三个允许键，拒绝松散文件权限、重复项及错误引号。
- Backend 容器设为 UID/GID `10001:991`、`cap_drop: ALL`、`no-new-privileges`。
- 更新路由快照：现有安全中心增加了 3 条自动封禁接口，均明确核对 `require_super_admin`。当前验收路由数为 346，匿名/无权限场景分别为 332/265。

## 本机验证

| 检查 | 结果 |
| --- | --- |
| 后端完整测试 | 6,091 passed、5 skipped；6 条既有依赖/收集警告 |
| 后端 lint | `ruff check app` 通过；改动后 123 项相关权限/Agent/运维测试通过 |
| 后端编译与 Alembic | `compileall app tests` 通过；唯一 head 为 `062_audit_log_action_length` |
| 前端 | ESLint 通过；139 个测试文件、1,741 项通过；Vue 类型检查及 Vite 生产构建通过 |
| 部署执行器/安装回滚 | 104 passed、2 skipped |
| 发布 Shell 门禁 | release binding 33/33；发布失败与回滚模拟通过 |
| Docker Compose | 配置解析通过；渲染结果为 `10001:991`、drop ALL、no-new-privileges、空执行器令牌 |
| 依赖审计 | npm 全依赖和生产依赖均为 0 条已知漏洞；Python 生产锁无已知漏洞 |
| 差异/语法 | `git diff --check`、改动 Python compileall、相关 Ruff 检查通过 |

## 生产基线

- 当前正式运行 release 是 `/opt/prism-releases/70dfb9b08f8a950a40d8682a791ca882037b80b4`，版本 `4.0.45`；生产健康检查三次结果为 `ok/ready`。
- `/opt/code-review` 当前源码目录显示 `4.0.31`，不是正在运行的 release；发布来源必须以运行中的 release SHA 为父提交，不从该旧目录构建。
- Prism 前端、后端、MySQL、Redis、ClamAV、Embedding、隔离测试数据库均运行；`auto-surface-mm-local`、`lijiadong-portfolio`、`momentum-radar`、`site_total`、`postfix` 和 `bt` 单元 inactive。
- 根分区使用率 `78%`，可用约 `42,019,264 KiB`。无需删除数据或清理备份。
- 发布前 `/run/prism-ops` 为 `root:991 0770`，后端容器没有 CapDrop/no-new-privileges，ops executor unit 仍加载 `.env`。这些为本次拟修复项，不是修复后状态。
- 当前 active release `.env` 为 root 所有、模式 `0600`；需要的保护网段键存在且语法有效，SSH 端口与状态目录使用默认值。令牌旧键仍保留以兼容应用回滚，但新 Compose 明确覆盖为空；不输出、移除或轮换该值。
- 当前生产 `BACKUP_DIR` 指向可用的持久备份目录，与旧代码路径硬编码值不同；新执行器从当前发布配置读取该根目录，并拒绝读取其相邻目录。
- 正式 release 树有一个未跟踪备份文件 `.env.before-70dfb9b08-20261005T044155Z`。保留它；发布通过 Git bundle 克隆精确提交，不把此文件并入新 release。

## 发布后必须补记

记录新版本完整 commit SHA、实际发布备份/隔离恢复证据、运行容器 image digest、systemd unit 属性、runtime 目录/socket owner/mode、后端 `CapEff` 与 mount 状态、后端实际 `status` 只读请求、错误 UID 的 403、Prism 健康/业务检查及非 Prism 服务状态。若任一门禁失败，停止并使用发布脚本的应用回滚流程；数据库不得自动降级。

## 范围限制

本次不修改腾讯云安全组，不运行真实攻击，不删除备份，不轮换外部 API 或供应商密钥。SO_PEERCRED 只验证后端进程身份；若后端自身失陷，它仍可直接调用保留的参数受限固定 root 动作，越过应用层 actor/审批检查。该残余边界见 DESIGN 与 TODO。
