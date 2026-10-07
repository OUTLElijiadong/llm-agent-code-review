# 黑白盒业务可靠性验收记录

## v4.0.82 当前生产状态（2026-10-08）

v4.0.82 / `fad2f2ddd364486f5bddcd0e1059c343e67ef45a` 已部署到生产主站与独立 Sandbox Worker。Worker profile 已从 v4.0.78 runner 更新至当前 v4.0.82 runner 镜像，五种语言的 profile digest 与镜像 ID、runner 源码 SHA 一致；执行器 `runsc`、五语言 whitebox/blackbox/combined 支持和 browser blackbox health 均 ready。五种语言隔离白盒冒烟通过，Python loopback 黑盒 smoke 三轮通过。详情及依赖下载/部署原因见[证据 73](证据/73-v4082生产部署与Worker复核-20261008.md)。

这些 smoke 仅证明生产 Worker 的固定执行链能运行隔离样本。尚未用真实业务 Web 项目完成黑盒 UI 任务回放；唯一可选的历史项目没有可运行入口，不能作为黑盒正例。原 v4.0.81 任务的 `exit_code=124` 已确认是 Python profile 120 秒硬时限到期；为什么路由失败后容器继续运行到时限仍未查明。v4.0.82 只修复终态模式文案，没有声称修复该运行行为或业务路由。

发布后 `ops-check` 为 `degraded`、`can_continue=true`、无阻断项，磁盘使用率 93%（告警线 85%、临界线 95%）。未运行 cleanup/prune。发布备份、隔离恢复、Alembic、HTTPS 和服务健康情况详见证据 73。

## v4.0.83 候选发布验收（2026-10-08）

候选修复了 runner 的有界运行、超时分阶段回执、启动失败回执和路由候选词法提取。历史 `exit_code=124` 的直接原因是 120 秒 profile 硬时限；旧样本无 Web 入口，黑盒 404 是符合样本的负例。旧日志不足以证明 5 秒后继续运行至 120 秒的具体阶段，因此本候选只声明限制等待并提高后续诊断能力，不声称反推出历史卡点。独立复核新发现的普通字符串伪路由已先复现，再由源码词法扫描修复；注释、字符串、Python 多行字符串负例及 Node `===` 真实路径正例均已回归。证据见[证据 74](证据/74-v4083候选黑盒超时归因与最终回归-20261008.md)。

本地后端全量 **6577 passed、6 skipped、5 warnings**；runner 回归 **25 passed**，黑盒上下文 **9 passed**，执行器与报告 **137 passed**，部署 Python 回归 **137 passed、2 skipped**，部署脚本绑定与故障注入 **33 项通过**。Ruff、compileall、ShellCheck error 级、Shell 语法、Compose 配置、`git diff --check` 通过。skip 与 warnings 单独列示，不计为通过。精确 SHA 的远端 CI 尚待完成。

发布前只读核验确认主站健康/就绪、容器镜像、`APP_RELEASE` 与活动发布树均指向 v4.0.82 / `fad2f2ddd364486f5bddcd0e1059c343e67ef45a`；`/opt/code-review` 是历史 v4.0.72 遗留 checkout。生产备份隔离恢复验证通过（104 张表，Alembic 062）。`ops-check` 状态为 `degraded`、`can_continue=true`、无阻断项，唯一降级是磁盘 93%；可用空间 14,365,466,624 字节，略高于发布脚本 12 GiB 下限。未做 cleanup/prune。当前仍是发布前候选，不能记为生产已修复；精确 SHA、容量复查及主站/Worker 发布回执待补。

## 候选 v4.0.79（生产发布前）

修复前红测：在生产基线临时 worktree 运行新增回归，7 failed / 62 deselected；具体失败项见证据/43。候选同一批修复后定向回归为 142 passed，见证据/35。后续独立红队进一步发现动态调用、描述符、容器索引、`os.environb` 和 runner 子进程清理问题；最终红绿/全量结果见证据/49。复核时又发现静态字符串组合上限误伤复杂但合法的 URL 查询参数；现仅对导入、反射及动态方法解析的局部溢出执行 fail-closed，危险动态导入仍由上限负例覆盖，合法查询正例见 `test_blackbox_contract_keeps_encoded_high_complexity_query_with_trusted_port`。

