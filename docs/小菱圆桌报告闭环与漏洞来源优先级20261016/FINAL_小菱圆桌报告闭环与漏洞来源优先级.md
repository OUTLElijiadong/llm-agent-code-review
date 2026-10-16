# 最终交付：小菱圆桌、报告闭环与漏洞来源优先级

## 完成内容

修复小菱停止后的圆桌与派生 AgentTeam 续跑屏障，确保取消终态不被自动评价、`task.result` 或迟到结果覆盖；只有当前用户在同一会话明确要求继续时才能创建新圆桌会话。Native Responses 在途 HTTP stream 现会响应持久取消状态并清理 HTTPX 响应流上下文；WebSocket 能恢复真实讨论快照，模型等待期间发送阶段心跳，前端区分成功、部分、失败和取消。MockTransport 测试没有观测真实 TCP，也不证明 provider 已停止计算或计费；旧同步圆桌请求也仍不能强杀。

报告模块补齐可达的 Review、Sandbox 与 Pentest 终态报告，保留真实覆盖、跳过、失败和错误信息。漏洞来源界面将 CNVD 与 CNNVD 分开，并禁止从标题或 CWE 推测官方记录。管理员页面新增每日 Token 热力日历、未知用量与真实零值区分、波浪显现和数字滚动；登录成功光球/对勾、团队真实依赖星图、根路径追溯及文档流展开也已实现。

## 验收与生产发布

后端全量测试 7,234 项通过、6 项跳过，覆盖率 84%；前端 151 个测试文件、1,987 项通过；报告导出专项 79 项通过；布局 Playwright E2E 37/37 通过。类型检查、生产构建、lint、Ruff、compileall 与部署脚本回归通过。

Prism v4.0.109 已于 2026-10-16T14:32:56Z 完成 `all` 发布，发布 SHA 为 `a4d70e8fedfe545a844deb4dc753565178020ea9`。发布前 979,659,816 字节数据库备份经 SHA-256、gzip 和独立容器恢复验证，恢复 109 张表；Alembic 为 `064_remote_pentest_chain (head)`。前后端镜像、current/previous 账本、HTTPS 健康/就绪端点及 `ops-check` 全部与目标版本一致。完整凭证见 [[ACCEPTANCE_小菱圆桌报告闭环与漏洞来源优先级|验收记录]] 与 `evidence/production/release-v4.0.109.json`。

Safari 真实账号交互未完成：当前会话对该站点的 Computer Use 被硬性禁止，故不把本地 E2E、静态资源检查或 HTTP 冒烟冒充真实 GUI 验收。剩余产品能力与停止计费边界见 [[TODO_小菱圆桌报告闭环与漏洞来源优先级|待办清单]]。
