# Search 与 Fetch 计费设计

## 总体架构

```mermaid
flowchart LR
  Human[企业成员消息] --> Assistant[企业小菱 Responses]
  Assistant --> Tool[Search/Fetch 本地工具执行器]
  Tool --> Auth[当前成员/会话/小组/租约复核]
  Auth --> Catalog[生效的双人审批费率快照]
  Catalog --> Ledger[企业钱包预占与月额度锁]
  Ledger --> Gate[最终发送授权检查]
  Gate --> Vendor[DeepSeek 官方 Search/Fetch API]
  Vendor --> Outcome{响应可确定?}
  Outcome -->|成功且有结果| Settle[按成功次数结算]
  Outcome -->|明确失败或空结果| Release[按供应商合同释放]
  Outcome -->|无响应/无法判定| Unknown[冻结并进入 C4 对账]
  Settle --> Durable[结果与账务同事务持久化]
  Release --> Durable
  Durable --> Checkpoint[回放已保存结果，不重发]
  Unknown --> Stop[熔断当前工具轮次并等待 C4]
  Crash[标记外呼后进程崩溃] --> Recover[租约过期且超过 30 分钟后转 unknown]
  Recover --> Stop
```

## 分层职责

- **工具执行器**：参数校验、单 run 3 次请求硬限额、工具 schema 隐藏/失败关闭、输出截断及工具结果标记。
- **成本目录**：费率目录包含 Search/Fetch 的供应商单位成本、客户扣费，以及分别固定为中文人民币指南和英文美元价目页的来源 URL/核验时间；币价来源、汇率来源、费用、生效时间全部进入版本哈希，双人批准后不可变。计划生效时间和每次调用时都重新校验来源年龄；调用时还重算官方成本下限和 30% 毛利。
- **账本**：`BillingReservation.billable_kind` 将模型调用与 Search/Fetch 区分；工具 reservation 使用企业钱包、批次预占明细、企业额度行锁、幂等键、单位成本/客户费率/目录版本快照和 unknown/C4 状态。工具 attempt-started 在任何网络操作前持久化提交；已有 key 永不触发第二次发送。
- **供应商适配器**：固定 HTTPS 主机与固定两个 endpoint，使用平台 DeepSeek key，`trust_env=False`；禁止自由配置 host、自动重试与把用户 URL 交给本机 HTTP client。
- **用量查询与管理 UI**：Token 图只合计 `billable_kind=model`；个人/企业用量显示 Search/Fetch 成功次数、用户扣费、未知/待处理冻结和明确不计费次数。费率目录显示单笔毛利；C4 在单条 reservation 展示供应商成本和客户费率快照。不宣称用量页已聚合供应商成本或整体 margin。

## 计费流程与状态

```mermaid
sequenceDiagram
  participant X as 企业小菱
  participant E as 工具执行器
  participant L as 钱包/额度账本
  participant D as DeepSeek API
  X->>E: function_call(call_id, args)
  E->>E: 校验 call_id 与 run/attempt 限额
  E->>L: 校验当前权限并冻结 1 次请求费用
  L-->>E: reservation 已提交
  E->>E: 再核对角色/会话/租约与停止标志
  alt 授权已失效
    E->>L: 仅当 attempt-started 为空时按未发送证据释放
  else 授权仍有效
    E->>L: 持久化 attempt-started 并提交
    E->>D: 一次 HTTPS POST，无自动重试
    alt HTTP 200 且 Search 有结果/Fetch 有正文
      D-->>E: 可验证成功响应
      E->>L: 按 reservation 工具快照结算 1 次
    else HTTP 200 空结果或明确失败响应
      D-->>E: 可验证未计费响应
      E->>L: 记录 not_billable 并释放预占
    else 无响应或响应格式无法确定
      D-->>E: 结果未知
      E->>L: 保留 unknown 冻结
    end
  end
  E-->>X: 有界结果/无结果/待对账错误
```

## 数据与不变量

- `BillingRateCatalog` 新列保存 Search/Fetch 每成功请求供应商成本（CNY 微元）、客户费率（毫积分）及两个独立币种来源事实；新目录 version/source hash 纳入工具来源和四项费率。历史目录默认费率为 0，执行端拒绝，须经创建、双人审批和未来生效才能启用。
- `BillingReservation.billable_kind` 区分 `model`、`search` 与 `fetch`；工具记录还含规范化工具名及供应商成本/客户费率/目录版本快照。Search/Fetch reservation 使用独立 `request_key`，唯一键阻断重复供应商调用。DDL 的工具约束必须对费率字段显式 `IS NOT NULL`，避免 SQL CHECK 的 UNKNOWN 通过。
- 只允许企业钱包；预占同时保留适用企业、小组、成员月额度。结算时释放预占并增加实际企业额度用量；不允许个人回退。
- model reservation 仍要求可信 Token usage 和模型费率；任何 server-side usage counters 不得借新字段自动绕过现有未计价服务端工具 gate。
- 工具 reservation 不填输入/输出 Token，个人/企业 Token 图查询必须按 `billable_kind=model` 过滤；用量页面分别汇总 Search/Fetch 成功次数、unknown、客户扣费和待处理/不计费计数；C4 单条记录显示供应商成本与客户价格快照，不提供供应商成本或 margin 的用量聚合。
- `provider_attempt_started_at` 写入并提交是唯一网络发送闸门；同 key 重放只读取已持久化结果，找不到结果时一律拒绝重发。确定的工具结果与结算/释放事务性写入；该 marker 为空才可按未发送释放；明确不计费记录 `not_billable`，连接或响应不确定则保留 `unknown` 预占。陈旧的 marker 预占只能经租约过期、30 分钟等待和管理员恢复操作转 `unknown`，绝不自动结算/释放。
- C4 以原费率快照确认成功次数为 0 时，不扣积分；用量页仍将这类已核实的零成功 reservation 计入明确不计费调用，避免“已查明但消失”。
- unknown 无法证明未发送时保持冻结；管理员只有来源账单或可审计未发送证据，才能走当前双人 C4 人工流程。

## 异常策略

- 无活动目录或工具价为 0：不下发 schema，后端拒绝执行。
- 目录/钱包/限额/团队授权/企业状态校验失败：提交前无 HTTP 外呼。
- 授权变更发生在预占后发送前：最后发送门失败，释放未发送预占。
- 连接中断、读取超时、无法解析成功响应：unknown，禁止 provider 自动重试和任务自动重放。
- 明确 HTTP 错误：按官方失败不计费规则释放；不在当前工具 call 内重试。
- Search 空 `results`：按官方零结果免费合同释放；正结果每条调用只计一次，不按结果条数计价。
- Fetch HTTP 200 且 content 为空：按明确不计费结果标记 `not_billable` 并释放预占。
- Fetch 响应结构错误、正文缺失或无法判断供应商是否完成计费：unknown，冻结并交 C4；不得按同一调用重发。
