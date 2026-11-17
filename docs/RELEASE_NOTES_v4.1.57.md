# Prism 4.1.57

## 管理员小菱 Responses 兼容

- DeepSeek Responses reasoning 历史项在计量前移除官方明确不支持的 `summary` 与 `encrypted_content`，并将归一化后的同一份请求用于计量与发送。
- 保留 fail-closed 计量：其他未知输入字段仍在供应商调用前拒绝。
- 增加平台管理员真实计费作用域回归，覆盖历史 reasoning 清理、usage 结算，以及不触碰个人和企业积分账本。

## 验证

- 平台成本作用域、DeepSeek Responses 兼容及计量门定向回归：166 passed。
- 实际生产管理员浏览器调用与发布状态记录在 `docs/管理员小菱平台计费作用域/ACCEPTANCE_管理员小菱平台计费作用域.md`。