候选从生产 v4.0.78 commit 957a6588fc59b924ed1ebae81446a361b37c28bf 的相同 Git tree（b85c818b56f764ba7182fb688db0b9308072bade）开始，当前 v4.0.79 修复范围：多文件源码正文原样进入 grounding 以保留逐字引文；递归压缩请求只带来源 ID 和当前正文，避免重复提交整个文件元数据；Python 黑盒动态端口来源检查覆盖间接调用、helper 参数/返回、反射与 `os.environb` 别名；普通及部署注入 runner 的离线依赖准备失败会阻止假通过；黑盒探活超时后按进程组回收应用及其子进程。

- 本地候选后端全量回归（`backend/.venv311/bin/python -m pytest -q --no-cov`）：6495 passed、6 skipped、5 warnings，见证据/57。6 项 Redis 用例按测试配置跳过，不计为通过；后续 CI 已在隔离 Redis Unix socket 上实际执行六项，见证据/60。
- 最终黑白盒/源码/runner 定向回归：244 passed、3 warnings，见证据/56；新增真实 SIGINT/SIGTERM runner 中断与 Node 子进程回收测试。为复核生产记录中的嵌套调用 AST 异常，本轮在候选上独立重复相关过滤测试三轮，每轮 89 passed、68 deselected、1 warning，覆盖 nested-call AST 不崩溃与动态端口正负边界，见证据/62。该单测未调用真实模型，也不等于生产任务复测。
- Redis 限流场景：block 持续、亚秒过期、admission window、失败结算窗口、旧检查窗口、Unix socket factory 均通过，证据/36。临时无网络容器、socket 和数据已删除。
- 前端：1850 tests passed / 142 files；ESLint 与 vue-tsc/Vite v4.0.79 构建通过，证据/38–40。
- 发布绑定：33 项通过；同一 test_scripts.sh 所含故障注入矩阵对备份、校验、构建、迁移、切换、健康、HTTPS失败及回滚拒绝进行了预期断言，证据/41。
- 持续集成：GitHub Actions run [`37576385449`](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/37576385449) 在 SHA `d3dc73eeddb802d2b8d9408cf24a9eee014c0247` 三个 job 全部成功。后端全量 6504 passed、5 warnings、0 failed/error/skipped，覆盖率 83%；六个真实 Redis Lua 限流场景和三个 socket 路径测试均执行。Ruff、compileall、pip-audit 通过，未发现已知依赖漏洞。前端 142 个测试文件、1850 项通过，npm audit 0 vulnerabilities，ESLint、类型检查及构建通过。部署回归 137 passed、2 skipped（opt-in Linux kernel cases），33 项绑定和故障注入脚本、ShellCheck、Compose 校验通过。第二轮 CI 暴露的容器/宿主机 Redis socket 路径比较错误已复现并修正；详见证据/59–60。
- Ruff（实现与新增测试）、compileall、ShellCheck error 级、runner/install 脚本 `bash -n`、`git diff --check` 均通过；默认 ShellCheck 模式仍报告已有 warning/info，见证据/49 与最终检查。

以上仅为候选自动化证据，不能表述为生产 Worker 验收。grounding 无效仍失败关闭；不可拆分小片或调用预算耗尽仍返回失败。它提高已观察故障路径的确定性，不证明任意外部项目、模型、语言、依赖或网络环境都成功。

## 生产基线与发布后验收

2026-10-07 只读请求 `GET https://lijiadong.cn/healthz` 与 `/readyz` 分别返回 `{"status":"ok","version":"4.0.78","release":"957a6588fc59b924ed1ebae81446a361b37c28bf"}`、`{"status":"ready","version":"4.0.78","release":"957a6588fc59b924ed1ebae81446a361b37c28bf"}`。普通 Safari 新加载的工作台页脚也显示 v4.0.78。一个此前打开的 `/readyz` 标签仍显示旧的 v4.0.75；真实点击刷新后更新为 v4.0.78，与接口一致，因此这次观察支持“旧标签页保留旧响应”，不支持当前新加载页面存在版本错配的结论。详见证据/54、证据/61。schema 基线为 `062_audit_log_action_length`。

