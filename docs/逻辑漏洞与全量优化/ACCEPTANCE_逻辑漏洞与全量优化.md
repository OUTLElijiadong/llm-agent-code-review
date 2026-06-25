# ACCEPTANCE_逻辑漏洞与全量优化

## 完成情况

| 项目 | 状态 | 证据 |
| --- | --- | --- |
| 讨论审预检项目归属校验 | 已完成 | `/api/discuss/start` 增加 owner/admin 校验；越权单测通过 |
| 讨论 WebSocket 会话 owner 校验 | 已完成 | `DiscussionSession.owner_user_id` + WS 连接校验；owner/admin 单测通过 |
| 任务问题列表对象级授权 | 已完成 | `review_service.list_task_issues` 增加任务 owner/admin 和 deleted 校验 |
| 问题详情对象级授权 | 已完成 | `issue_service.get_issue` 增加所属任务 owner/admin 校验 |
| 规则写操作权限 | 已完成 | `rule_service.toggle/update/delete` 增加 owner/admin 校验；Agent 旁路同步更新 |
| Clarify 回填用户绑定 | 已完成 | ClarifyStore 记录 `user_id`，回填接口拒绝跨用户执行 |
| 用户自定义 API SSRF 防护 | 已完成 | 统一 `validate_ai_base_url`，保存、测试连接、运行时解析均接入 |
| 模型 HTTP 调用安全收口 | 已完成 | DeepSeekAgent/BaseAgent 规范化 base URL，并禁用环境代理继承 |
| Caddy HTTPS-only 恢复 | 已完成 | HTTP 入口只 301 到 HTTPS；HTTPS 入口启用 HSTS 和基础安全响应头 |
| 前端低风险修复 | 已完成 | redirect 拒绝 `//`；外链补齐 `noopener noreferrer`；API 配置说明同步 |

## 验证记录

| 命令 | 结果 |
| --- | --- |
| `cd backend && .venv/bin/python -m pytest -q` | 通过，`160 passed` |
| `cd backend && .venv/bin/python -m ruff check app tests` | 通过 |
| `cd backend && .venv/bin/python -m compileall app tests` | 通过 |
| `cd frontend && npm run build` | 通过 |
| `cd deploy && docker compose config --quiet` | 通过 |
| `docker run --rm ... caddy validate` | 未运行成功：本机 Docker daemon 未启动，无法连接 `/Users/li/.docker/run/docker.sock` |

## 验收结论

本轮仓库内可确定的高风险逻辑漏洞已修复，并已通过后端全量测试、静态检查、编译检查、前端构建和 Compose 配置校验。剩余 Caddyfile 语法建议在 Docker/Caddy 可用环境补跑 `caddy validate`。

