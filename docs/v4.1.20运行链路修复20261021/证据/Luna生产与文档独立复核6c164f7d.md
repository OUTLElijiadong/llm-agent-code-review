# Luna 生产与文档独立复核（6c164f7d）

复核日期：2026-10-22（UTC+8）
复核对象：生产候选 `6c164f7d2a596f4839bad7563ac68eb70655725a`
结论：**发布身份与所列生产证据、当前最终文档相符；没有发现新的发布阻断。存在一项需保留的运维降级和明确的验收边界，不能写成全链路无条件健康。**

## 独立复核范围

只读检查 `证据/生产发布6c164f7d/` 下主线程限定的 `remote-checks.json`、`summary.json`、`cross-checks.json`、`healthz.json`、`readyz.json`、`db-source-business-channel.json`、`natural-scheduler-observation.json`、`精确提交CI结果.json`；核对 `FINAL_运行链路修复.md`、`TODO_运行链路修复.md`、`ACCEPTANCE_运行链路修复.md` 当前顶部，以及 `真实点击验收_20261022.md`。为区分自然调度动作状态和业务采集状态，仅只读查看精确提交 `6c164f7d2a596f4839bad7563ac68eb70655725a` 的执行器回执归档、调度器、安全监控与安全中心状态投影，并检查安全中心页面状态映射。未改代码或已有文档，未运行应用测试、生产动作、monitor、浏览器或LLM调用；未读取额外临时JSON。

## 生产证据核对

- **精确版本一致。** `remote-checks.json` 的 `expected_sha` 与远端 `git` 输出均为完整 SHA `6c164f7d2a596f4839bad7563ac68eb70655725a`；前后端镜像标签、`APP_RELEASE`、`BACKEND_RELEASE`、`FRONTEND_RELEASE`、账本、公开 `/healthz` 与 `/readyz`、summary及两个HTTP证据均指向同一SHA和版本 `4.1.20`。healthz与readyz均为HTTP 200，状态分别为 `ok`、`ready`。
- **对账与发布恢复。** `cross-checks.json` 明确记录37项通过、0项失败，覆盖精确CI、完整发布、备份恢复、Git/账本、容器及环境、迁移、健康端点、维护锁、执行器哈希及正式socket探针等。远端账本切换时间为 `2026-10-22T00:12:00Z`（08:12 UTC+8），pending不存在、维护锁不存在、隔离验证库已清理；MySQL当前迁移与heads均为 `064_remote_pentest_chain`。备份记录大小 `1001291208` 字节，SHA-256 `f70bd172ae169daa3e04c6a7cec8d376882e4c5eb3898f7fef20a549a07fb452`，sidecar hash相同，meta标注109张表；summary的hash匹配为true，cross-check备份恢复为true。限定说明：所列证据用恢复检查布尔结果及meta中的109表声明记录恢复，不含逐表内容清单；不把它扩大表述成此次独立复跑了逐表查询。
- **正式服务身份探针只证明安全降级。** `db-source-business-channel.json` 有2条正式socket回执，均为UID `10001` / GID `991`、HTTP 200、外层成功；两次耗时分别327ms和111ms。内层均为 `ok=false`、`degraded=true`、`reason_code=general_log_disabled`、`history_not_scanned=true`、`statistics_absent=true`。因此证明日志源关闭时快速、明确地不扫描历史且不补零；不证明非空威胁采集已验证成功。
- **自然调度需按状态层解释。** `natural-scheduler-observation.json` 只有1条发布时间后的 `source=scheduler`、`action=db_threat_signals` 动作：其审计 `status=success`、`executor_ok=true`，但动作内层 `ok=false`、`degraded=true`、`general_log_disabled`、`history_not_scanned=true`。另有1轮时间关联monitor记录，时间为00:14:34Z，`status=failed`、`success=false`，错误/降级动作均为 `db_threat_signals`；轮次 `source=null`，文件明确说明轮次未持久化触发来源，所以只能称时间关联，不能证明该轮由scheduler触发。
- **动作success不是采集success。** 精确提交的 `ops_service._finalize_execution` 以执行器外层回执的 `result.get("ok")` 设置 `OpsExecution.status`；所给生产证据同时记录 `outer_ok=true` 与嵌套业务 `result.ok=false`。故该条 `OpsExecution.status=success` 表示执行器请求/动作回执外层完成，不代表数据库日志被采集或威胁统计可用。业务安全中心并不从 `OpsExecution.status` 推导采集状态：安全监控轮次把带 `degraded=true` 的来源写入错误/降级明细并令轮次 `success=false`；安全中心投影据该轮次计算来源 `degraded` 和 `last_run` 异常。精确提交前端的状态映射也将 `failed_sources`、`degraded_sources`、`degraded` 或非成功轮次显示为异常/降级，而不是“采集成功”。因此本次证据未显示安全中心业务状态把这条动作外层success误报成采集成功，**不构成该项发布阻断**。但是本轮没有登录态真实点击或页面截图；无法据此宣称生产UI实际渲染已验收。调度审计状态与业务结果是不同层次，不应概括成“所有status一致”。
- **运行资源处于降级。** `ops_check` 的退出码虽为0，其业务状态明确是 `degraded`、`can_continue=true`，没有blocking checks；根分区使用86%，超过85%告警线，低于95% critical线，要求人工审阅清理。Swap已用 `3222249472/3222265856` 字节，仅余16384字节，接近满。MySQL健康但restart count为1。发布期间及发布后所记录内核OOM事件窗口均为空，summary `oom_events` 为空；这只代表相应有限窗口没有新事件，不等于历史无OOM或MySQL重启原因已排除。最终文档TODO保留了此前03:30:59（UTC+8）的MySQL cgroup OOM及继续观察项。该资源降级是发布后的实际运维事项，不是本次状态传播/版本对账失败；不能用ops-check退出码0改写成整体 `ok`。

