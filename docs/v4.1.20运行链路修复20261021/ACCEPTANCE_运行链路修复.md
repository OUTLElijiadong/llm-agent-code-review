# 运行链路修复验收记录

观察日期：2026-10-23。本页当前为最终候选集成阶段记录。最新只读核验生产为 4.1.22 / `69991e04a8a3ed6d45220320eea4e0c8e027b3ee`，不能将候选测试写成生产完成；下文带日期的早期生产状态仅为历史记录。

## 已复现与修复

- 页面运行检查点晚于客户端动作恢复：先回读同账号、surface、session、run的持久状态，确认存在后再拉取动作；轮询与回执受会话代际和终态约束。真实无权限或不存在仍保留403/404。
- 消息死信投递与迟到模型回调竞争：对持久终态加锁刷新、幂等返回，保留 `dead_letter`，不放宽非法状态转换。
- 圆桌JSON围栏、抽取不完整和当前读竞态：剥离合法围栏、有界修复与完整性检查，终态不复活；无完整覆盖不出分。
- 星图在真实建队/临时成员/派发时更新；可展开每个成员的工作、状态和结果。界面支持鼠标和键盘缩放、尺寸保存及窄屏让位。
- 导航以当前账号能力为依据；合法用户页面可发现，管理能力仍由权限检查约束。
- CVSS 3.1向量服务端复算；新版平台综合分采用70%安全项、30%原规则项，缺向量如实回退。独立远程网站链路已对齐平铺评分契约，导出取0–10的基础分；只有确认发现进入计分，未分级观察拒绝出分。

## 验证证据与边界

|验证|已取得结果|证据位置|
|---|---|---|
|运行与CVSS集成|120通过，2警告|`/tmp/prism-runtime-cvss-integrated.log`|
|沙箱CVSS生产链关联回归|443通过，2警告|`/tmp/prism-sandbox-cvss-final-regression.log`|
|前端本地生产构建、ESLint|通过|`/tmp/prism-final-frontend-build.log`、`/tmp/prism-final-frontend-lint.log`|
|后端全app Ruff、diff检查|通过|`/tmp/prism-final-app-ruff.log`|
|中间提交bc622b13 CI|后端4失败、8471通过；前端、部署故障回归、CodeQL通过；此SHA拒绝发布|CI 39775288165、39775288185|
|4项CI失败本地复现及修复|圆桌失败终态同步修复；两项旧评分夹具更新；134项通过|`/tmp/prism-four-ci-fix-regression.log`|
|CVSS、远程与圆桌合并定向回归|127通过|`/tmp/prism-merge-cvss-roundtable.log`；不使用缺少原始日志的55项统计|
|最终候选完整CI与生产all发布|待最终SHA|以最终提交精确对账，不沿用旧SHA证据|
|最终生产三账号点击与Luna视觉复核|待发布后执行|按20项清单逐项留证|

以上测试集合有重叠，不能累加成总通过数。报告217的17项、29分属于历史规则快照；没有有效向量，不能冒充CVSS评分。此前PDF/Word抽查不等于全部逐页验收。先前LFI授权不作为本轮新授权。

## 资源核验

当前只读实查：MySQL上限1342177280字节、healthy、历史重启1次；根分区85%、可用28G，swap 3G已满。清理七天前归档journal释放约3.7GB；业务审计、数据库、权威备份和release未清理。以上是观察值，不是OOM已根治的证明。

## 并发发布核对

2026-10-22再次只读核验发现服务器正在发布独立导航/布局修复`af9c8d6169eb1909cbb5c629c64487872857514a`，当前运行服务暂仍为`2eb834a2`。本候选须先整合其改动并取得新SHA完整CI；不得用旧生产基线强行覆盖。

## 2026-10-22 合并 4.1.21 运行与布局修复

