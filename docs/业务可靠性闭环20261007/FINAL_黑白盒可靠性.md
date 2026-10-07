# 黑白盒可靠性阶段交付

## v4.0.83 候选修复状态（2026-10-08）

已在候选代码中增加黑白盒 runner 的总时限与单阶段/动态断言时限，区分应用启动失败、依赖准备超时、断言超时和总预算耗尽，并在终止时清理应用进程。路由词法扫描忽略注释和字符串，覆盖了独立复核发现的 `ROUTE_DOC = "route('/healthz')"` 假路由。历史故障的 120 秒 `exit_code=124` 有证据支持；触发具体等待的旧阶段仍不可追溯。

候选本地后端全量 **6577 passed、6 skipped、5 warnings**；关键 runner **25 passed**、黑盒上下文 **9 passed**、执行器/报告 **137 passed**、deploy Python **137 passed、2 skipped**；部署脚本 33 项绑定及故障注入通过。静态检查与 Compose 配置通过。精确提交、CI 和生产发布后，需补录本节状态；候选未发布期间生产仍为 v4.0.82。生产只读门禁显示磁盘 93%、无 blocking checks，仍须在实际切换前重查容量。

## v4.0.82 部署更新（2026-10-08）

v4.0.82 已部署到生产主站和独立 Sandbox Worker。独立 Worker 漂移已补齐：执行器、profile 与 Python/Node/Java/Go/PHP 五种 runner 镜像均使用本次发布的 runner 源码 SHA，profile digest 与本地镜像 ID 完全匹配，`runsc` 和 browser blackbox health ready。生产 Worker 五语言白盒隔离 smoke 通过，Python loopback 黑盒 smoke 三轮通过。部署、备份、CI、Safari 版本和完整运行态细节见[证据 73](证据/73-v4082生产部署与Worker复核-20261008.md)。

已定位历史黑盒 404 的范围：所用唯一可选项目没有可运行 Web 入口或路由，因而其黑盒负例符合样本事实；不能据此推断 Prism 生产路由故障。`exit_code=124` 的直接原因是 Python runner 触发 profile 中 120 秒硬时限；为什么路由失败后进程仍持续运行到时限尚未证明，不宣称已修复。真实业务黑盒验收仍待获授权且可运行的 Web 项目样本。磁盘 93% 仍在告警区间，未做清理。

候选 v4.0.79 修复了多文件源码 grounding 引文、压缩拆分重复携带元数据、Python 黑盒端口来源检查遗漏间接动态调用与 `os.environb` 写入、部署注入黑盒跳过依赖准备，以及黑盒启动超时未回收 Node 子进程等已复现问题。额外收紧了静态字符串复杂度溢出的作用范围，保持危险反射/导入 fail-closed，同时允许复杂合法 URL 参数；新增 SIGINT/SIGTERM runner 进程回收回归。最终黑白盒/源码/runner 定向回归在 SHA `d3dc73eeddb802d2b8d9408cf24a9eee014c0247` 重跑为 244 passed。GitHub Actions run `37576385449` 三 job 全绿：后端 6504 passed、5 warnings、0 failed/error/skipped，含实际执行并通过的六项 Redis Lua 行为测试；覆盖率 83%，Ruff、compileall、pip-audit 通过。前端 1850 passed、142 个文件，依赖审计、ESLint、类型检查和构建通过。部署执行器 137 passed、2 skipped（可选 Linux 内核测试），33 项发布绑定、部署故障注入、ShellCheck 与 Compose 检查通过。此前两轮 CI 分别揭示解释器/fixture 冲突和 Redis 容器路径比较缺陷，均已修复，详见证据/59–60。

2026-10-07 v4.0.79 已实际部署到生产，运行 SHA `d3dc73eeddb802d2b8d9408cf24a9eee014c0247`、Alembic 062；备份校验、隔离恢复 104 表、API smoke、HTTPS/健康页、资产切换和运行镜像一致均通过。部署后 `ops-check` 为 degraded/can_continue，唯一告警为磁盘 88%（警告线 85%、严重线 95%）；该项需人工 dry-run 审阅，未执行清理。普通 Safari 发布后白盒任务 `sbx_65b29eecb2754ab5a856a023` 终态 succeeded，但报告结构化 coverage=partial：确定性执行通过，AI 动态用例因标准库反射符号误拒绝而 skipped，且项目未发现测试文件。Safari 状态标签错误显示绿色“已通过”。本地已先复现并修复 grounding 误拒绝与前端覆盖标签，版本升至 v4.0.80；候选门禁、生产 Worker 复测与黑盒正负样本闭环仍待完成。详见证据/64–66。