本轮在普通 Safari 代码沙箱页只读查看了已有生产任务，没有创建、重试、取消或删除任务。下载并查看了既有失败任务的小型 `sandbox.log`，复核后将本机下载副本移入废纸篓。黑盒任务 `sbx_3950674251e94925afdbad92` 的生成步骤出现来源引文核验失败、动态用例引用未证实的 `HTTPError`、`Path`，以及 `'Name' object has no attribute 'func'` 异常；Worker 的原始 `sandbox.log` 记录未检测到支持的 Python 部署入口，因此这条状态码 0 记录不能证明可运行应用的黑盒正例会失败，也没有独立证明样本归档确实无入口。源码复核发现 v4.0.78 的嵌套 AST 调用路径能够产生相同 `Name.func` 异常，候选改用显式 AST 类型分派，相关回归三轮通过；但生产生成用例原文和完整模型栈追踪不可得，仍不能证明该路径就是线上异常的准确触发点，也不能证明它导致了 Worker 的入口检测/启动失败或已完成线上修复。另两条任务分别为白盒确定性检查通过但 AI 动态补充因压缩核验跳过，以及 `/health` 200 的确定性黑盒通过但 AI 动态用例三轮引用不存在符号后回退。它们都不能证明 AI 动态测试已完成。本轮仅查看既有历史记录，没有进行候选版本生产复测；逐项证据见证据/61、62。

v4.0.79 仍是候选，尚未在生产 Worker 执行本轮新样本。精确代码 SHA 的完整 Git bundle 已在项目内生成并通过新 bare 仓库取回验证，详情见证据/63；但尚未上传生产。`li@81.70.251.90` SSH 返回 `Permission denied (publickey,...)`，GitHub 仓库没有生产部署 workflow；本机 Docker daemon 关闭仅表示本机容器路径未运行，生产主机 Docker 状态尚不可查。当前没有可用通道在服务器执行发布脚本，不能声称候选已部署或这些生产问题已由候选修复。恢复授权部署通道后，先完成生产备份/隔离恢复和运行态门禁，再针对已观察的来源压缩/动态用例失败重跑原场景，并执行 Python 白盒、无入口样本启动失败负例、loopback-only runnable service 黑盒正例；每个关键场景至少独立重复三轮，核对动态用例清单、Worker 原始回执、报告回读、任务终态及资源清理。健康页不替代业务测试。

## 未覆盖范围

- 隔离 Redis 场景通过不覆盖生产 Redis 故障转移或网络分区。
- Node/PHP/Go/Java runner 测试不代表其源码语义 grounding 已达到 Python 覆盖。
- 模拟压缩结果不证明真实供应商端到端接收并理解超过 1,000,000 token。
- Safari 生产任务和资源清理闭环待部署后执行。

## 生产状态更正与 v4.0.80 候选（2026-10-07）

本文前述“生产仍为 v4.0.78、v4.0.79 未部署”为发布前快照，现已被以下事实更新：v4.0.79 / `d3dc73eeddb802d2b8d9408cf24a9eee014c0247` 已成功发布；部署备份 gzip/checksum、104 表隔离恢复、Alembic 062、前后端构建、API smoke、资产切换、HTTPS 和运行 SHA 一致均通过。发布后 `ops-check` 为 `degraded` 且 `can_continue=true`，唯一告警是磁盘 88%（告警线 85%、严重线 95%）；没有执行清理。

普通 Safari 的生产白盒任务 `sbx_65b29eecb2754ab5a856a023` 终态为 runner succeeded，但结构化报告 `verification_status=partial`：项目无自动发现测试文件，AI 动态用例三轮被 grounding 拒绝并 skipped。Safari 原 UI 仍显示绿色“已通过”。详见证据/64，结论仅覆盖确定性 runner 实际执行范围，未有黑盒路由回执。