生产 af9c8d6169eb1909cbb5c629c64487872857514a 已于 13:10:52Z 完成完整 all 发布，本轮继续基于该版本整合，避免覆盖修复。后端冲突已按实际身份与路由契约解决：用户能力121项（原120项加审查员只读审计），管理模板不加入普通用户能力；平台配置 section 保留最高管理员校验。自动合并的重复圆桌守卫定义已去重，CVSS提示词同步新策略。

第一轮434项中433通过，1项旧模板测试与管理路由身份契约矛盾；调整为验证权限码不能绕过管理身份后，浏览器服务与Responses集成163项全部通过。完整合并候选CI、发布与最终点击验收仍待执行，局部证据不得替代最终验收。日志：`/tmp/prism-merged-backend-regression.log`、`/tmp/prism-merge-followup.log`。

整合完成后的后端定向回归434项全部通过，Ruff通过；独立复核建议已落实，补测failed页面运行终态后浏览器服务44项全部通过。Luna已解决10个前端冲突，定向Vitest五文件262项全部通过，vue-tsc及对应ESLint通过。并行Vitest曾出现超时/调用计数差异，最终完整CI须继续核验；本地构建、最终SHA完整CI、发布及真实点击仍按阶段记录。服务器24小时以前构建缓存清理回收0B，未删除镜像、卷或业务数据。


## 2026-10-23 最终发布前追加回归

- 普通账号真实点击复现“打开代码中心，不要发起审查”落入模型组队并错误要求高风险审批。前端纯导航识别器现按完整否定分句处理，不吞掉后续肯定业务请求。
- 后端站点团队在风险审批前通过现有合同解析器校验；缺失合同返回可修正参数错误，合法高风险合同仍走原指纹审批。
- 定向回归：前端导航83项通过（`/tmp/prism-navigation-after-frontend.log`）；后端站点团队及监督链118项通过（`/tmp/prism-navigation-after-backend.log`）。这些是本地证据，尚未作为生产验收。
- 发布前生产只读核验：tracked clean、无候选切换；根分区86%，27GiB可用，swap 3GiB全部使用。

- 评分收尾的独立审查指出：普通审查未知严重度不能默认为中级；主提示词需同步70/30；领域导出必须传递评分明细，并以向量复算每条发现。该组补丁待定向回归与最终CI，不提前记为通过。


## 2026-10-23 Luna 独立收尾复核

- Luna 只读复核未提交补丁，定向后端120项、导航83项通过，Ruff及差异空白检查通过。日志为 `/tmp/prism-luna-final-pytest.log`、`/tmp/prism-luna-final-navigation-test.log`、`/tmp/prism-luna-final-ruff.log`。
- 复核指出主提示词将转换后的安全分简写为CVSS基础分；已改为明确公式 `安全分 = 100 − 10 × 最高有效CVSS 3.1基础分`。最终完整CI仍需绑定收尾提交，生产点击尚未开始。

## 2026-10-23 全量门禁追加复现

候选 `956f0debbc6abcc986029299f7ed114668c9defe` 的全量后端结果为8561通过、1失败（GitHub业务回归39818988121）。失败为旧解析契约仍期望丢弃未知严重度条目；本地已复现同一断言。更新该测试为验证条目保留为“未分级”且诊断、输入计数不丢失，与本轮拒分策略一致，未回退实现。新提交仍须重新取得完整CI、CodeQL与生产证据。

### 最终 CVSS 报告一致性补充（2026-10-23）

- 圆桌正式任务结算后，新增读取实际问题统计、报告复算、JSON 导出的集成断言：分数均为 40，CVSS 分项及逐项向量保持一致。
- 沙箱领域导出有已评分发现但评分分项未保存向量列表时，明确说明缺少明细；不再错误显示“无有效向量”，也不反推或伪造向量。
- 本地相关两个测试文件合计 96 passed、1 warning（91.63 秒）。日志：`/tmp/prism-final-cvss-integration.log`。
- Luna 独立复核撤回圆桌初始 `task_id=0` 所致误报，确认正式编排、结算、报告和导出链路。此处仅为源码和定向回归结果，最终生产验收另记。

