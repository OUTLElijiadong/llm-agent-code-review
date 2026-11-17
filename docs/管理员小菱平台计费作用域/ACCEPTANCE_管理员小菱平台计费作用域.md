# 管理员小菱平台计费作用域验收

## 验收状态

- [x] 生产基线：4.1.55 / `34f9fb71f9aa0c0d56f4fdbc805471b98959b3`。
- [x] 生产唯一数据库迁移 head：`121_billing_model_evidence`。
- [x] 根因：管理员请求未进入可信付款作用域；商业计量入口没有平台付款人；默认 Pro 不在个人可售模型名单。
- [x] 独立平台成本表、迁移与预占/结算/未知/未发送状态机。
- [x] 管理员 Responses、后台恢复和 Agent 团队按运行共享成本作用域。
- [x] 管理员不扣个人/企业积分；BYOK 与普通个人路径保持原付费方。
- [x] RBAC 在预占和发送前复核；BYOK 发送前同样复核管理员权限；未知调用不可自动重放；同一运行允许合法并发模型请求。
- [x] DeepSeek 官方人民币费率来源、核对日期和费率版本写入每条成本记录。
- [x] 本地后端定向回归：393 passed、4 skipped；覆盖实际管理员 SSE 入口、平台账本、人工重试冻结、未知结果并发发送拦截、BYOK 网络错误提示、管理员团队、后台恢复及 122 迁移。
- [x] 计费证据兼容门测试：70 passed；覆盖当前生产旧 reader、本次审核过的 gate 指纹，以及 122 当前版本带激活标记时对完整 121 结构的兼容判断；未审核指纹拒绝。
- [x] 后端修改文件 Ruff、`compileall`、`git diff --check` 通过；发布脚本回归通过，隔离 MySQL 全新迁移链 4 passed。
- [x] 生产 MySQL 迁移与 4.1.56 首次发布；生产数据库 head 为 `122_platform_model_cost_ledger`，服务、HTTPS、health/ready 和运维检查通过。
- [ ] 生产管理员 Safari 模型调用成功；4.1.56 的真实重试已进入计量校验，但被历史 reasoning 项协议兼容拦截，见下方生产证据，4.1.57 修复后须重新验收。

## 范围边界

自动化结果仅证明本地代码/隔离数据库行为。只有上线 SHA、生产迁移状态、运行镜像、HTTPS 健康和真实管理员账户在普通 Safari 的模型调用全部核对后，才能标记生产验收完成。

## 本地验收证据

- 定向后端：393 passed、4 skipped；另有计费证据兼容门 70 passed。MySQL 相关 4 个环境依赖测试在这次无外部数据库的批次中跳过；隔离 MySQL 8.0.45 全新迁移链另行 4 passed，覆盖升级到 122 和负用量/成本约束。
- Ruff：后端修改文件全通过；发布兼容脚本用 `--ignore E501,E731,EXE001` 通过，忽略项是既有行长/表达式规则与脚本非可执行权限检查。`compileall` 和 `git diff --check` 通过。
- SQLite：升级、回退、再升级与 DB 负值约束通过；发布 shell 回归覆盖失败注入、回滚及账单结构兼容。
- 未知结果：同一运行不能自动续发；当前没有管理员对账 UI/API，需运维按供应商账单核对平台成本流水。人工重试会先冻结中断预占，运行无未知项时才可继续。
- 价格来源：[DeepSeek 官方模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)；峰时缓存未命中费率为 Flash ¥2/¥8、V4 Pro ¥9/¥27 每百万 Token。

## 生产浏览器证据

- 4.1.56 (`849b42b3adb295af3a19ee47a77bc4373f412f49`) 已发布；发布后 `healthz` 与 `readyz` 返回 `status=ok/ready`，release 为该 SHA，容器 healthy，`ops-check` 为 `status=ok`，Alembic current/head 均为 `122_platform_model_cost_ledger`。
- 平台管理员 Safari 打开 `/admin/billing-reservations` 的“小菱 · 管理助手”，重试历史任务“检查待审批事项”。原始错误“平台模型调用未进入可信计费作用域”消失；随后出现“Responses 推理项字段未知，禁止付费调用”。
- 根因是计量门收到历史 reasoning 项后拒绝未知字段。校验发生在成本预占和供应商发送之前，因此该次请求没有产生平台模型成本或供应商调用；同一 payload 适配修复已加入 4.1.57。
- 4.1.57 定向回归：166 passed，覆盖管理员成本结算、DeepSeek reasoning 字段规范化、未知字段仍 fail closed、Responses 兼容及计量门。生产部署与 Safari 再次调用尚待完成。
