# 主机治理与执行器边界对齐

## 原始需求

继续完成 Prism 生产主机治理；保留本项目完整运行，确认其他服务停用，并处理阶段二安全评估暴露的主机运维执行器风险。

## 已核实上下文

- 生产当前运行 `4.0.45`，release SHA 为 `70dfb9b08f8a950a40d8682a791ca882037b80b4`。
- Prism 前后端、MySQL、Redis、ClamAV、Embedding 与测试数据库容器仍运行；本机公网入口由 Prism 前端提供。
- `auto-surface-mm-local`、`lijiadong-portfolio`、`momentum-radar`、`site_total`、`postfix`、宝塔服务仍为 inactive；本次不改云安全组。
- `prism-ops-executor` 以 root 运行。后端容器身份为 UID 10001、主 GID 991，并可读写挂载整个 `/run/prism-ops`；GID 991 是 `prism-ops`。
- 执行器共享令牌通过 Compose 的 `.env` 进入后端容器；执行器 systemd unit 又把整份 `.env` 注入 root 进程。
- 生产源码允许写任意绝对路径、对任意 systemd unit 执行 `daemon_reload/start`，并提供任意软件包、SSH 公钥、账号、防火墙和 Docker 容器管理动作。主机审计尚未验证这些动作曾被实弹调用；本轮不触发它们。
- 生产 `/healthz` 连续三次返回 `4.0.45 / 70df…`，`/readyz` 连续三次为 `ready`。备份树内目录和普通文件权限已收紧到 `700/600`；没有改文件内容或删除数据。

## 范围与边界

1. 在宿主机执行器边界阻止后端进程借通用文件写入、动态 systemd、软件包、账号/SSH 密钥、防火墙或任意 Docker 动作获得 root 执行和持久化能力。
2. 保留 Prism 的只读巡检、证据采集与固定安全封禁动作；对其他保留的生产运维动作继续使用现有审批与幂等审计。
3. 移除后端可读的执行器静态令牌，改由本机 Unix socket 的 peer credentials 校验后端固定 UID/GID；禁止把应用 `.env` 整份注入 root 执行器进程。
4. 将 socket 父目录设为 root 可写、`prism-ops` 组仅可遍历的 `0710`，后端可连接 socket 但不能列举、创建或删除目录项。
5. 限制宿主机文件、目录和 journal 只读动作的可查询范围；保留 SSH、Docker、Prism 定时器及公网业务入口。
6. 保持数据、备份和停用应用文件，不改云安全组；本次不启用 SELinux/firewalld/auditd/fail2ban，也不轮换未核实的第三方供应商密钥。

## 未做的推断

- 只读源码审计证明存在可利用的权限链；本轮没有在生产执行攻击，也不能据此认定曾有攻击者实际取得 root。
- 磁盘目前为 78% 使用率、约 41 GiB 可用；没有复现此前 86% 的容量告警。
- 定时器能运行不等于外部告警已送达；送达链另列为待验范围。

## 需求澄清

用户已经授权继续完成生产治理，并明确不改安全组。剩余决策均可按最小权限、保持 Prism 正常和不删除数据的原则处理，无须扩大到全机策略替换。
