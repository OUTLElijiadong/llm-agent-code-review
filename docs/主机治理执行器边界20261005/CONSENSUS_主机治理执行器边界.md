# 主机治理与执行器边界共识

## 需求

消除后端容器通过 Prism root 运维执行器写任意文件并触发任意 systemd 命令的通用 root 能力，同时收紧同一执行器中可产生软件包脚本执行、SSH 持久化、任意端口开放和恢复其他业务容器的动作。保留 Prism 自动封禁、读取主机安全证据、固定维护动作与生产业务服务。

## 实现方案

- 执行器只接受 Unix socket peer credentials：生产后端进程 UID 10001 / GID 991；移除 Bearer 静态令牌校验和后端令牌注入。
- root 执行器 unit 不读取应用 `.env`；安全封禁 oneshot 也不加载整份 `.env`，脚本仅解析 3 个明确允许的安全配置键。socket 目录采用 `0710 root:prism-ops` 且启动时验证属主/组，socket 采用 `0660 root:prism-ops`；后端组仅有目录遍历权。
- 执行器动作白名单移除任意路径写入、任意 systemd unit、软件包、任意 Docker 容器、通用 firewall、系统账号和 SSH 公钥管理。
- 后端容器以固定 UID/GID 运行并移除 Linux capabilities，禁止通过提权程序获得新权限。
- 目录读取、文本读取、journal 查询均限定到 Prism 运行所需的日志/备份范围与固定 unit；保留固定巡检和安全封禁接口。
- root 执行器继续信任 UID/GID 正确的后端调用全部固定动作；该边界不二次验证应用审批。后端进程失陷时仍可能越过应用层审批调用固定恢复、清理或服务变更动作，见残余风险记录。
- 生产主机备份树的目录权限为 `0700`、普通文件为 `0600`；不删除、不覆盖任何备份内容。
- 不变更腾讯云安全组，不停 Prism 容器，不启用未经验证的 SELinux/firewalld 策略。

## 验收标准

1. 单元测试证明非白名单动作在任何系统命令、文件替换或网络规则变更前失败。
2. Peer credentials 测试覆盖允许 UID/GID、错误 UID/GID、缺少 Linux peer credential 能力时 fail closed。
3. 生产部署后从后端容器以无 Bearer token 请求固定只读 `status` 成功；非允许 peer 被拒绝。
4. 安全封禁固定动作的既有测试继续通过；生产自动封禁 timer 与执行器健康。
5. 发布后生产 `/healthz`、`/readyz`、版本 SHA、前后端及关键依赖健康，Prism 核心页面可访问。
6. 停用的非 Prism 服务仍 inactive；未改安全组；数据和备份内容保留。
7. 明确记录仍未验的攻击实弹、外部告警送达和第三方密钥撤销，不以局部通过代替全机安全结论。
