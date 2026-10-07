# 黑白盒可靠性阶段交付

候选 v4.0.79 修复了多文件源码 grounding 引文、压缩拆分重复携带元数据、Python 黑盒端口来源检查遗漏间接动态调用与 `os.environb` 写入、部署注入黑盒跳过依赖准备，以及黑盒启动超时未回收 Node 子进程等已复现问题。额外收紧了静态字符串复杂度溢出的作用范围，保持危险反射/导入 fail-closed，同时允许复杂合法 URL 参数；新增 SIGINT/SIGTERM runner 进程回收回归。最终黑白盒/源码/runner 定向回归在当前 SHA 重跑为 244 passed。GitHub Actions run `37576385449` 于 SHA `d3dc73eeddb802d2b8d9408cf24a9eee014c0247` 三 job 全绿：后端 6504 passed、5 warnings、0 failed/error/skipped，含实际执行并通过的六项 Redis Lua 行为测试；覆盖率 83%，Ruff、compileall、pip-audit 通过。前端 1850 passed、142 个文件，依赖审计、ESLint、类型检查和构建通过。部署执行器 137 passed、2 skipped（可选 Linux 内核测试），33 项发布绑定、部署故障注入、ShellCheck 与 Compose 检查通过。此前两轮 CI 分别揭示解释器/fixture 冲突和 Redis 容器路径比较缺陷，均已修复，详见证据/59–60。生产发布后验收仍未完成。

2026-10-07 生产健康接口仍报告 v4.0.78 / 957a6588fc59b924ed1ebae81446a361b37c28bf；v4.0.79 未部署。Docker 由用户关闭，SSH 部署凭据不可用，因此无法完成线上发布或真实 Worker/Safari 任务。当前只能说明已测范围的自动化结果，不可承诺所有真实业务绝不失败。部署后仍须由普通 Safari 执行白盒、启动失败负例和 loopback 黑盒正例，复核 Worker 原始回执、报告、任务终态与资源清理后才可关闭任务。
