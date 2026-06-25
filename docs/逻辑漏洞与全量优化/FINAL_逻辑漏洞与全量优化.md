# FINAL_逻辑漏洞与全量优化

## 交付总结

本次完成了智能代码审查平台的逻辑漏洞检查和高风险优化，重点修复已认证用户之间的对象级越权、讨论审权限不一致、规则治理旁路、用户自定义模型 API SSRF，以及生产 Caddy 入口回退问题。

## 核心修复

- 讨论审与普通审查统一项目/文件权限边界，普通用户不能启动他人项目代码的圆桌讨论审。
- 讨论 WebSocket 会话绑定创建者，连接时加载数据库用户并要求 owner/admin。
- 审查任务问题列表、问题详情均校验所属任务 owner/admin，避免 ID 枚举泄露。
- 审查规则启停、更新、删除统一校验规则 owner/admin；普通用户不能改全局内置规则或他人规则。
- Clarify 主动追问绑定创建用户，防止 clarify_id 泄露后的跨用户执行。
- 用户 API 配置新增 SSRF 防护：默认阻止 localhost、单标签内网主机名、`.local/.internal/.lan` 等私有域、内网/链路本地/保留 IP。
- 新增 `ALLOW_PRIVATE_AI_BASE_URL` 和 `ENFORCE_AI_BASE_URL_DNS_CHECK` 环境开关，默认安全，允许管理员按部署场景显式放行。
- 模型调用关闭 `httpx` 环境代理继承，减少环境变量代理导致的非预期外呼路径。
- Caddy 恢复 HTTPS-only：HTTP 只跳转 HTTPS，HTTPS 启用 HSTS、nosniff、frame deny、Referrer-Policy、Permissions-Policy。
- 前端收紧登录 redirect 和外链安全属性，同步 API 配置页面的内网模型说明。

## 测试结果

- 后端全量测试：`160 passed`
- 后端 ruff：通过
- 后端 compileall：通过
- 前端 build：通过
- Docker Compose config：通过

## 风险说明

- 当前默认不启用 AI API 域名 DNS 解析校验，因为当前本地网络存在 fake-IP/代理解析，强制 DNS 校验会误伤公网模型 API。若生产环境 DNS 可信且无 fake-IP，可设置 `ENFORCE_AI_BASE_URL_DNS_CHECK=true` 加强防护。
- Caddyfile 已按文本和 Compose 层校验，但本机 Docker daemon 未运行，未能执行 Caddy 官方容器的 `caddy validate`。

