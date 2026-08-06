# 运维教程(仅超级管理员可检索)

> 本教程只有唯一超级管理员 admin 的检索才能命中。所有服务器运维必须通过 admin_execute_operation 执行白名单动作，写/高危动作自动等待用户批准；不要编造执行结果。先查后改。

## 一、实时查看服务器信息(只读)
- 整体状态(CPU/内存/磁盘/负载/运行时长)：status 或 admin_system_status。
- 主机资产(CPU 核数/内存/磁盘/网络/系统)：host_inventory。
- 服务日志：journal_query(unit=服务名，如 backend/frontend/mysql/nginx；可选 since/lines)。
- 读文件：read_text_file(path=绝对路径)。
- 证书状态：certificate_status。
- 安全事件：ssh_login_events、flytrap_attack_events、nginx_attack_events、backup_audit、db_threat_signals、db_health。

## 二、常见运维操作
### 开放/关闭端口(用户说"开放 8080 端口")
- 开放：firewall_action { operation: "add", target_type: "port", value: "8080" }。
- 关闭：firewall_action { operation: "remove", target_type: "port", value: "8080" }。
- 开放服务：target_type: "service", value: "http"/"https" 等；可选 zone。
- 说明：这是 firewalld 白名单动作，执行后如实汇报结果。

### 重启服务
- restart_service { service: "backend"|"frontend"|"mysql"|"redis"|"clamav" }。

### 安装/升级/卸载软件包
- package_action { operation: "install"|"upgrade"|"remove", packages: ["包名"] }。

### 系统服务与容器
- systemd_unit_action { unit: "单元名", operation: "start"|"stop"|"restart"|"reload"|"enable"|"disable"|"daemon_reload" }。
- docker_container_action { container: "容器名", operation: "start"|"stop"|"restart"|"pause"|"unpause" }。

### 账号与密钥
- 创建/锁定/解锁/删除系统账号：account_action { operation, username, shell?, remove_home? }。
- 添加/移除 SSH 授权公钥：ssh_authorized_key_action { operation: "add"|"remove", username, public_key, fingerprint? }。

### 备份与恢复
- 完整备份：backup_database；校验备份：verify_backup { file }。
- 恢复数据库：restore_database { file }(高危，需批准)。
- 应用回滚：rollback_application { target: "all"|"backend"|"frontend" }。
- 清理旧产物：cleanup。

### 配置与证书
- 更新运行配置：update_config { key, value }。
- 平滑重载 Nginx：nginx_reload。
- 续签证书：renew_certificate。
- 数据库维护：database_maintenance。

## 三、平台部署与发布(给管理员参考)
- 后端/前端镜像用 overlay 方式发布：改代码 → 本地/服务器构建 overlay 镜像 → 更新 deploy/.env 的 BACKEND_RELEASE/FRONTEND_RELEASE → docker compose up -d backend/frontend → 执行 sync-frontend-assets.sh 同步前端卷 → 健康检查。
- 数据库迁移：alembic upgrade head(容器内 /app)。
- 发布前先备份：deploy/backup.sh；发布失败可回滚：deploy/rollback.sh。

## 四、故障排查
- 前端空白/登录后空白：执行 sync-frontend-assets.sh 同步 assets 卷；检查 nginx 是否拿到新 index.html。
- 沙箱「镜像 digest 校验失败/未配置不可变 digest」：执行 deploy/sandbox/pin-profiles.sh --apply 重新固化 5 种沙箱镜像 digest，重启 prism-sandbox-executor。
- 沙箱 executor 未运行：systemctl status prism-sandbox-executor；查看 journalctl -u prism-sandbox-executor。
- 后端不健康：docker ps 看容器；journalctl -u prism-backend 或 docker logs cr_backend。
- LLM 报 503/超时：检查大模型配置(/admin/llm)与网络；换模型或重试。
- 磁盘高/备份失败：backup_audit 查备份记录；清理旧备份(cleanup)。
- 审批异常：查审批中心；敏感审批仅超管可见。
