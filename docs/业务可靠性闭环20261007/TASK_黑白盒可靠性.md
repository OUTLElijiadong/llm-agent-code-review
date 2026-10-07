# 黑白盒可靠性原子任务

| 项 | 工作 | 状态 |
| --- | --- | --- |
| 1 | 记录生产失败和 v4.0.78 基线，区分候选/生产证据 | 完成 |
| 2 | 修复源码 grounding，覆盖引文、JSON 特殊字符和多文件边界 | 完成，v4.0.79 |
| 3 | 修复递归拆分重复携带完整文件元数据，校验分片拼接和单次出现 | 完成，v4.0.79 |
| 4 | 修复 Python AST 动态端口来源漏检；覆盖反射、容器/描述符/委托、参数/返回值、os.environb 和字节别名，同时保留只读 helper/无关环境变量正例 | 完成，v4.0.79 |
| 5 | 修复 runner 依赖准备跳过和黑盒超时遗留子进程，补充普通/注入/combined 回归 | 完成，v4.0.79 |
| 6 | 后端/前端全量回归、静态检查、构建、发布绑定/故障注入 | v4.0.80 本地后端全量 6511 passed/6 skipped/5 warnings，三文件定向 280 passed，前端 1851 passed、lint/typecheck/build 通过；精确 SHA Business CI、CodeQL 成功。详见证据/68 |
| 7 | 从生产精确提交发布 v4.0.80，核对备份、迁移、运行 SHA、镜像、健康与资产 | 主后端/前端、账本、健康、备份、隔离恢复、迁移与 HTTPS 已核验；ops-check degraded：磁盘 89%。独立复核另发现 decoy 实际仍为健康 v4.0.79，与当前 Compose 期望不一致；详见证据/68 |
| 8 | 普通 Safari 实测首个白盒样本并回读多 Agent 报告 | 完成但仅部分通过：任务 `sbx_65b29eecb2754ab5a856a023` 的确定性 runner 通过，AI 动态用例三轮被 grounding 拒绝，整体 coverage=partial；详见证据/64 |
| 9 | 复核生产证据、修复白盒 grounding 误拒绝与“已通过”覆盖状态误导 | 修复已随 v4.0.80 发布；生产 Worker 与 UI 复测未完成，不能关闭验收 |
| 10 | 建立候选 SHA 自动化门禁，覆盖后端全量及隔离 Redis、前端 lint/test/build、部署故障注入和 Compose 配置 | v4.0.80 精确 SHA `396ca2312cefb229d6bbccc81315d41ce64a1ce7` 的 Business CI run `37611845225` 与 CodeQL run `37611845349` 全部成功；详情见证据/68 |
| 11 | 为 inspect/importlib 等受信标准库反射链增加 grounding 红绿回归，保留项目 API 幻觉拒绝 | 本地已复现并修复；导入、赋值、参数/循环/with、walrus、match、del 遮蔽负例均通过；三文件 280 passed，精确 SHA CI 通过；生产 Worker 复测待执行 |
| 12 | 结构化覆盖为 partial 时让任务列表、详情标签和结论显示“部分通过” | 本地回归与候选构建通过，精确 SHA CI 通过；生产 Safari 页面标签复测待执行 |
| 13 | 复测白盒动态用例、黑盒启动失败负例和 loopback runnable 服务正例；完成三轮关键路径并清理本轮测试数据 | 未完成：发布后 Safari 真实点击被中断，尚未在 v4.0.80 Worker 执行新样本；历史任务保留 72h 且无终态删除入口，本轮未创建新任务。到期清理需后续核验 |
| 14 | 修复内联标准库调用结果的 grounding 误拒绝，复测生成器与黑白盒门禁 | v4.0.81 候选完成；红绿回归、沙箱上下文定向 110 passed/2 warnings、本地全量 6556 passed/6 skipped/5 warnings、Business CI SHA `b5c81173` 为 6562 passed/5 warnings/0 skipped、CodeQL/前端/部署 job 全绿；Ruff/compileall/diff-check 通过。生产发布与 Worker 复测待完成。详见证据/70 |
