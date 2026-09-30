# 待办：外部审查问题核验与修复

1. **生产发布与复测**：候选 4.0.34 未提交、未部署。本机 Docker 保持关闭；发布须使用既有发布流程。发布后逐项复测 CSP 强制头、站点主要页面、登录与角色权限，并确认 `Server` 响应不再包含 Nginx 版本。
2. **发布后 CSP/Nginx 验收**：候选 `nginx.conf`/模板已在生产当前 Nginx 镜像环境通过 `nginx -t`，渲染策略的 WebSocket 域名已核对。发布后仍需使用正常浏览器会话确认强制 CSP 未阻断 Vue、字体、WebSocket 和主要业务流程，并核验 `Server` 头不再暴露版本。
3. **8888**：BT-Panel 绑定 `*:8888`，用户选择暂不改腾讯云安全组。8888 仍公网可达；根路径 404 不代表后台路径受限。若以后要收紧，请在控制台关闭公网入站或配置来源 IP allowlist，并从外网复测。
4. **证书自动续期**：所有现存证书的 ACME staging dry-run 已通过，但生产 systemd timer 未安装。发布后安装 `prism-cert-renew.timer`，确认 enabled/next run，并通过 systemd 启动续期服务验证正式续期与 Nginx reload；持续检查 journal 与证书到期时间。
5. **OpenSSH 版本标识**：生产已升级到 `openssh-server-9.3p2-16.oc9.x86_64`；本机 RPM changelog 列出修复 CVE-2026-35414、CVE-2026-35385、CVE-2025-26465，CVE-2026-35386 已由既有回补覆盖；`sshd -t`、active 状态和公钥登录复测通过，密码/KbdInteractive 认证关闭。协议 banner 仍显示上游 `9.3`，这是发行版回补包的版本基线；不要仅凭 banner 判断未打补丁或擅自编译替换发行版 sshd。
6. **百万 Token 真实模型验收**：现有两条测试是合成 transcript、估算 Token 和 mock；真实供应商 1M-token 请求、成本、语义保持及无截断端到端未验证。
7. **第一轮 R1 实时数据**：危急审批待办、磁盘水位和 Token 日用量没有在本轮读取；旧报告数字不是当前生产状态。需按 [验收表附录](./ACCEPTANCE_外部审查问题核验修复.md#附录报告中的第一轮遗留项) 使用管理员权限另行复核。
