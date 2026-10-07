# 黑白盒业务可靠性验收记录

## 候选 v4.0.79（生产发布前）

修复前红测：在生产基线临时 worktree 运行新增回归，7 failed / 62 deselected；具体失败项见证据/43。候选同一批修复后定向回归为 142 passed，见证据/35。后续独立红队进一步发现动态调用、描述符、容器索引、`os.environb` 和 runner 子进程清理问题；最终红绿/全量结果见证据/49。复核时又发现静态字符串组合上限误伤复杂但合法的 URL 查询参数；现仅对导入、反射及动态方法解析的局部溢出执行 fail-closed，危险动态导入仍由上限负例覆盖，合法查询正例见 `test_blackbox_contract_keeps_encoded_high_complexity_query_with_trusted_port`。

候选从生产 v4.0.78 commit 957a6588fc59b924ed1ebae81446a361b37c28bf 的相同 Git tree（b85c818b56f764ba7182fb688db0b9308072bade）开始，当前 v4.0.79 修复范围：多文件源码正文原样进入 grounding 以保留逐字引文；递归压缩请求只带来源 ID 和当前正文，避免重复提交整个文件元数据；Python 黑盒动态端口来源检查覆盖间接调用、helper 参数/返回、反射与 `os.environb` 别名；普通及部署注入 runner 的离线依赖准备失败会阻止假通过；黑盒探活超时后按进程组回收应用及其子进程。

- 最新后端完整测试（`backend/.venv311/bin/python -m pytest -q --no-cov`）：6495 passed、6 skipped、5 warnings，见证据/57。6 项 Redis 用例按测试配置跳过，不计为通过；隔离 Redis Unix socket 集成另见证据/36。
- 最终黑白盒/源码/runner 定向回归：244 passed、3 warnings，见证据/56；新增真实 SIGINT/SIGTERM runner 中断与 Node 子进程回收测试。
- Redis 限流场景：block 持续、亚秒过期、admission window、失败结算窗口、旧检查窗口、Unix socket factory 均通过，证据/36。临时无网络容器、socket 和数据已删除。
- 前端：1850 tests passed / 142 files；ESLint 与 vue-tsc/Vite v4.0.79 构建通过，证据/38–40。
- 发布绑定：33 项通过；同一 test_scripts.sh 所含故障注入矩阵对备份、校验、构建、迁移、切换、健康、HTTPS失败及回滚拒绝进行了预期断言，证据/41。
- 持续集成：新增 `.github/workflows/ci.yml`，按候选 SHA 执行后端全量回归与覆盖率（启动网络隔离 Redis，让 Lua 限流 6 项从 skip 转为真实执行）、前端依赖审计/测试/ESLint/构建、部署执行器/发布故障矩阵/ShellCheck/Compose 配置校验。首次 GitHub run `37572839777` 的前端 job 成功、部署脚本 job 失败、后端 job 仍在运行；部署日志未提供失败阶段，工作树已加入阶段标记，本地完整复跑通过，最终 GitHub 复跑仍待完成。证据见/59。
- Ruff（实现与新增测试）、compileall、ShellCheck error 级、runner/install 脚本 `bash -n`、`git diff --check` 均通过；默认 ShellCheck 模式仍报告已有 warning/info，见证据/49 与最终检查。

以上仅为候选自动化证据，不能表述为生产 Worker 验收。grounding 无效仍失败关闭；不可拆分小片或调用预算耗尽仍返回失败。它提高已观察故障路径的确定性，不证明任意外部项目、模型、语言、依赖或网络环境都成功。

## 生产基线与发布后验收

2026-10-07 只读请求 `GET https://lijiadong.cn/healthz` 返回 `{"status":"ok","version":"4.0.78","release":"957a6588fc59b924ed1ebae81446a361b37c28bf"}`，详见证据/54。schema 基线为 `062_audit_log_action_length`。v4.0.79 仍是候选，线上 Worker/Safari 尚未执行本轮新样本；本轮无法部署（Docker 由用户关闭，SSH 部署凭据不可用）。发布后必须真实执行 Python 白盒、无入口样本启动失败负例、loopback-only runnable service 黑盒正例，并核对动态用例清单、Worker 原始回执、报告回读、任务终态及资源清理。健康页不替代业务测试。

## 未覆盖范围

- 隔离 Redis 场景通过不覆盖生产 Redis 故障转移或网络分区。
- Node/PHP/Go/Java runner 测试不代表其源码语义 grounding 已达到 Python 覆盖。
- 模拟压缩结果不证明真实供应商端到端接收并理解超过 1,000,000 token。
- Safari 生产任务和资源清理闭环待部署后执行。
