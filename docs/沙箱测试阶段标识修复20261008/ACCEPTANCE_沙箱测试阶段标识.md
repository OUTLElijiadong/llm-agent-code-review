# 沙箱测试阶段标识与提交竞态验收

## 已确认原因

- 历史黑盒任务 `sbx_3b317e05c0fa45128c20e31d` 的样本没有可用 Web 路由；八个探测地址返回 404，因此该黑盒业务断言失败。这个证据不能证明 Prism 公网服务故障。
- v4.0.83 将黑盒与组合启动事件统一记录为 `running_whitebox`，造成 UI 误标。v4.0.84 已修复事件阶段，并于 2026-10-07 23:42:46 UTC 部署主站；独立 Worker 也已切换到同一 release。
- v4.0.84 发布后第一轮无路由负例在 `2026-10-07T23:45:27Z` 被误判失败。追加审计事件顺序为 `validating → preparing → stopping → cleanup_retry → failed`，错误为“执行器重启时任务停留在不可恢复阶段 preparing”。systemd `NRestarts=0`，说明不是服务重启。
- 根因是 30 秒周期 janitor 与同进程 `submit_job()` 竞态：janitor 看到正在提交的 `preparing` 状态，将其当成上次进程遗留并清理。其余两轮无路由负例均按预期返回 404/exit 1，没有超时。

## v4.0.84 生产复测（部分通过，发现新漏洞）

- 黑盒有效路由：3/3 成功，`/healthz` 返回 200。
- 白盒：3/3 成功；组合：3/3 成功。
- 黑盒无路由：2/3 按预期快速失败（HTTP 404、exit 1、无 timeout）；1/3 被 janitor 竞态中止，不能计为场景通过。
- 12/12 Worker 任务均回执 `cleanup_confirmed=true`，对应容器与源码目录均不存在。
- 仅删除这 12 个 `codex_20261008_post84_*` Worker 状态 JSON；追加式审计日志保留，行数由 1136 增至 1196。未创建应用数据库任务。

## v4.0.85 候选验证

- 进行中任务保护覆盖 `validating`/`preparing` × 两种预约集合；模拟预约消失后的两种状态均失败关闭并确认清理；另用真实 `submit_job()` 线程分别阻塞在 validating/preparing 时并发调用 `_recover_jobs()`，任务继续运行且阶段序列正确。
- `backend/tests/test_prism_sandbox_executor.py`：86 passed。
- `frontend/src/utils/sandboxPresentation.test.ts`：6 passed。
- Ruff 与执行器 Python 字节码编译通过。
- [ ] 精确候选 SHA 的 GitHub Business CI、CodeQL 全部通过。
- [ ] v4.0.85 主站与独立 Worker 生产发布及账本一致性验证。
- [ ] 发布后黑盒有效/无路由、白盒和组合各 3 轮；四类每轮阶段、终态与清理均符合预期。
- [ ] Safari 普通窗口真实点击检查显示与历史兼容；不使用无痕模式。
- [ ] 清理最终生产 Worker 临时状态，确认容器/源码目录不存在并保留审计记录。

## 历史证据限制

历史 4.0.78 任务退出前没有进程快照。单 PID 清理缺口与约 120 秒后 exit 124 的现象一致；不能断言当时具体哪个子进程存活。4.0.83 前的原样本失败是路由断言不匹配，不能拿本轮临时样本冒充真实业务路由验收。
