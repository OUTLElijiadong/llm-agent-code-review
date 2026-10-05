# 自动封禁验收记录

状态：代码、自动化回归与 Linux 隔离网络验收通过；生产发布和真实浏览器配置验收待执行。

## 生产复现的审计缺陷

v4.0.44 管理员真实点击保存后，root 执行器 ledger 已记录 `security_block_configure` 成功且策略在后续宿主机轮询中核实启用；但后端 `OpsExecution` 停在 `running`，页面历史因此标记“未知”。生产 MySQL `SHOW COLUMNS audit_log LIKE 'action'` 返回 `varchar(40)`；写入 `admin_copilot.ops.security_block_configure` 时抛出 `Data too long for column 'action'`，导致运维执行、工具调用和审计日志的同一事务无法提交。

此缺陷已在生产真实路径复现；v4.0.45 将 `audit_log.action` 扩为 63 字符，覆盖当前最长的 43 字符 namespaced 运维动作，同时在 `utf8mb4` 下保持最大 252 字节，避免跨越 InnoDB `VARCHAR` 255 字节边界。待发布后使用原请求编号走执行器幂等恢复，不重复执行宿主机动作，并核对执行行、审计日志与 root ledger 三方状态。

## 已核对范围

- 旧生产 v4.0.43 的安全中心只监控，没有临时自动封禁。
- 目标宿主为 iptables v1.8.9 legacy；已只读确认 ipset、iptables 与 DOCKER-USER 存在，容器发布 TCP 80/443。
- 自动规则只读本机 SSH/Nginx 可信日志；常规阈值 SSH 20 / Web 30，窗口默认 300 秒，Web 还要求 3 个不同敏感目标。
- 小菱额外研判使用 SSH 10 / Web 10 且 3 个目标的宿主候选；模型仅见脱敏计数和候选编号。root 重读证据后最多临时封禁 120 秒。
- 管理员当前来源、最近一小时成功 SSH 来源、服务器地址、白名单与非公网来源受保护；启用前写入管理出口及服务器公网地址的 root 环境名单。
- 六项内部 root 动作无法从一般聊天 Agent 的可用工具清单或通用执行入口调用。

## 已完成的本地检查

| 范围 | 结果 | 证据 |
|---|---|---|
| 执行器规则、IP/CIDR 边界、普通 4xx、SSH/Web 阈值、IPSET TTL、候选稳定性与失败回执 | 51 通过 | `deploy/tests/test_prism_security_block.py` |
| systemd 安装成功和失败回滚（含 timer 状态） | 2 通过 | `deploy/tests/test_systemd_install.py` |
| 后端策略、API 权限、AI 输出、日预算、固定巡检、运维执行器 | 1,221 通过 | 回归含完整权限矩阵 |
| 前端完整测试 | 1,741 通过 | 139 个测试文件 |
| 前端检查 | lint、vue-tsc、生产 build 通过 | 执行器子代理独立结果 |
| 部署脚本与故障矩阵 | 通过 | `证据/部署回归.txt`；Docker CLI 仅使用假的 docker 命令，Compose 解析未在本机执行 |
| Linux 隔离网络：宿主 INPUT、DNAT Web、TTL 与手动解封 | 最新源码 2 场景通过，96.338 秒 | `证据/内核隔离回归.txt`、`证据/内核源码指纹.txt` |

## 明确未证明的事项

- 隔离网络使用合成日志证据，只验证内核规则、方向与恢复，不能证明模型能识别真实公网攻击者，也不代表进行过生产攻击重放。
- 生产发布版本/数据库备份恢复、生产内核状态、线上策略回执和浏览器真实点击尚未完成。
- IPv6 单独预检；缺少其 INPUT / DOCKER-USER 能力时不声称 IPv6 已拦截。
