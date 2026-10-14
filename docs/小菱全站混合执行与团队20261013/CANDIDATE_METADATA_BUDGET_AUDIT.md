# 团队结果预算与列表元数据独立复核

- 候选：`1dd46c05db02a1794c459c6b4b0e688c03927fb1`。
- 本次结论：本地契约复核未发现已确认阻断缺陷；不代表生产业务验收通过。
- 方法：重新读取结果投影、完整脱敏器、分页、owner/team/task限制、工具参数与编排转发、列表窄列和状态COUNT；独立实际执行两份测试文件，并增加多页原文重构样本。未修改代码、未登录账号、未操作GUI。

## 实际执行与证据

1. `test_team_business_projection.py` 18项与 `test_agent_team_list_metadata.py` 4项，共22项通过。原SQL捕获案例验证列表不SELECT结果/摘要/输入/能力JSON、不读取成员，task只做COUNT；100万字符团队摘要和20万字符任务正文均未被列表载入。
2. 独立增加80组结构摘要预算样本：预算1024、1600、8000、16000、24000各16组。UTF-8计量为 `len(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode())`，包含摘要本身、预览、字段名、引用与哈希元数据；80组均未超预算，原秘密值不公开，结构摘要哈希与完整脱敏值规范JSON相符。
3. 独立内存SQLite中生成25个长Unicode字段名、25份正文。通过两页集合、全部字符串页、包含中间数组的索引引用逐一完整重构字段名和正文；key_reference还实际经过固定工具参数校验和Orchestrator。秘密子字段为`[REDACTED]`，原结果JSON不改。
4. 独立增样：他人管理员owner两条读取均拒绝；非法转义、负数组下标及越界数组下标三条均拒绝。既有专项还覆盖不属于团队/不存在任务、路径与索引互斥、bool/负值/越界/深度、同账号分页、surface/session/status过滤与倒序/偏移。

## 源码核对

- `agent_team_projection.py:17` 最小1024；先完整脱敏，再计算规范UTF-8尺寸；结构元数据超限先减field_names和preview，引用本身仍无法装入则显式拒绝，未伪造截断原文。
- `agent_team_service.py:1707` 先按当前账号读取团队，任务再按该team_id和task_id双限制；`1719`先完整脱敏，再走JSONPointer或稳定字典键序/数组下标。长字段名以entries/key_reference读取，字符串页2000字符、集合页最多20项、子项投影1024字节，索引最多32级。
- `tool_contracts.py:510`与`orchestrator.py:949`转发team/task/path/indices/part/offset/limit，未提供task_id时拒绝结果定位。集合引用实际可经工具参数及编排转发读取。
- `agent_team_service.py:1665`团队列表17列元数据；owner及surface过滤在数据库查询层，COUNT只对本页team_id做分组，未展开业务JSON。新旧任务统计语义一致：等待依赖计入queued，dead_letter/expired计入failed，cancelled只计总数。

## 边界

该证据是本地SQLite、合成样本和候选源码。它不证明生产API/Safari、1百万token、模型压缩后理解或长时间稳定性。分页输出有界不等于读取原值的进程内存有界：分页仍解析并完整脱敏原task.result_json；团队列表的窄列优化是另一条查询路径。生产发布与业务复测须单独记录。

## 文件

- `evidence/independent/prism-independent-budget-metadata-1dd46c05.log`
- `evidence/independent/prism-independent-budget-metadata-extra-1dd46c05.json`
