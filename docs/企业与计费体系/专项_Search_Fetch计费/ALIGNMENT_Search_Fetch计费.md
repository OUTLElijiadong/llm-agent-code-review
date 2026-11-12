# Search 与 Fetch 计费对齐

## 项目与任务特性

- 项目使用 FastAPI、SQLAlchemy、Alembic 与 Vue；企业小菱有独立 Responses 运行时、工具执行器、运行检查点和企业钱包计量。
- 模型账本已经冻结输入/输出费率与供应商身份；`BillingReservation`、企业月额度及 C4 对账工作台共同承担预占、结算、未知冻结和人工复核。
- 当前服务端模型工具在付费 scope 中失败关闭；已有 Search/Fetch 计数只用于记录未定价供应商 usage，正数不能自动结算。

## 需求与边界

- 让公司群、小组群和企业成员私聊中的小菱能在受控范围内搜索网页和读取网页。
- 搜索/抓取必须从员工所属企业钱包扣费、计入企业/小组/成员月额度，并按供应商成功次数结算。
- 请求前必须留出足够积分；预算不足不发请求；无法判定供应商是否成功时保留冻结金额，禁止自动重试。
- 本专项不启用 Responses 内置 `web_search`，不解锁 Pro，不开放任意 MCP、平台通用 API、Agent Registry 团队或个人钱包路径。后续扩到这些入口须逐一接入同一合同。

## 已核实的供应商合同

- DeepSeek 官方 Search & Fetch 是两个独立 JSON API：`POST https://api.deepseek.com/v1/tools/search` 与 `/v1/tools/fetch`，共享 DeepSeek Bearer Key。
- 中文 Search 指南列每 1,000 次成功请求 ¥5；英文 Models & Pricing 列 Search 和 Fetch 各 US$0.75/1,000 次成功请求。按已复核的中行参考价 ¥6.7168/USD，美元口径折算为 ¥5.0376/1,000 次，高于中文价格；按较高口径向上取整，成本快照下限为 5,038 CNY 微元/成功请求。Search 只有 HTTP 200 且 `results` 非空计费；Fetch 只有 HTTP 200 且返回正文计费；空搜索、明确失败与超时不计费。
- Responses 内置 `web_search` 只支持 `deepseek-v4-pro`，且官方说明 `max_tool_calls` 会被忽略；不能用它构造可验证的单次成本上限。
- 来源：[DeepSeek 官方 Search & Fetch 指南](https://api-docs.deepseek.com/zh-cn/guides/search/)、[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)、[中国银行外汇牌价](https://www.boc.cn/sourcedb/whpj/)、[Responses API Guide](https://api-docs.deepseek.com/guides/responses_api/)。本次核验日期为 2026-11-07；价格和调用合同可能变化，创建费率版本时须重新核对并记录实际核验时间。

## 实施决策

- 由企业小菱把 Search/Fetch 注册为普通本地 function tools；每次模型工具调用只允许触发一个独立供应商 API 请求，不使用供应商内置 web-search。
- 每次实际 HTTP 请求都使用独立、幂等 reservation：先按已审批费率冻结企业积分和适用月额度，再执行最后一次授权/租约检查，然后才发送 HTTPS 请求。
- 当前积分商品为 1,000 积分 ¥4。供应商成本按两个官方价格口径中较高者取 ¥0.005038/成功请求；客户收费按 30% 直接毛利底线向上取整为 **1,800 毫积分/成功请求**，折合 ¥0.0072；相对 5,038 微元成本的精确毛利为 **30.0278%（向下取整 3,002 基点）**。Search 与 Fetch 单价相同。它不覆盖税费、支付手续费、服务器和其他经营成本，不能解释为整体盈利保证。
- 同一个小菱 run 最多发送 **3 次** Search/Fetch 外部请求（合计），限制的是实际尝试次数而非成功次数；每次请求另受企业/小组/成员钱包月度额度和可用积分限制。
- 成功请求按实际 success 计数结算；确定不计费的空结果/失败响应释放预占；无 HTTP 响应、HTTP 200 但响应格式无法确认等情形记为 `unknown` 并冻结。所有路径禁用自动 HTTP 重试，显式用户重试会使用新工具 call ID；旧 `unknown` reservation 不释放。
- 供应商价、客户价、有效时间、CNY 与 USD 两个官方来源/核验时间和摘要哈希并入现有双人审批的不可变费率目录。历史目录无 Search/Fetch 费率时视为未开通；只有来源新鲜、成本底线与毛利通过且费率目录已批准生效后才开放。
- 中文人民币指南与英文美元价目必须分别登记并通过固定官方 URL 校验。两者和汇率来源都要覆盖计划生效时间；运行时再次检查来源年龄、官方地址、保守成本下限与 30% 毛利，防止排期过远、来源过期或目录异常时继续外呼。两个 API 可按各自的成本与售价单独启停。
- 复用统一 `BillingReservation`、批次分摊、额度台账和 C4 双人对账，不另建第二套钱包。增加 `billable_kind` 区分模型 Token 与按次工具；Token 图只汇总模型记录，工具请求数/费用单独展示。
- 单 run 最多 3 次尝试、Search 返回最多 5 个摘要结果、Fetch 与工具输出均有字节上限；只把截断后的来源和正文放进当前企业小菱检查点。查询串、响应体和密钥不额外写账单或审计表。

## 待验证事项

- 一次性 MySQL 8.0.45 从现 head 098 升级到新 head；检查旧 reservation/费率目录快照不变、无价旧目录仍 fail closed。
- 假供应商验证 Search 成功、Fetch 成功、空搜索、明确错误、连接中断、畸形 JSON、重复工具 call ID、预算/额度不足、角色撤权与 lease fencing。
- 验证小菱检查点恢复不会重复发出已开始的请求，C4 只允许按原 reservation 快照人工结算或双人确认未发送后释放。
- 验收只代表本地实现和模拟响应；真实 DeepSeek 供应商账单、真实企业账号浏览器与生产部署仍另行验证。
