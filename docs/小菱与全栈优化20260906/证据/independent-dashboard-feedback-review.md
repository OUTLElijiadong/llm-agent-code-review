# Dashboard反馈修复独立只读审查

## 结论

需修改：3项可复现功能/校验缺口，另有测试覆盖退化。未发现本补丁新引入的日期旧响应覆盖或卸载后状态写入；3项行为缺口均为旧问题在当前目标中未完全收口，不能用已运行12+6宣称全面完成。

**仅本地未提交补丁审查，不代表已部署生产。** 基准HEAD/release：`343616d9d40c1e44e17c7da465c7173c418debb5`。复核期间两文件SHA保持：True。

## 161新增补证报告归因校正

- JSON事实：备份project已完成100行、161一行、主键唯一和8列解析；此前“尚未完成这些项目”是归因错误，已更正。
- 保留阻塞：当前161行规范化、最终字段相等及指纹输出未完成；不把备份100/1当作当前库聚合。SHA一致的采集器控制流只用于定位阻塞阶段，未新增数据库采集。
- 仅校正`/tmp/prism-optimization-20260906/independent-project161-backup-comparison.md`与对应JSON；未读备份、未改旧阶段报告。

## DASH-01 · P2 · 真实四类全零风险响应仍进入普通饼图，而非零数据空态

- 源码：`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:140`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:378`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/dashboard_service.py:165`。
- 现有测试：`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:88`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:166`。
- 反例：风险接口按真实后端行为返回严重/高/中/低四行且count全部为0。当前riskData.length为4，因此渲染BaseChart，未进入暂无严重度数据。
- 实测：隔离探针确认.chart-output存在。组件未设置stillShowZeroSum；已安装ECharts的PieSeries.js:144默认true，pieLayout.js:121在总和0时分配相等扇区。未执行真实Canvas视觉验收。
- 新旧归因：既有零值显示缺陷在本次空态修复中未覆盖，非新引入的日期竞态。HEAD原实现也仅按riskData.length判断。
- 需改：先验证计数合法，再区分成功且总计为0与真正非零分布；全零显示明确空态或无扇区的零值图，仍允许导出真实零统计，不把零判作错误。
- 应补测试：用后端真实四行零计数替代仅[]的空态假设，断言无普通饼图且不伪造比例；保留[]兼容测试。

## DASH-02 · P2 · 图表非法值被标记为成功，使完整统计导出门禁失效

- 源码：`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:472`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:485`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:495`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:510`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:519`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:221`。
- 现有测试：`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:140`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:209`。
- 反例：风险count=-1；维度count为字符串"3"；评分score=101；频次date为非法日期且count=-1。响应均为数组，所以map/聚合不抛错。
- 实测：隔离探针四区均为success，exportDisabled=false。accept只转换不校验；维度0+字符串可能拼接，日期可能变为Invalid Date，越界评分进入百分比宽度。
- 新旧归因：旧实现已有图表载荷校验缺口；本次新增success状态及canExportCurrentData仍把请求返回成功等同于数据有效，因此本轮禁导出不完整/非法统计目标未闭合，不认定为生产新回归。
- 需改：在各accept写入响应式状态之前完整验证数组及成员：计数为非负安全整数、评分有限且在0至100、日期符合约定且有效、必需标识/标签类型正确；失败只把对应分区置error，不覆盖旧缓存、不静默把非法值转为0。
- 应补测试：为四图分别添加null/负数/字符串/非有限/越界/非法日期矩阵；断言区块错误、其他成功分区保留、导出按钮和直接调用处理函数均拒绝。

## DASH-03 · P2 · 摘要仅验证recent_tasks为数组，非法成员仍在成功态触发渲染异常

- 源码：`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:461`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:426`。
- 现有测试：`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:140`。
- 反例：摘要计数及均分全部合法，但recent_tasks=[null]。入口只做Array.isArray即赋值并置success，activityFeed访问成员id时抛错。
- 实测：隔离Vue错误处理器捕获1次渲染异常，同时summaryState仍为success，而非摘要error。没有把合成输入当作真实生产数据。
- 新旧归因：旧活动流也直接访问任务成员，本补丁增加摘要校验后仍残留此洞；不是新增的数据删除或评分口径变化。
- 需改：在summary.value赋值之前按RecentTaskOut约定校验每个成员及用于渲染/HTML转义的字段；非法成员使摘要受控报错，不默默丢弃后仍称完整成功。
- 应补测试：覆盖null成员、错误字段类型、非有限/越界分数，确认没有Vue渲染异常、摘要可重试、导出不可用。