基于这次真实回放，本地复现并修复标准库反射误拒绝、白名单绑定遮蔽绕过，以及 UI 未区分 runner 生命周期成功与整体 coverage partial。grounding 负例现覆盖导入/赋值/函数和 lambda 参数/循环与 with 目标/walrus/match/del 等遮蔽。v4.0.80 当前候选本地后端三文件回归 280 passed、3 warnings；后端全量 6511 passed、6 skipped、5 warnings；前端 142 文件、1851 passed；前端 lint、vue-tsc/Vite build、Ruff、compileall 通过。详见证据/65–66。精确 SHA 的 CI、v4.0.80 生产发布、同一白盒任务三轮复测、黑盒无入口负例与 loopback 应用正例均未完成，因此整体目标仍未验收完成。

测试任务 `sbx_65b29eecb2754ab5a856a023` 的 Worker 环境和作业目录已在回执中确认回收，但生产界面没有终态任务删除入口，默认保留 72 小时；不能声称持久化任务行已即时删除。仅在产品生命周期到期后能继续核验其清理状态，历史生产记录保持不动。

## v4.0.80 生产发布与复测边界（2026-10-07）

v4.0.80 / `396ca2312cefb229d6bbccc81315d41ce64a1ce7` 已发布。CI 的 Business、CodeQL 全绿；生产备份 gzip/checksum、104 表隔离恢复、Alembic 062、后端/前端健康、API、HTTPS、前端资源版本和运行账本一致均已核验。生产健康/就绪接口与前端 `buildInfo` 均报告 v4.0.80 和该 SHA。完整发布回执、bundle 校验值和降级边界见证据/68。

发布后 `ops-check` 为 `degraded`、`can_continue=true`，唯一降级为磁盘使用 89%（85% 告警、95% 阻断）；未执行清理。可选诱捕镜像构建因 Alpine 包安装停滞被终止，现有 v4.0.79 诱捕容器保持健康；对应源代码与 Compose 未变化，镜像版本标签未同步。

生产服务端验收不等于黑白盒业务验收。Safari 页面在真实点击刷新时被切换，之后为避免打断当前页面停止操作；因此没有 v4.0.80 Worker 任务回放，没有新建生产沙箱任务。白盒动态测试与 partial UI 修复、无入口黑盒负例、loopback 应用黑盒正例及各三轮复测仍待普通 Safari 恢复后完成。详细记录见证据/69。

## v4.0.81 最终候选复测（2026-10-08）

历史生产任务 `sbx_65b29eecb2754ab5a856a023`（任务版本 v4.0.79）发生三轮 AI 动态用例 grounding 拒绝并回退到确定性检查；原始模型脚本未保留。本地复现同类 AST 调用结果属性误判后修复了绑定、遮蔽、模块写入、动态命名空间、helper 参数与安全 builtin 同名伪装边界。正向样本保留合法 inline inspect/importlib loader 与只读 builtin 行为；具体红绿和静态分析范围见证据/70。

最终候选定向回归 `test_sandbox_agent_context.py` 为 **110 passed、2 warnings**；本地后端全量 **6556 passed、6 skipped、5 warnings（250.84 秒）**，skip 单独计数。前端 142 个测试文件、1851 passed，lint 与 typecheck/build 通过，Vite 转换 3742 个模块，产物版本为 v4.0.81、构建时间 `2026-10-07T16:12:32Z`。目标 Ruff、全后端 compileall、发布脚本 33 项绑定及故障注入、`git diff --check` 均通过。

提交 `b5c81173aed6784e8a3172cf9b212d550f02b1c0` 的 GitHub Actions 已通过：Business CI [37654707054](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/37654707054) 的后端全量 **6562 passed、5 warnings、0 skipped**（847.63 秒，含隔离 Redis 测试），部署/故障注入与前端三项 job 全绿；前端 **142 files、1851 passed**，依赖审计 0 vulnerabilities。CodeQL [37654707139](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/37654707139) 的 Python 与 JavaScript/TypeScript 扫描全绿。精确版本 bundle 校验值见证据/70。

生产 `/healthz`/`readyz` 仍报告 v4.0.80 / `396ca2312cefb229d6bbccc81315d41ce64a1ce7`；SSH 部署认证返回公钥拒绝，仓库无部署 workflow。尚未发布 v4.0.81、未新建 Worker 任务，因而没有生产修复后回放或真实 Safari 验收。磁盘 89% 告警和 decoy 版本偏差是最近一次只读复核中的独立待办，需部署通道恢复后重新核验。详见证据/68、70。
