# Luna 消费者复核

观察日期：2026-10-22  
复核范围：`backend/app/services/security_monitor_service.py` 与对应单元测试。仅验证消费者对数据库日志源不可用回执的处理；未运行生产安全监控、未执行生产操作、未占用浏览器。

## 发现

### P1：旧执行器的 `ok=false` 可能被整轮巡检当作成功

原消费者只在回执明确包含 `degraded=true` 时登记降级和错误。旧执行器只返回 `ok=false` 时，该动作仍计为已完成且不产生错误，可能令整轮 `success=true`，把数据库日志源盲区误报为正常。

修复：`_call_action` 对 `db_threat_signals` 的 `ok=false` 回执复制并强制标准化 `degraded=true`，保留原字段。既有巡检降级流程因此将该动作加入 `degraded_actions`、记录可重试错误，并令整轮 `success=false`；不为源不可用创建数据库威胁告警。

### P2：不可用状态的未知计数被伪报为零且来源信息丢失

原状态聚合把缺失计数用 `or 0` 转成零，并只返回 `reason`，没有透传 `reason_code`、`history_not_scanned` 和来源状态，无法区分“没有发现”与“没有扫描”。

修复：`query_security_status` 对不可用数据库信号将 `destructive_total`、`dump_exfil_total`、`error_total` 返回为 `null`，设置 `degraded=true`，保留 `reason`、`reason_code`、`history_not_scanned` 及所有 `source_*` 元数据。若动作调用本身抛错，也返回降级状态与错误原因。成功路径的整数计数保持原行为，样本/SQL 不加入状态响应。

## 验证

- 红灯：新增的 2 个消费者回归先运行，结果 `2 failed, 35 deselected`；分别复现巡检仍为 `success=true`、状态缺少 `degraded`。
- 绿灯：完整运行 `backend/tests/unit/services/test_security_monitor_service.py`，结果 `37 passed`。
- Ruff：指定虚拟环境执行目标服务和测试文件检查，`All checks passed!`。
- Python 编译检查：服务与测试文件 `py_compile` 通过。
- 补充：`git diff --check` 通过。

## 边界与剩余风险

- `SecurityStatusOut.db_threat` 是 `dict`，无需改 schema；本次检索 `frontend/src` 未发现 `db_threat` 或 `destructive_total` 直读，无需前端变更。
- 用户提供的 Executor 验证为 `175` 项通过、`2` 项跳过；该数字不属于本次消费者测试结果，未在此复跑。
- 未进行生产监控、生产发布或浏览器验收；本记录不代表这些验收通过。
- 不可用时按既有响应形状保留用户列表字段为空数组；调用方应以 `ok=false` / `degraded=true` 判断数据不可用，不得把空列表解释为已扫描且无记录。