## CI与文档一致性

- 精确提交CI记录的业务run `39695533672` 和CodeQL run `39695533666` 的head SHA均为完整候选SHA，run及列出的jobs均为completed/success。业务统计为：后端8314 passed、7 warnings；前端159个测试文件、2113 tests passed；部署执行器176 passed、2 skipped；发布绑定Shell 33 passed、0 failed。前端文件数和测试项数是同一组统计的不同口径；部署Python定向成绩包含于部署总组，不能重复累加。文档明确写“定向与全量成绩不累加”，CI JSON也明确CodeQL job成功不等于告警清单为零。另有后端依赖审计原文 `No known vulnerabilities found`，它不应与CodeQL结论混为一谈。
- `FINAL` 当前顶部的版本、完整SHA、08:12 UTC+8账本时间、发布/恢复完成和16项未验收，与生产证据一致。正文准确区分327/111ms降级探针、1条scheduler来源动作与1轮仅时间关联的failed/false记录；其“状态与业务结果无矛盾”应理解为monitor轮次status和业务结果一致，不包括动作审计status与内层采集结果相同。为避免读者混淆，未来维护该段时建议保留本报告所述外层执行器状态与内层采集状态的区别；本次按要求未改原文。
- `TODO` 当前顶部承认发布完成，继续列明16项登录态点击未验收、非空威胁采集未验证、磁盘与swap压力及有限OOM观察窗口；没有把发布门禁等同业务验收。
- `ACCEPTANCE` 当前顶部对齐SHA、CI、发布/恢复、37项对账、探针和自然回执状态分层，并说明轮次没有持久化source。`真实点击验收_20261022.md` 明确16项均未形成完整真实点击通过证据；healthz通过、匿名旁证及CI没有被计作点击验收。记录还明确浏览器停在空账号/密码登录表单，且未保存独立截图文件。

## 阻断与边界结论

**本次限定的生产证据与文档复核未发现新的发布阻断，精确版本/CI/备份恢复对账符合证据。**需保留的事项是：运维检查真实处于 `degraded`（磁盘86%、swap近满），需按TODO人工审阅并持续观察；日志源关闭，未验证非空威胁采集；OOM结论限于给定观测窗口；16项真实点击仍未验收。它们都是明确的运行或验收边界，不能在后续摘要中写成全链路已验证。CodeQL仅能确认任务成功，不能据此声明零告警。安全中心状态投影代码已核对，但生产登录态UI未实际点击，因此不作视觉/浏览器通过声明。
