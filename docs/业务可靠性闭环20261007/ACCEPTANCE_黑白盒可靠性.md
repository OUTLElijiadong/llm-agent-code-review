# 黑白盒业务可靠性验收记录

## 候选 v4.0.79（生产发布前）

修复前红测：在生产基线临时 worktree 运行新增回归，7 failed / 62 deselected；具体失败项见证据/43。候选同一批修复后定向回归为 142 passed，见证据/35。后续独立红队进一步发现动态调用、描述符、容器索引、`os.environb` 和 runner 子进程清理问题；最终红绿/全量结果见证据/49。复核时又发现静态字符串组合上限误伤复杂但合法的 URL 查询参数；现仅对导入、反射及动态方法解析的局部溢出执行 fail-closed，危险动态导入仍由上限负例覆盖，合法查询正例见 `test_blackbox_contract_keeps_encoded_high_complexity_query_with_trusted_port`。

候选从生产 v4.0.78 commit 957a6588fc59b924ed1ebae81446a361b37c28bf 的相同 Git tree（b85c818b56f764ba7182fb688db0b9308072bade）开始，当前 v4.0.79 修复范围：多文件源码正文原样进入 grounding 以保留逐字引文；递归压缩请求只带来源 ID 和当前正文，避免重复提交整个文件元数据；Python 黑盒动态端口来源检查覆盖间接调用、helper 参数/返回、反射与 `os.environb` 别名；普通及部署注入 runner 的离线依赖准备失败会阻止假通过；黑盒探活超时后按进程组回收应用及其子进程。

- 本地候选后端全量回归（`backend/.venv311/bin/python -m pytest -q --no-cov`）：6495 passed、6 skipped、5 warnings，见证据/57。6 项 Redis 用例按测试配置跳过，不计为通过；后续 CI 已在隔离 Redis Unix socket 上实际执行六项，见证据/60。
- 最终黑白盒/源码/runner 定向回归：244 passed、3 warnings，见证据/56；新增真实 SIGINT/SIGTERM runner 中断与 Node 子进程回收测试。
- Redis 限流场景：block 持续、亚秒过期、admission window、失败结算窗口、旧检查窗口、Unix socket factory 均通过，证据/36。临时无网络容器、socket 和数据已删除。
- 前端：1850 tests passed / 142 files；ESLint 与 vue-tsc/Vite v4.0.79 构建通过，证据/38–40。
- 发布绑定：33 项通过；同一 test_scripts.sh 所含故障注入矩阵对备份、校验、构建、迁移、切换、健康、HTTPS失败及回滚拒绝进行了预期断言，证据/41。
- 持续集成：GitHub Actions run [`37576385449`](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/37576385449) 在 SHA `d3dc73eeddb802d2b8d9408cf24a9eee014c0247` 三个 job 全部成功。后端全量 6504 passed、5 warnings、0 failed/error/skipped，覆盖率 83%；六个真实 Redis Lua 限流场景和三个 socket 路径测试均执行。Ruff、compileall、pip-audit 通过，未发现已知依赖漏洞。前端 142 个测试文件、1850 项通过，npm audit 0 vulnerabilities，ESLint、类型检查及构建通过。部署回归 137 passed、2 skipped（opt-in Linux kernel cases），33 项绑定和故障注入脚本、ShellCheck、Compose 校验通过。第二轮 CI 暴露的容器/宿主机 Redis socket 路径比较错误已复现并修正；详见证据/59–60。
- Ruff（实现与新增测试）、compileall、ShellCheck error 级、runner/install 脚本 `bash -n`、`git diff --check` 均通过；默认 ShellCheck 模式仍报告已有 warning/info，见证据/49 与最终检查。

以上仅为候选自动化证据，不能表述为生产 Worker 验收。grounding 无效仍失败关闭；不可拆分小片或调用预算耗尽仍返回失败。它提高已观察故障路径的确定性，不证明任意外部项目、模型、语言、依赖或网络环境都成功。

## 生产基线与发布后验收

2026-10-07 只读请求 `GET https://lijiadong.cn/healthz` 与 `/readyz` 分别返回 `{"status":"ok","version":"4.0.78","release":"957a6588fc59b924ed1ebae81446a361b37c28bf"}`、`{"status":"ready","version":"4.0.78","release":"957a6588fc59b924ed1ebae81446a361b37c28bf"}`。普通 Safari 新加载的工作台页脚也显示 v4.0.78。一个此前打开的 `/readyz` 标签仍显示旧的 v4.0.75；真实点击刷新后更新为 v4.0.78，与接口一致，因此这次观察支持“旧标签页保留旧响应”，不支持当前新加载页面存在版本错配的结论。详见证据/54、证据/61。schema 基线为 `062_audit_log_action_length`。

本轮在普通 Safari 代码沙箱页只读查看了已有生产任务，没有创建、重试、取消或删除任务，也没有下载附件。观察到一个黑盒任务失败（`sbx_3950674251e94925afdbad92`）：源码来源校验先报分片引文无法核验，后续压缩覆盖校验虽通过，动态用例仍引用不存在的 `HTTPError`、`Path`，再触发 `'Name' object has no attribute 'func'`；最终应用未就绪、`/` 返回状态码 0，黑盒失败。另一条白盒任务 `sbx_1fdc7440f3d34081b5ef7f55` 的确定性检查通过，但 AI 动态补充因源码压缩核验失败而跳过。黑盒任务 `sbx_0bb11491c12a402d9b0fda43` 的 `/health` 路由与 Agent 断言通过（200），但动态用例三轮引用不存在符号后回退，因此其结论仅为确定性黑盒通过。此为现有历史任务的页面观察，不是本轮新建任务或修复后生产复测；逐项证据见证据/61。

v4.0.79 仍是候选，尚未在生产 Worker 执行本轮新样本。部署检查再次确认 Docker daemon 未运行，`li@81.70.251.90` SSH 返回 `Permission denied (publickey,...)`，GitHub 仓库没有生产部署 workflow；因此不能声称候选已部署或这些生产失败已由候选修复。发布后必须针对已观察的来源压缩/动态用例失败重跑原场景，并执行 Python 白盒、无入口样本启动失败负例、loopback-only runnable service 黑盒正例；每个关键场景至少独立重复三轮，核对动态用例清单、Worker 原始回执、报告回读、任务终态及资源清理。健康页不替代业务测试。

## 未覆盖范围

- 隔离 Redis 场景通过不覆盖生产 Redis 故障转移或网络分区。
- Node/PHP/Go/Java runner 测试不代表其源码语义 grounding 已达到 Python 覆盖。
- 模拟压缩结果不证明真实供应商端到端接收并理解超过 1,000,000 token。
- Safari 生产任务和资源清理闭环待部署后执行。
