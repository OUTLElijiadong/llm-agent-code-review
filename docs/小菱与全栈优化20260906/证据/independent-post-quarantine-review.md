# 隔离后有界独立复核

## 结论：部分通过，161完整前后未变仍阻塞

- 复核生成时间UTC：2026-09-05T20:48:40.055342+00:00。
- 不把161证据不足写成已通过；没有为了补齐该项再创建库、恢复数据或重扫89表。

| 检查 | 判定 |
|---|---|
| 清单与committed回执 | 通过 |
| 四目标状态时间戳源码及已知关联 | 通过 |
| 四项目字段覆盖完整性 | 通过 |
| 核心6表行数保持 | 通过 |
| 当前health与release | 通过 |
| 161完整前后未变 | 阻塞 |
| 两条team现状核实 | 通过 |
| 本地17与32计数 | 通过 |
| 既有阶段报告未修改 | 通过 |
| 采集完成 | 通过 |

## 清单与完成回执

- operation：`874c7f1be92b4c338c93d232389c1b2e`；回执状态：`committed`；精确目标24、25、26、27。
- manifest SHA256：`d36ca7e949035d7310e563a96fd1919519d5a45fb95f2175d8abb4a21133b59a`。
- `.result.json` SHA256：`d7273989b447639d6bef2b374d88d2ede0030de26dfd9d258d07fa032cb4302f`；其manifest_sha256与清单实际哈希相同。
- 本次新备份402726075字节，SHA256为`7bb99942cc0e571008743fc66b0d1cdd88fcf13640579b32fb493c0679d7dc6f`；与用户及manifest备份元数据一致。恢复通过属于主线程证据，本次未重做恢复。

## 四目标及核心6表

- 项目24：通过；当前quarantined，manifest前后仅status不同；create_time/update_time与before相同。单文件1079字节、固定源码SHA256及相对路径匹配；既有任务数量和测试关联不变。
- 项目25：通过；当前quarantined，manifest前后仅status不同；create_time/update_time与before相同。单文件1079字节、固定源码SHA256及相对路径匹配；既有任务数量和测试关联不变。
- 项目26：通过；当前quarantined，manifest前后仅status不同；create_time/update_time与before相同。单文件1079字节、固定源码SHA256及相对路径匹配；既有任务数量和测试关联不变。
- 项目27：通过；当前quarantined，manifest前后仅status不同；create_time/update_time与before相同。单文件1079字节、固定源码SHA256及相对路径匹配；既有任务数量和测试关联不变。

四项固定源码SHA256：`5058a415e5bf92ea54204f5e691f3941f3d761d0a280c4b5cbb071dc8ea9f4e9`。description/language与原白名单一致，数据库project列集合已核对是否完整覆盖，未返回字段原文。

| 表 | 之前 | 本次 | 相等 |
|---|---:|---:|---|
| `project` | 100 | 100 | True |
| `code_file` | 1771 | 1771 | True |
| `code_version` | 1773 | 1773 | True |
| `review_task` | 146 | 146 | True |
| `review_issue` | 1201 | 1201 | True |
| `review_report` | 72 | 72 | True |

## 具体阻塞：项目161

161不在清单和committed回执目标中；现有历史字段与当前布尔核对结果见JSON。**缺少完整变更前快照，尤其update_time、description、language，无法独立证明其所有字段未变。** 当前cr_testdb容器只有sandbox_test九表，不能作为此次89表恢复基线。已停止扩展，不重建基线。

## 两条团队诊断

- task11/team5：queued / failed组合被独立核实。
- task35/team12：waiting_dependency / failed组合被独立核实。
- 仅确认现状，不推断完整成因、不归因于隔离，也不宣称已修复。

## 健康与版本

公网healthz/readyz及发布账本按本次采样核对3.8.4和完整release `343616d9d40c1e44e17c7da465c7173c418debb5`；详情与时间在JSON。

## 单独本地段：规范化17/32计数

- green.xml：17用例，17通过。red.xml：17用例，16失败、1通过；属于红绿验证阶段，不是生产失败。
- generated JSON：contract_count=32，agents=32，唯一code=32；静态读取源_CONTRACTS 15项与_SERVICE_CONTRACTS 17项，合计32且code集合完全相同。
- 未重跑测试或--check；本地规范化补丁不属于已部署343616d9运行代码，生产结论不引用此结果。

## 只读与保留边界

- 原precheck、production-review和来源白名单等5份阶段文件SHA256保持不变。
- 不返回源码、说明、环境变量或密钥原文；不写主仓库、生产或业务数据，不调用模型。
- 既有阶段报告不修改；本报告与对应JSON均0600。
