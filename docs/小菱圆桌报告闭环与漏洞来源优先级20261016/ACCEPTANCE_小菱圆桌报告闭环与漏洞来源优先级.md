# 验收：小菱圆桌、报告闭环与漏洞来源优先级

## 验收结果

| 子任务 | 结果 | 验收说明 |
|---|---|---|
| T1 停止与续会屏障 | 通过（应用侧请求中断） | 父 Responses 取消会取消同 owner、surface、session、root run 的未终态团队和任务；圆桌状态保持 cancelled，迟到结果不回投续跑。Native Responses 在途异步 HTTP stream 与持久取消轮询竞速，检测到取消后请求协程进入清理；取消/失败/部分不能续会，只有同用户当前会话的新明确继续输入可创建新会话。 |
| T2 圆桌主持与实时状态 | 部分通过 | WebSocket 快照、阶段心跳、取消终态与前端状态区分已覆盖。真实小菱模型主持、用户插话后主持优先调度和按任务创建临时专家未实现，固定专家组合未改变。 |
| T3 代码审查报告闭环 | 部分通过 | success、partial coverage、failed、cancelled 等可达状态进入报告列表/详情，错误与文件级覆盖事实保留；审查专用执行器没有独立 stopped/expired 状态转换器，不能宣称自身可达这些状态。 |
| T4 沙箱与授权渗透报告 | 通过（受格式契约限制） | 沙箱 success/partial/failed/stopped/expired 及渗透测试 success/failed/cancelled/expired/interrupted/stale cleanup 有可读终态报告；发布失败不伪报成功，取消/到期重试可补偿。领域导出遵循现有契约：JSON；沙箱另有 Markdown，未支持的 HTML/PDF/DOCX 明确返回限制。 |
| T5 漏洞来源优先级 | 通过（展示层） | CNVD 与 CNNVD 分别展示；只有官方记录编号或官方详情链接才可标为相应来源。无匹配证据时显示 OWASP fallback。现有业务数据不含官方记录标识，故无实时同步或逐条官方匹配结论。 |
| T6/T7 管理员 Token 与动效 | 通过 | 7/30/90 天用量趋势保留；新增按 UTC 星期对齐的热力日历，真实 0、未知、非零分别呈现；数字滚动复用 `useCountUp`；登录成功按钮收成光球并显示对勾；依赖图仅使用 `depends_on` 真实边并支持根节点可达路径追溯；详情在文档流内展开并推动后续内容。 |

## 验证证据

- 后端受影响文件回归：696 项通过，包含 Responses 取消/续会、团队与 WS、报告闭环、沙箱/渗透及管理员 Token 用量；两条第三方弃用警告，无失败。测试环境为临时 Python 3.11 虚拟环境。
- 前端目标组件回归：Vitest 定向套件 158/158；Agent Chat/Trace 子集复跑 73/73。
- 前端生产构建：Vue 类型检查与 Vite 生产构建通过。
- 前端质量与布局：目标 ESLint 通过；布局 Playwright E2E 37/37 通过，覆盖生产圆桌抽屉标题栏及窄屏溢出。
- 后端静态检查与差异：Ruff、compileall、`git diff --check` 与 `git diff --cached --check` 通过；前端/后端未解决冲突均清零。
- 生产发布与浏览器验收：待完成。候选版本 v4.0.109，基于生产 v4.0.108 精确 SHA；须在后续追加发布 SHA、备份与隔离恢复、镜像/账本/migration/health/HTTPS 及真实账号页面证据。

## 边界

- 本地测试不等于线上行为验收；生产部署和真实账号浏览器验收状态以本节后续追加记录为准。
- Native Responses 的异步 HTTP stream 取消路径已通过 MockTransport 验证 HTTPX 响应流上下文清理；未观测真实 TCP 断开，也不证明 provider 端计算或计费即时停止。圆桌旧同步 provider 请求若已在线程池或独立 Worker 中启动，仍不能由应用强杀，可能持续至客户端超时并计费。
- 小菱真实模型主持和任务级临时专家未完成，审查专用 stopped/expired 转换入口、CNVD/CNNVD 实时数据源以及 Sandbox/Pentest 的 PDF/HTML/DOCX 等价导出均未实现。
