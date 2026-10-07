# 黑白盒可靠性阶段交付

候选 v4.0.79 修复了多文件源码 grounding 引文、压缩拆分重复携带元数据、Python 黑盒端口来源检查遗漏间接动态调用与 `os.environb` 写入、部署注入黑盒跳过依赖准备，以及黑盒启动超时未回收 Node 子进程等已复现问题。额外收紧了静态字符串复杂度溢出的作用范围，保持危险反射/导入 fail-closed，同时允许复杂合法 URL 参数；新增 SIGINT/SIGTERM runner 进程回收回归。最终黑白盒/源码/runner 定向回归在 SHA `d3dc73eeddb802d2b8d9408cf24a9eee014c0247` 重跑为 244 passed。GitHub Actions run `37576385449` 三 job 全绿：后端 6504 passed、5 warnings、0 failed/error/skipped，含实际执行并通过的六项 Redis Lua 行为测试；覆盖率 83%，Ruff、compileall、pip-audit 通过。前端 1850 passed、142 个文件，依赖审计、ESLint、类型检查和构建通过。部署执行器 137 passed、2 skipped（可选 Linux 内核测试），33 项发布绑定、部署故障注入、ShellCheck 与 Compose 检查通过。此前两轮 CI 分别揭示解释器/fixture 冲突和 Redis 容器路径比较缺陷，均已修复，详见证据/59–60。

2026-10-07 v4.0.79 已实际部署到生产，运行 SHA `d3dc73eeddb802d2b8d9408cf24a9eee014c0247`、Alembic 062；备份校验、隔离恢复 104 表、API smoke、HTTPS/健康页、资产切换和运行镜像一致均通过。部署后 `ops-check` 为 degraded/can_continue，唯一告警为磁盘 88%（警告线 85%、严重线 95%）；该项需人工 dry-run 审阅，未执行清理。普通 Safari 发布后白盒任务 `sbx_65b29eecb2754ab5a856a023` 终态 succeeded，但报告结构化 coverage=partial：确定性执行通过，AI 动态用例因标准库反射符号误拒绝而 skipped，且项目未发现测试文件。Safari 状态标签错误显示绿色“已通过”。本地已先复现并修复 grounding 误拒绝与前端覆盖标签，版本升至 v4.0.80；候选门禁、生产 Worker 复测与黑盒正负样本闭环仍待完成。详见证据/64–66。

v4.0.80 本地全量验证：后端 6511 passed、6 skipped、5 warnings（242.39 秒）；grounding/沙箱报告三文件定向回归 280 passed、3 warnings；前端 142 个测试文件、1851 passed；前端 lint、`vue-tsc`/Vite 构建、后端 Ruff/compileall 通过。后端全量用例有 6 项 skip，因此候选精确 SHA 的 CI 结果仍是必要门禁。终态任务删除受产品能力限制，任务记录默认 72h 到期；目前只确认 Worker 容器和作业目录已清理，未确认任务记录已经删除。

## v4.0.80 交付状态

v4.0.80 / `396ca2312cefb229d6bbccc81315d41ce64a1ce7` 已于 2026-10-07 发布至生产。精确 SHA 的 Business CI、CodeQL 均成功；候选本地后端 6,511 passed、6 skipped，前端 1,851 passed。生产新备份与 104 表隔离恢复、Alembic 062、后端/前端健康、API smoke、HTTPS、buildInfo 版本与运行 SHA 均通过。具体数据见证据/68。

**整体黑白盒业务验收仍未完成。** Safari 在尝试刷新时被用户切换到另一页面，随后停止浏览器操作；没有在 v4.0.80 上重跑 Worker 白盒或黑盒任务，也没有完成用户界面读回。因此修复是否已在生产真实任务中闭环，尚无实测证据。当前仍待普通 Safari 继续实测标准库反射白盒与 partial 显示、无入口黑盒负例、loopback 可运行 Web 服务黑盒正例；关键场景计划各 3 轮。现有生产测试记录保留 72 小时，无终态删除入口，本轮未创建任务或即时删除历史记录。诱捕层旧 v4.0.79 镜像仍健康，因可选构建停滞未更新；磁盘巡检为 89% 并处于告警态。详见证据/68–69。