## 已确认的保护与数据口径

- **未知与真实零**：摘要首载/失败均不展示统计卡和完成数；review_count=0时均分为破折号且无风险徽标；成功审查真实0分仍显示0.0及极高风险。 验证方式：`source_and_test_assertion_review`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:30`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:222`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:121`。
- **日期竞态**：loadCharts同步发起四请求、每区独立版本号；旧响应在accept前被挡住，旧失败也不能改变新状态。原两个风险图deferred测试确实命中两条分支。 验证方式：`source_and_test_assertion_review`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:472`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:528`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:181`。
- **失败保留边界**：之前成功的图数据保留在内存，但新请求loading/error时隐藏；未失败的累计摘要继续展示；对应单图重试不重载摘要或其他图。 验证方式：`isolated_probe_passed`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:138`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:472`。
- **摘要重叠刷新**：summaryVersion使较旧摘要不能覆盖最新摘要。 验证方式：`isolated_probe_passed`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:455`。
- **卸载后的在途结果**：disposed拦截成功赋值和catch状态变更；卸载清除刷新timer并移除事件监听。探针分别结算成功摘要和失败风险请求，未观察到状态写入。 验证方式：`isolated_probe_passed`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:460`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:477`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:628`。
- **统计口径与宣传**：概览是累计可见范围，recent_tasks为最近成功任务，评分是最近6条而非按分数TOP或所选日期区间。频次含未删除各状态任务，现标题为审查任务趋势；导出文本区分累计与区间。v3.4/1M/实时流式宣传已移除。 验证方式：`source_and_backend_contract_review`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:510`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:568`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/dashboard_service.py:95`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/dashboard_service.py:122`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/dashboard_service.py:206`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/dashboard_service.py:238`。
- **复用reviewScore**：riskLevel和scoreColor复用既有reviewRiskLevel/reviewScoreColor；没有保留另一套90/75阈值。新测试验证真实0分，其余阈值未在本次重新执行。 验证方式：`source_review`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:300`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:398`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/utils/reviewScore.ts:11`。
- **追加导出错误反馈修复**：当前window.open保留noopener,noreferrer，不判断返回句柄；抛错时释放URL并error返回，未抛错时60秒回收且仅info已请求打开。该旧缺陷已从代码上修复，未宣称实际窗口打开成功。两条新测试分别断言null不即时回收/60秒后回收、throw时error与释放；公共setup清理fake timers和stub globals，未见此处测试污染。定向运行结果仍待主线程回执，本代理未重跑或构造窗口句柄。 验证方式：`latest_diff_and_test_assertion_review`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:588`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:230`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:249`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/test/setup.ts:63`。

## 新测试是否有效

- 原12例的deferred请求、分区状态和请求次数断言有实际行为区分力，不是仅匹配源码字符串；风险图两条旧响应测试分别覆盖旧成功与旧失败。
- 6条权限用例跨6个页面，仅1条直接涉及Dashboard，不能记为18条Dashboard专属用例。
- BaseChart通过JSON展示option，因此能断言数据进入组件，但不能替代真实ECharts视觉验收；useCountUp和SecurityPostureCard已桩化，不能据此声称动画或态势区生命周期被测。

- **TEST-01 导出改名令旧权限测试的DOM否定断言失效**：旧断言仍检查不存在“导出周报”；现在按钮名为“导出统计报告”，即便错误渲染按钮该文字断言仍会通过。computed权限false断言依然有效，但不能替代模板可见性断言。 需补：按data-testid=export-dashboard断言不存在，并验证无权限直接调用处理函数不打开窗口；可在新的Dashboard.test.ts补齐，不需要本代理改动其他源文件。 来源：`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/permissionVisibility.regression.test.ts:227`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue:21`。
- **TEST-02 测试标题包含加载态禁导出，但该case只模拟拒绝**：该用例证明HTTP失败时按钮禁用，并未保留deferred请求验证loading，也未直接调用onWeeklyReport验证第二道守卫。追加两条导出测试覆盖null及抛错，不替代这两个入口状态。 需补：补deferred加载分支与直接调用处理函数，不构造浏览器窗口句柄，也不把请求打开等同于打开成功。 来源：`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:209`。
- **TEST-03 原12例没有卸载后结算和已有数据再失败场景**：afterEach卸载不等于卸载后再resolve/reject。现有日期竞态两个用例真实覆盖风险图区的旧成功和旧失败；不应称为四图、摘要、卸载及SecurityPostureCard全部覆盖。 需补：把本次已通过的失败保留、摘要重叠、卸载保护探针纳入正式测试；如宣称四图竞态全覆盖，应参数化各key。 来源：`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:94`；`/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts:181`。

