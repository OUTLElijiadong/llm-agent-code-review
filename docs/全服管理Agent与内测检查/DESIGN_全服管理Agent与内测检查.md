# DESIGN：全服管理 Agent 与内测前检查

## 调用链

```text
管理员 -> 管理副驾驶 -> Responses 规划器 -> admin_execute_operation
                                      -> 持久化审批/幂等执行
                                      -> ops_service 审计与脱敏
                                      -> Unix Socket + Bearer Token
                                      -> Root 执行器
                                      -> systemd / Docker / 文件系统 / 软件包 / 防火墙 / 账户
```

## 管理界面业务能力链

```text
管理员 -> 管理 Agent -> admin_execute_capability
                         -> 固定能力注册表(页面/方法/路径/风险/权限)
                         -> OpenAPI/Pydantic 参数契约
                         -> Responses 审批/幂等执行账本
                         -> 当前管理员 JWT -> 应用自身 API
                         -> 原路由权限/业务 Service/数据库审计
```

- 模型不能提供 HTTP 方法和路径，只能选择注册能力编码。
- 能力发现结果通过注册能力对应的 OpenAPI operation 生成精确 JSON Schema；执行时用同一契约再次校验路径、查询和请求体参数。
- 执行前再次校验管理身份、具体权限和能力风险；审批记录、SSE 事件和幂等执行账本只持久化脱敏参数与结果。
- 能力注册表覆盖管理总览、治理、审批、发布、内测码、策略、工具、知识、任务、告警、奖惩、回滚、用户、RBAC、AI 日志、审计、进化、Skill、Embedding、LLM 和报告模板。

## 新增动作

### 只读

- `host_inventory`：主机、资源、文件系统、systemd 运行/失败单元、Docker 容器、监听端口和定时器。
- `list_directory`：列出一个绝对目录的一层条目，不递归读取文件内容。
- `read_text_file`：读取非敏感 UTF-8 普通文件，返回大小、SHA-256、截断状态和内容。
- `journal_query`：按已校验的 systemd 单元、时间范围和条数读取脱敏日志。

### 写操作

- `systemd_unit_action`：对已存在的单元执行 `start/stop/restart/reload/enable/disable/daemon_reload`。
- `docker_container_action`：对已存在的容器执行 `start/stop/restart/pause/unpause`。
- `write_text_file`：按 `path/content/expected_sha256/mode` 原子写入文本文件，并返回回滚副本和新 SHA-256。
- `package_action`：用固定 `dnf` 命令安装、升级或移除已校验的软件包名。
- `firewall_action`：用固定 `firewall-cmd` 动作管理端口或服务，并在成功后 reload。
- `account_action`：创建系统账户、锁定、解锁或删除明确账户；不接受明文密码。
- `ssh_authorized_key_action`：为明确账户新增或移除合法 OpenSSH 公钥，不读取或处理私钥。

## 双层门禁

1. API 层只允许真实管理员使用 `surface=admin`。
2. Responses 运行时对所有非只读动作生成持久化审批状态。
3. critical 审批卡要求管理员输入完全一致的“确认执行”。
4. 批准续跑使用原 `run_id + call_id`，生成稳定请求 ID；执行记录存在时返回已有结果，不重复副作用。
5. Root 执行器再次校验动作、参数、目标存在性、路径和大小，不能信任模型或后端已校验。

## 文件安全与回滚

- 禁止读取或写入凭据文件、私钥、Shadow、SSH 密钥、项目 `.env` 和虚拟文件系统。
- 写入前若目标存在，要求普通文件且不能是符号链接；提供 `expected_sha256` 时必须匹配当前内容。
- 原内容复制到 `/var/lib/prism-ops/file-backups/<request-id>/`，权限仅 Root 可读。
- 新内容写入同目录临时文件，`fsync` 后 `os.replace`，保留或显式设置 POSIX 权限。

## 审计脱敏

- `write_text_file` 不记录 `content`，只记录路径、字节数和 SHA-256。
- 软件包、防火墙、账户和公钥动作只记录结构化目标；公钥仅记录指纹，不记录完整内容。
- 文件读取对敏感路径直接拒绝，避免内容进入模型、工具结果或聊天历史。
- 宿主机审计记录只保存请求 ID、动作、脱敏参数摘要、状态、耗时和错误摘要。

## 发布与回滚

- 发布前运行本地后端定向测试、前端组件测试、TypeScript 检查、构建和部署脚本测试。
- 生产先执行数据库备份及隔离恢复验证，再同步精确文件，安装 systemd 单元并重建 Backend/Frontend。
- 部署失败时恢复同步前文件副本和上一镜像标签；数据库无迁移时不执行数据回滚。
- 发布后验证 HTTPS、healthz/readyz、Alembic、容器、执行器、Agent 只读动作、critical 审批和跨角色隔离。
- 使用自然语言在管理 Agent 中选择多个代表性页面能力，验证真实 API 结果可由页面同源查询观察，不依赖 UI 模拟点击完成业务动作。
