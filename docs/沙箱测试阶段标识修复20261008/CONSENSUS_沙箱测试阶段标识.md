# 沙箱测试阶段标识共识

## 实现决策

- 保留 `status=running_whitebox` 这一既有活动状态，避免影响轮询、停止和清理协议。
- 后端事件 `stage` 使用 `running_whitebox`、`running_blackbox`、`running_combined` 区分模式。
- 前端显示新阶段；对旧版统一存成 `running_whitebox` 的事件，使用环境的 `test_mode` 做兼容显示。
- `_recover_jobs()` 必须在处理 active 状态前检查进程内 `SUBMISSIONS_INFLIGHT` 和 `PENDING_SUBMISSIONS`；同进程 janitor 不得与 `submit_job()` 争抢进行中的 validating/preparing 状态。进程重启后预约集合为空，旧的 validating/preparing 任务仍失败关闭并清理。
- 不改变历史数据库记录、不删改审计日志；本次生产 Worker 临时状态只在逐项确认清理后删除。

## 验收范围

候选代码：执行器阶段和 janitor 竞态回归单测、界面标签单测、相关文件 Lint、前端正式构建、完整 CI。

生产环境：核对精确提交、备份/迁移/健康/HTTPS/磁盘，使用固定沙箱样本对黑盒有效路由、无可识别路由、已识别路由返回 404、白盒和组合五组场景各重复三次；Safari 普通窗口检查新版和历史事件兼容，不使用无痕模式。额外的路由 404 负例作为扩展验证纳入验收。

## 停止条件

发布 SHA、活动账本、镜像、服务工作目录不一致；备份恢复或迁移门禁失败；运行健康失败；磁盘/内存空间不足；生产样本出现未预期超时、容器残留或清理回执缺失时停止发布或后续测试。
