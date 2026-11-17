# Prism 4.1.57

## 管理员小菱 Responses 兼容

- DeepSeek Responses reasoning 历史项在计量前移除官方明确不支持的 `summary` 与 `encrypted_content`，并将归一化后的同一份请求用于计量与发送。
- 保留 fail-closed 计量：其他未知输入字段仍在供应商调用前拒绝。
- 增加平台管理员真实计费作用域回归，覆盖历史 reasoning 清理、usage 结算，以及不触碰个人和企业积分账本。

## 验证

- 平台成本作用域、DeepSeek Responses 兼容及计量门定向回归：166 passed。
- 生产已部署 SHA `136359b0080ecb8fea2f468f3da3f1e09f31085f`；迁移 current/head 均为 `122_platform_model_cost_ledger`，容器、发布账本、备份与 178 张表的隔离恢复检查通过。
- 公网 health/ready、HTTPS 跳转和 `ops-check` 通过。生产普通 Safari 管理员重试只读任务“检查待审批事项”成功完成模型调用及 3 个子 Agent 团队汇总，没有执行审批动作。
- GitHub Actions 的前端测试/构建和部署/执行器/故障注入 job 已通过；后端全量业务回归在本记录更新时仍运行中。
- 实际生产管理员浏览器调用与发布状态记录在 `docs/管理员小菱平台计费作用域/ACCEPTANCE_管理员小菱平台计费作用域.md`；完整结果与后续范围见该目录的 `FINAL_管理员小菱平台计费作用域.md` 和 `TODO_管理员小菱平台计费作用域.md`。