## 2026-10-23 页面团队桥补充

新增真实页面团队入口和租约代次校验；Luna 复核指出父运行停止与团队完成的交叉状态，已用两项红测试复现，补丁改为先核父运行。31项页面桥回归及星图适配回归正在执行，结果待补。

上一轮101项中98通过、3失败，失败均为新增 ACK 夹具 snapshot_id 使用非十六进制字符；修正夹具后这3项单独通过（`/tmp/prism-browser-ack-fixed.log`）。不将夹具错误计为产品根因，也不把修正前整组称为通过。


## 2026-10-23 停止意图与创建确认追加核验

- 页面父run保持running但stop_requested=true：补丁前4项失败，证据 `/tmp/prism-browser-stop-red.log`；补丁后8项通过（包括派发前停止和动作返回后停止），证据 `/tmp/prism-browser-stop-green.log`。活动动作被取消或记不确定，不重放，已完成账本保留。
- 108项页面桥/旧browser/stream联合回归全部通过（447.46秒），证据 `/tmp/prism-browser-stop-final.log`。
- Luna发现创建高风险或未分类团队仍在Responses和service层强制确认，报告 `/tmp/prism-luna-team-creation-qa-audit.md`；已作为当前发布阻断实施修复，不将已存在普通问答片段测试冒充整链完成。
- 当前网页仍显示4.1.21/af9c8d61；上述新补丁未发布，尚不能确认生产已修复。

- 星图组件8项定向Vitest、文件级ESLint通过；前端全量类型检查需最终CI补验，不能以长时间无输出记为通过。
- 发布前只读复核确认生产4.1.21/af9c8d61、tracked clean、无pending；可用磁盘25G（87%）、可用内存3.0GiB，最近24小时未检出内核OOM。最近定时备份成功，当前仅核验文件存在，正式发布仍须备份与隔离恢复门禁。


## 2026-10-23 页面停止及租约锁边界补充

- 父运行停止标记使用 locking current read；页面动作统一 team → task → parent 的锁序，轮询不得跨 sleep 持锁。
- 原停止边界三入口注入交错测试已通过：`/tmp/prism-parent-stop-lock-green-valid.log`，3 passed。联合回归 `/tmp/prism-browser-stop-lock-regression-valid.log`，111 passed。
- 独立复核发现等待 parent 锁期间任务租约可自然过期。新增时钟推进测试覆盖 dispatch/claim/ACK，修复前 `/tmp/prism-browser-parent-wait-expiry-red.log` 有效复现 3 失败；修复后 `/tmp/prism-browser-parent-wait-expiry-green.log` 3 passed。
- 测试为 SQLite Session 注入交错，不冒充真实双连接 MySQL 锁等待证据。最终补丁联合回归及独立复核正在收集。
- 星图空挂载到 forming/真实团队的 ResizeObserver 生命周期测试：9 passed；容器消失和卸载时断开，旧回调不覆盖新布局。
- `vue-tsc --noEmit` 已通过；评分后续变更仍须最终 CI 重新验证。
- 本次只读生产基线仍为 4.1.21 / af9c8d6169eb1909cbb5c629c64487872857514a，候选 4.1.22 尚未部署。

- 最终页面动作联合回归 `/tmp/prism-browser-parent-wait-regression.log`：114 passed。Luna 独立复核 `/tmp/prism-browser-parent-wait-final-review.md` 未发现新 P0/P1；本机未确认专用隔离 MySQL，不将用途未知的 3307 实例用于测试。


## 2026-10-23 最终集成候选

- 创建高风险或未分类团队的 Responses/service 两层确认已移除；创建不写入当前账号已确认标记，也不注入远程执行授权。成员实际执行继续核验权限、合同和任务指纹。
- 普通问答集成测试改为仅替换模型调用，保留真实临时成员 handler、调度、DB 持久结果、同轮团队读回和完成守卫。最终通过状态以本次候选完整 CI 为准。
- 修正 Sentinel/full 与圆桌终态四档严重度计数遇到未分级发现时的 KeyError，拒分原因统一为 ungraded_findings；报告补充 FIRST 归属与 CVSS 向量。
- 本地后端全 app Ruff、diff 检查、锁定依赖漏洞审计通过。此次集成候选尚待完整 CI 和生产 all 发布；生产核验仍为 4.1.21 / af9c8d61。