v4.0.80 本地全量验证：后端 6511 passed、6 skipped、5 warnings（242.39 秒）；grounding/沙箱报告三文件定向回归 280 passed、3 warnings；前端 142 个测试文件、1851 passed；前端 lint、`vue-tsc`/Vite 构建、后端 Ruff/compileall 通过。后端全量用例有 6 项 skip，因此候选精确 SHA 的 CI 结果仍是必要门禁。终态任务删除受产品能力限制，任务记录默认 72h 到期；目前只确认 Worker 容器和作业目录已清理，未确认任务记录已经删除。

## v4.0.80 交付状态

v4.0.80 / `396ca2312cefb229d6bbccc81315d41ce64a1ce7` 已于 2026-10-07 发布至生产。精确 SHA 的 Business CI、CodeQL 均成功；候选本地后端 6,511 passed、6 skipped，前端 1,851 passed。生产新备份与 104 表隔离恢复、Alembic 062、后端/前端健康、API smoke、HTTPS、buildInfo 版本与运行 SHA 均通过。具体数据见证据/68。

**整体黑白盒业务验收仍未完成。** Safari 在尝试刷新时被用户切换到另一页面，随后停止浏览器操作；没有在 v4.0.80 上重跑 Worker 白盒或黑盒任务，也没有完成用户界面读回。因此修复是否已在生产真实任务中闭环，尚无实测证据。当前仍待普通 Safari 继续实测标准库反射白盒与 partial 显示、无入口黑盒负例、loopback 可运行 Web 服务黑盒正例；关键场景计划各 3 轮。现有生产测试记录保留 72 小时，无终态删除入口，本轮未创建任务或即时删除历史记录。诱捕层旧 v4.0.79 镜像仍健康，因可选构建停滞未更新；磁盘巡检为 89% 并处于告警态。详见证据/68–69。

## v4.0.81 最终候选与闭环边界（2026-10-08）

本地修复标准库调用结果的 AST grounding 误拒绝，并收紧静态可见的同名模块、导入/赋值别名、属性写入/删除、动态命名空间、helper 直接参数/属性/星号解包/字面量容器和安全 builtin 同名伪装。精确白名单保持窄范围，合法 `inspect`/`importlib` 反射、参数结果与只读 builtin 仍有正向用例。对任意 Python 动态代码或多层变量容器跨过程数据流不作全语义安全保证。详见证据/70。

候选本地验收：沙箱上下文定向 **110 passed、2 warnings**；后端全量 **6556 passed、6 skipped、5 warnings（250.84 秒）**；前端 **142 个测试文件、1851 passed**，lint、vue-tsc/Vite 构建通过（3742 个模块；构建注入 v4.0.81 和时间 `2026-10-07T16:12:32Z`）；目标 Ruff、全后端 compileall、33 项发布绑定/故障注入、`git diff --check` 通过。后端全量命令使用 `--disable-warnings`，仅记录准确汇总数，不猜测 warning 来源。skip 和 warnings 单独保留，不冒充通过或零告警。

候选代码提交 `b5c81173aed6784e8a3172cf9b212d550f02b1c0` 的 Business CI [37654707054](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/37654707054) 与 CodeQL [37654707139](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/37654707139) 均成功。Business CI 后端全量 6562 passed、5 warnings、0 skipped（847.63 秒；隔离 Redis 用例实际执行），前端 142 files / 1851 passed，依赖审计无已知漏洞；部署和故障注入 job 成功。CodeQL Python 与 JavaScript/TypeScript 均成功。

2026-10-08 更新：v4.0.81 已发布，生产发布基线、健康/就绪、运行镜像、备份与独立 ops-check 复核通过，详细见证据/71。部署时的 SSH 拒绝原因是首次使用了不匹配的 `li` 登录名；用已配置密钥的 `root` 身份后完成发布。普通 Safari 页面刷新后显示 v4.0.81；真实白盒任务 `sbx_c3c03892e4684e5691a5e950` 终态已通过，动态用例 3/3、容器/作业目录回收确认。但报告指出项目没有自带测试文件或可启动 Web 入口，AI 用例的业务覆盖深度有限；这不能外推为完整白盒或黑盒通过。

**整体黑白盒业务验收仍未完成。** v4.0.81 目前仅有一个生产白盒样本通过，未达到多独立样本/三轮重复要求。唯一可选项目没有 `main/app/index/server`，因此黑盒正例及无入口负例的新版本复测仍待补足；历史 v4.0.79 失败记录不能代表新版本。Safari 时间线显示 Agent 报告“角色 5/4”，下载报告没有角色清单，记为 UI/计数待核验，不判定为已确认调度漏洞。运维 `ops-check` 仍为 degraded，唯一告警是磁盘使用率 91%（告警线 85%、临界线 95%）；未执行清理。真实账号 RBAC、论坛和全业务角色闭环本次未覆盖，测试记录默认保留 72 小时。详见证据/71。
