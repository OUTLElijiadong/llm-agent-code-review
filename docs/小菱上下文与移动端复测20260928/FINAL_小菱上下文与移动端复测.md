# 小菱上下文与移动端复测：最终报告

## 交付结论

v4.0.28 已发布到生产，提交 SHA 为 `6d7b44bac5dc404ca41dd82f3df2f471df8227d0`。本次修复了 Responses Runtime 在长历史语义压缩输出多次达到 `max_output_tokens` 后直接失败的问题：压缩引文缩短并在服务端限长，完整来源批次在最终输出仍不完整时递归拆小、按序合并；不会静默裁切来源。修改包含本地回归测试和验收记录。

发布后真实供应商重复验证了 1,920,095 与 1,536,234 平台保守估算 tokens 的合成对话。两个样本在生产当前模型 `deepseek-flash` 上最终均 completed；原始 transcript 前缀保持不变，来源标记唯一，早/中/晚锚点进入最终实际模型请求并在答复中召回。估算值不是 DeepSeek tokenizer 计数，也不代表单一 HTTP 请求带有百万 token 输入。

部署门禁、备份独立恢复、运行镜像、Alembic、`ops-check`、HTTPS 和五个公网路由均已核验。Safari 管理员 `outle` 的 `390×844` 运行总览和小菱抽屉已真实刷新与点击，当前抽查没有 404。此证据只覆盖对应运行时压缩路径和单个管理员移动视口，不能推及其他 Agent/账号/页面。

## 实现和验证证据

- 代码：`backend/app/services/deepseek_responses_runtime.py`；回归：`backend/tests/unit/services/test_deepseek_responses_runtime.py`。
- 本地后端全量：`5371 passed, 5 skipped, 5 warnings`，496.73 秒。变更文件 Ruff、`compileall` 和 `git diff --check` 通过；Responses Runtime 定向测试 `53 passed`。
- 生产：Backend 与 Frontend 均使用 SHA `6d7b44bac5dc404ca41dd82f3df2f471df8227d0`，Alembic `058_roundtable_sessions`。发布备份 440,423,576 字节，SHA-256 `168c1a6ce2ce40ac39913af379cd90d0c04f3e73398c3b16b860586e6dea50a8`，103 张表隔离恢复成功。
- 生产真实模型压力运行在生产后端调用 Responses SSE，但合成会话和检查点只在本进程内存中；无工具执行，不创建生产聊天、任务、项目或测试账号数据。管理员既有真实验收聊天依用户此前授权保留。
- 生产 UI 抽查通过真实 Safari 刷新和点击；窗口视口 `390×844`、2 倍像素比。截图由本轮 CUA 工具结果呈现，没有另存进仓库。

详细时间、usage、失败复现和重复运行指标见 [验收记录](./ACCEPTANCE_小菱上下文与移动端复测.md)。

## 未完成范围

跨所有 AI/Agent 入口的真实百万级模型压力、admin/reviewer/user 全账号权限与会话隔离、全站逐页和控件布局、圆桌后台讨论与结束后五分钟追问、黑白盒工作流及监督 Agent 的高风险审批均没有在 v4.0.28 本轮逐一真实复测。请按 [待办清单](./TODO_小菱上下文与移动端复测.md) 继续闭环，不能把局部通过记成整个平台验收完成。