## 2026-10-23 最终补齐

- 报告导出、CVSS 综合评分及 Sentinel 三文件联合回归：147 passed；日志 /tmp/prism-cvss-final-integration.log。保留无向量不得展示模型裸分的检查。
- 报告详情页全文件复测：49 passed；日志 /tmp/prism-cvss-reportdetail-rerun.log。先前1000项分页用例超时，原配置复跑通过，未放宽断言。
- CVSS 元数据带 FIRST 规范引用；详情页和 HTML 显示规范链接，PDF/Word 包含完整可读 URL。70/30 是平台规则，非 FIRST 官方综合公式。
- 建队与执行授权四项集成回归：4 passed；日志 /tmp/prism-team-autocreate-final.log。未分类测试账号补齐真实 RBAC 夹具，未扩大生产权限。
- Luna 只读最终复核未发现可证实 P0/P1；报告 /tmp/prism-final-integration-review.md。
- 本节仅为候选本地结果。精确 SHA 全量 CI、备份恢复、生产发布和真实点击仍需分别验证。

### 最终建队契约与依赖门禁补齐

- 旧候选 740281dd 的全量 CI：后端 8599 通过、13 失败；前端被依赖审计阻断，未进入测试。该 SHA 未部署。失败按原日志逐一复现，未关闭门禁。
- 导出 FIRST 元数据、未分类成员真实 RBAC 夹具及旧建队确认契约已修正；四个完整文件合计 136 passed，2 warnings，日志 /tmp/prism-creation-contract-final.log。
- 独立复核 /tmp/prism-creation-test-review.md 未发现 P0/P1；删除读回 fixture 显式设置执行期授权前置条件，创建事件不授予执行权。按复核建议将 fixture actor 与真实 user actor 对齐。
- DOMPurify 与 Vitest 补丁升级用于消除 CI 审计阻断；最终版本和审计结果以后续精确提交 CI 为准。

前端依赖最终本地审计：DOMPurify 3.4.17、Vitest / coverage-v8 4.1.12，官方 npm registry audit 为 0 漏洞；npm ci dry-run、npm ls 均退出 0。日志 /tmp/prism-frontend-audit-after.json、/tmp/prism-frontend-lock-ci-dry-run.log。构建与全量测试仍交最终 SHA CI 验证。


## 2026-10-23 4.1.22 上线与导航真实验收续修

- 4.1.22 / `69991e04a8a3ed6d45220320eea4e0c8e027b3ee` 已完成 all 发布；HTTPS 健康、精确 SHA、镜像与发布账本一致，备份及隔离恢复成功，迁移为 `064_remote_pentest_chain`。ops-check 为 degraded：磁盘87%，无阻断项。
- 该精确提交 CI：后端8613通过；前端161文件2258通过；部署/执行器176通过、2跳过；发布绑定33通过；前后端依赖审计0漏洞；CodeQL两语言通过。原始证据 `/tmp/prism-69991e04-full-ci/`。
- 普通问答真实点击：团队392两名成员及其任务完成，无建队确认，最终汇总明确团队已完成。截图与会话快照 `/tmp/prism-69991e04-browser-qa/`，不能替代其他路径验收。
- 页面动作新发现：左侧可见“项目管理”，inspect却无对应控件；用户页面观察器仅枚举main，遗漏位于其外的nav。已保存失败截图及快照。不得用navigate替代点击并声称通过。
- 4.1.23候选补入可见导航观察与优先分配的100控件预算；业务弹窗优先、助手排除、账号/会话/运行绑定、失效快照拒绝及真实点击遮挡检查继续生效；无目标滚动仍作用于主内容。
- 本地三文件回归43通过：原生nav/ARIA navigation发现并真实点击、长列表导航预算、隐藏/助手导航排除、弹窗背景隔离、主内容滚动。日志 `/tmp/prism-nav-green.log`。类型、完整CI及此候选生产复测尚待完成。
- 已在4.1.22生产通过小菱重新发起项目13/1.py圆桌验收；截至本节写入尚在执行，不提前宣称圆桌成功或覆盖完整。