## 独立隔离探针（与主线程计数分开）

| 用例 | 实际结果 |
|---|---|
| 真实后端四类全零应显示空态而非普通饼图 | 失败：反例已复现 |
| 四图非法字段应各自错误且不可导出 | 失败：反例已复现 |
| 摘要最近任务非法成员应被入口拒绝而非渲染异常 | 失败：反例已复现 |
| 日期刷新失败隐藏旧图并保留未失败摘要且仅单图重试 | 通过 |
| 摘要重叠刷新仅保留最新请求结果 | 通过 |
| 卸载后在途成功和失败不写状态且移除事件监听 | 通过 |

独立探针合计6例：3通过、3失败；失败表示预期保护未满足。全部API均为本地合成桩，无生产或真实模型调用。原始结果：`/tmp/prism-optimization-20260906/dashboard-probe/probe-results.json`。探针执行于追加导出两例之前，其验证分支在最新diff未改；新导出分支仅代码与断言复核，不冒充重跑14例。

## 主线程全量结果：待回执

- 当前静态声明数：Dashboard新测试14（原12+追加2），跨页权限测试6；这是声明数，不是本代理复跑通过数。
- **587仅是12新case阶段历史计数，不可用于最终封版；589仅为主线程预计重跑总数，尚未独立核实。**
- 全量前端测试最终计数、类型检查和build仍标记**等待主线程结果**，JSON中最终计数为null。收到对应原始结果及代码SHA后再独立汇总，不与6个探针混算。

## 审查快照与只读边界

- `/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.vue`：SHA256 `fe31c02451a634e9dd7442e129723074010218ccc62792900d16784889083a70`；读后保持=True。
- `/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src/views/dashboard/Dashboard.test.ts`：SHA256 `2721faf2ae2e7424d4eea3656f30a8d2016e77d893097474922129fec0fc303b`；读后保持=True。
- 不编辑源代码或主仓库测试、不接触team补丁、不执行生产或认证操作；两份反馈文件均0600。
- 161补证更正不改变“仍阻塞”的结论，不修改之前的release-precheck、production-review、post-quarantine-review。

---

## 追加：定向结果重计与R05单条件复核

- 采样UTC：2026-09-06T00:08:07.515377+00:00。本节追加，不覆盖既有审查结论；3项Dashboard反例仍保留。
- **前端定向20/20通过**：`/tmp/prism-release-20260906/dashboard-export-green.xml`逐testcase重计，14新增+6跨页权限，0失败/错误/跳过。
- 导出红阶段XML为14例：12通过、2失败；日志另有1个unhandled error，不把它混作第15个testcase。
- **R05局部107/107通过**：服务61、mesh dispatcher 11、team dispatcher 30、API 5；XML逐项重计。对应JSON是执行回执（exit_code=0），不是另一份107例，不重复计数。
- missing-verifier新三例：红阶段2失败1通过；绿色107中三例均存在且通过。

### R05仅复核一行

- `/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/app/services/agent_team_service.py:1513`：`if not verification and not running:`，AST与约定一致。
- 有在途running时不提前failed；队列阻断不清除正在执行的lease，父团队继续running。最后在途结束后缺verifier仍failed；没有running仍立即failed。
- `/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/tests/unit/services/test_agent_team_service.py:920`覆盖最后任务成功/失败两分支，并断言父团队终结时机、lease保持及只一次failed事件；`:951`覆盖无running立即失败。本次不重做58项评审，不复跑服务。

### 全量终态仍有边界

- 当前全量路径XML/JSON实际分别为587/587例。**没有取得589终态结果，不以旧587封版，也不把预计589写作实测。**
- 后端全量XML本次已独立重计：**2820通过，0失败/错误/跳过**，读取期间稳定；执行JSON回执`exit_code=0`，SHA256为`74b324b5a2c869737a9b5c1e8cf5d97f0b6f0f7d008c46f3b959b8b5719e5e41`。JSON是执行回执，不重复累计测试数；未以107局部替代全量。
- 不宣称真实登录、真实模型或MySQL多进程验证；不改源码、不访问生产。两份反馈继续0600。
