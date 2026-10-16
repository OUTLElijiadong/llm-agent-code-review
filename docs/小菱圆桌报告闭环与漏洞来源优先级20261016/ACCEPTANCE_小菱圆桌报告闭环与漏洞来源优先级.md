# 验收：小菱圆桌、报告闭环与漏洞来源优先级

## 验收结果

| 子任务 | 结果 | 验收说明 |
|---|---|---|
| T1 停止与续会屏障 | 通过（应用侧请求中断） | 父 Responses 取消会取消同 owner、surface、session、root run 的未终态团队和任务；圆桌状态保持 cancelled，迟到结果不回投续跑。Native Responses 在途异步 HTTP stream 与持久取消轮询竞速，检测到取消后请求协程进入清理；取消/失败/部分不能续会，只有同用户当前会话的新明确继续输入可创建新会话。供应商端计算/计费即时停止尚不能保证。 |
| T2 圆桌主持与实时状态 | 部分通过 | WebSocket 快照、阶段心跳、取消终态与前端状态区分已覆盖。真实小菱模型主持、用户插话后主持优先调度和按任务创建临时专家未实现，固定专家组合未改变。 |
| T3 代码审查报告闭环 | 部分通过 | success、partial coverage、failed、cancelled 等可达状态进入报告列表/详情，错误与文件级覆盖事实保留；审查专用执行器没有独立 stopped/expired 状态转换器，不能宣称自身可达这些状态。 |
| T4 沙箱与授权渗透报告 | 通过（受格式契约限制） | 沙箱 success/partial/failed/stopped/expired 及渗透测试 success/failed/cancelled/expired/interrupted/stale cleanup 有可读终态报告；发布失败不伪报成功，取消/到期重试可补偿。领域导出遵循现有契约：JSON；沙箱另有 Markdown，未支持的 HTML/PDF/DOCX 明确返回限制。 |
| T5 漏洞来源优先级 | 通过（展示层） | CNVD 与 CNNVD 分别展示；只有官方记录编号或官方详情链接才可标为相应来源。无匹配证据时显示 OWASP fallback。现有业务数据不含官方记录标识，故无实时同步或逐条官方匹配结论。 |
| T6/T7 管理员 Token 与动效 | 通过 | 7/30/90 天用量趋势保留；新增按 UTC 星期对齐的热力日历，真实 0、未知、非零分别呈现；数字滚动复用 `useCountUp`；登录成功按钮收成光球并显示对勾；依赖图仅使用 `depends_on` 真实边并支持根节点可达路径追溯；详情在文档流内展开并推动后续内容。 |

## 验证证据

- 后端全量：临时 Python 3.11 环境执行 `python -m pytest -q --tb=short`，7,234 项通过、6 项跳过、0 失败，覆盖率 84%。报告导出专项另执行 79 项，全部通过。
- 前端全量：Vitest 151 个测试文件、1,987 项通过；`vue-tsc`、Vite 生产构建及 lint 通过。布局 Playwright E2E 37/37 通过，覆盖圆桌抽屉标题栏与窄屏溢出。
- 发布脚本与静态检查：`bash deploy/tests/test_scripts.sh` 通过（发布绑定测试 33 项及部署故障注入矩阵）；候选 `backend/app` Ruff、`compileall`、Shell 语法、`git diff --check` 与 `git diff --cached --check` 通过。
- 生产发布：2026-10-16T14:32:56Z 完成 `all` 发布，版本 `4.0.109`，SHA `a4d70e8fedfe545a844deb4dc753565178020ea9`；前序版本 `4.0.108`，SHA `1b9d00e92b35c268d017fe0b78fc520ed02fb21e`。完整独立证据见 [生产发布回执](evidence/production/release-v4.0.109.json)。
- 数据保护与迁移：发布前备份 SHA-256 `27cec1358bb84beceded8d17fd14b6c02a96f8b802a291b1f69cfd745c1eeb31`，979,659,816 字节；sidecar 校验、gzip 校验和独立容器 `cr_testdb` 恢复均通过，恢复 109 张表；数据库 Alembic 为 `064_remote_pentest_chain (head)`。
- 生产运行：current/previous 账本分别指向 4.0.109 / 4.0.108；Backend 镜像 ID `sha256:7a315abd1cf2a3ce9109983e2155990037b189aab2b805a7caa8badcf6800b45`，Frontend 镜像 ID `sha256:dc46919c9a97bf14c9ff5eb0b47a09478af6b2ccdc5b2d1a1c339d7c2705ae5e`。Backend、Frontend、Decoy、MySQL、ClamAV 均为 healthy；发布 API 冒烟与发布后 `ops-check` 通过，release/containers/disk/memory/backup/alembic/https 均为 `ok`。
- 公网 HTTPS：`/healthz` 与 `/readyz` 均返回版本 4.0.109 和该发布 SHA；HTTP 首页 308 跳转 HTTPS，HTTPS 首页 200；管理员总览、登录和 Agent 聊天的生产静态资源均返回 JavaScript 200。
- 真实 Safari 账号交互：未执行。当前会话对目标站点的 Computer Use 被硬性禁止，因此没有通过 GUI 验证停止按钮、管理员图表和动画；本地测试、E2E 和 HTTP 证据不替代此项。

## 边界

- 本地测试和静态资源/HTTP 检查不等于真实账号浏览器行为验收；生产部署已经完成，但真实 Safari 账号交互仍未核验。
- Native Responses 的异步 HTTP stream 取消路径已通过 MockTransport 验证 HTTPX 响应流上下文清理；未观测真实 TCP 断开，也不证明 provider 端计算或计费即时停止。圆桌旧同步 provider 请求若已在线程池或独立 Worker 中启动，仍不能由应用强杀，可能持续至客户端超时并计费。
- 小菱真实模型主持和任务级临时专家未完成，审查专用 stopped/expired 转换入口、CNVD/CNNVD 实时数据源以及 Sandbox/Pentest 的 PDF/HTML/DOCX 等价导出均未实现。