- 后续真实点击发现：团队396只完成了文件核对；真正圆桌团队397以 `roundtable_incomplete` 失败，随后自动创建正式审查替代团队。父运行又触发397终态与回复成功声明不一致的守卫。保留失败现场 `/tmp/prism-69991e04-browser-qa/05-圆桌397失败现场.txt` 与同名jpg；正式审查不能替代圆桌验收，本项重新打开。
- 导航候选补丁定向回归43项通过；全量 `vue-tsc` 与文件级ESLint退出0（终端73844），不以静默日志推断通过。


## 2026-10-23 新圆桌真实失败与 4.1.23 续修

4.1.22 真实圆桌团队397／会话`disc_5d182c83f1`／报告232失败。数据库错误为“圆桌报告整理失败：Object of type datetime is not JSON serializable”，覆盖0/1文件。随后团队398创建的正式文件审查233完成1/1文件、34项问题；它不能替代圆桌通过。

根因：`DeepSeekAgent.call_raw()` 返回的审计元数据`create_time`是带时区的datetime，检查点直接`json.dumps`。新增集成回归在原代码精确复现`start_discussion:931 → save_checkpoint:720`的TypeError。修复仅在检查点边界把已知时间字段编码为ISO字符串，恢复时还原datetime；不使用宽泛`default=str`，保持审计日志ID、时区、微秒和原始响应正文。该回归已通过，同时验证已提交专家发言与主持总结恢复后不重复调用模型。日志：`/tmp/prism-roundtable-datetime-red.log`、`/tmp/prism-roundtable-datetime-green.log`。

候选4.1.23仍须完整CI、all发布和新圆桌成功证据；本地修复不代表生产已修复。


### 4.1.23 候选恢复回归与历史报告复核（2026-10-23）

- 圆桌相关五文件共174项：173项通过，`test_successful_extraction_is_reused_after_shutdown[1-resume]`在本机高负载下等待8秒进入归一化线程超时；未改断言、未改产品，原项单独复跑65.06秒通过。完整CI仍需绑定新候选提交，不能复用69991e04的结果。证据：`/tmp/prism-roundtable-regression.log`、`/tmp/prism-roundtable-timeout-rerun.log`。
- 普通用户真实点击报告#217：页面17项、29分；下载的Word文本核17项、严重0/高6/中6/低5、29分；PDF第一页实际渲染确认17项、29分且版式可读。Word已核全部文本，PDF仅视觉核首页，不能据此声称逐页排版或逐条正文全部一致。
- HTML预览真实点击成功打开新窗口，但浏览器安全策略拒绝读取blob协议页面；HTML正文尚未独立复核，不绕过策略。截图及文本：`/tmp/prism-69991e04-browser-qa/07-报告217页面及HTML预览.*`；PDF首页：同目录`217-PDF首页.png`。
- 报告#233是替代正式审查，界面显示1.py、34问题、0分；不能作为失败圆桌#232的成功证据。项目和文件最终归属以数据库实体与实际页面为准，不沿用模型生成的项目名称。

## 2026-10-23 候选集成核对

圆桌失败原因透传补丁限定两份文件，Luna 回报目标文件全量 37 项通过、定向 3 项通过；未保存专属终端日志，不混用同名的其他测试日志。生产 00:37Z 只读核验仍为 4.1.22 / 69991e04，tracked clean，无 pending；三容器健康，根分区 88%，swap 3G 已满。候选 4.1.23 尚待精确提交 CI 与发布。
