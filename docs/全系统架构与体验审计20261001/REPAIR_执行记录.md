# 全系统审计修复执行记录

用户于 2026-10-01 要求“帮我按顺序检查并修复”。本记录按既有 TODO 清单串行推进；此前审计文档中“只读、不改应用代码”的约束仅适用于盘点阶段，现由本次明确修复指令更新。每项保留修复前复现、代码范围、修复后相同场景与扩展样本。候选测试通过不等于生产发布或全系统验收。

## 执行顺序

1. B01 私域摘要权限
2. B02 执行时撤权
3. B04 REST/小菱/团队共享准入
4. F02 团队确认操作目标冻结
5. O01 证书状态路径
6. A01 上下文压缩事实保真
7. A02 委派依赖版本冻结
8. A03 高风险重试确认
9. F01/F03/F04/F05 异步与卸载一致性
10. B03 文件并发保存
11. A04 团队波次调度
12. A05 会话检查点存储
13. O02 管理页查询与轮询
14. F06 团队详情全文展开

## B01 私域报告摘要权限

- 修复前：新增的真实 API 回归在隔离 SQLite 项目里，`pentest` 与 `sandbox_test` 两类任务都在审查任务出口返回 `score=null`，而项目列表仍返回 `score=73`；新增断言在两个类型上均失败。此前独立探针还覆盖 owner/member 两种角色。
- 修改：`review_service.can_view_task_metrics` 统一复用报告可用性、`report:view`、任务归属/管理员判断。项目列表只隐藏无权查看的领域报告评分；项目详情近期任务同时隐藏评分与问题数。沙箱问题数从已有报告统计服务取值，避免其与详情页来源不一致。响应 Schema 允许受限指标为 null，详情 UI 用“—”显示空值。
- 回归：普通代码审查项目成员仍能看到其授权指标；仅增加 `report:view` 但不是任务创建者/管理员的成员仍看不到私域指标。
- 验证：`backend/.venv311/bin/python -m pytest tests/test_review_task_access_errors.py -q --no-cov`（backend工作目录）21 passed；`npm test -- --run src/views/project/FailureRecovery.test.ts`（frontend工作目录）25 passed。
- 边界：均为本地隔离数据库/组件测试；没有写生产数据，也未发布。
- 状态：候选修复完成，等待最终整体验收与既有授权范围内的发布安排。

## B02 执行时撤权

- 修复前：隔离 SQLite 后台 Worker 探针分别撤销项目成员资格、`review:start`、两者同时撤销；三种情况下 `_execute_review` 都收到 1 个冻结源码文件。原场景未调用模型，避免付费外发。证据脚本：`evidence/backend-probe.py`。
- 修改范围：`review_service.py` 在 Worker 启动、每个文件/分片边界、问题落库和最终成功提交前通过独立 SQLAlchemy Session 重验用户启用状态、任务执行租约、项目可见成员资格和 `review:start`。单 Agent `BaseAgent.call()` 与并行 `DeepSeekAgent.call_raw()` 在每次 HTTP 尝试前重验，包含上下文压缩请求与网络重试。撤权以 `failed + coverage.reason=authorization_revoked` 收口，保留此前已完成证据，不继续新调用。
- 修复后同场景：三种组合均为 `task_status=failed`、`coverage_reason=authorization_revoked`、`executor_invocations=[]`；具体成员撤销与 RBAC 撤销原因分别可辨认。第二轮重试授权被撤销时，HTTP 请求桩只收到首次请求。
- 测试：`tests/unit/services/test_review_worker_reauthorization.py` 新增 6 个用例；与状态/恢复、Agent 并行、截断恢复、上下文压缩、任务读取授权回归合计 61 passed。隔离探针输出保存在 `evidence/复测-B02-后台撤权.json`。`git diff --check` 通过。
- 边界：当前证据来自本地文件型 SQLite 与网络请求桩，没有验证生产 MySQL 隔离级别或跨进程部署。撤权发生在已开始的模型 HTTP 请求中时无法撤回上游已接收请求；完成后不会启动新的尝试，也不会提交该次未完成审查结果。尚未发布。
- 状态：候选修复完成，待后续整体验收与授权范围内的发布。

## B04 REST/小菱/团队共享准入

- 修复前：隔离 SQLite 小菱工具入口同账号同一分钟连续调用 6 次，原先 6 次都进入准入并返回成功；REST 路由的 SlowAPI 装饰器未覆盖服务层/小菱工具入口。复测脚本：`evidence/backend-probe.py`。
- 修改：新增 `review_admission_service`，REST、小菱和团队调用收敛至 `review_service.start` 同一按账号配额。使用同一 Limits 存储后端和 `prism:api` 前缀形成共享额度；生产环境未配置 Redis 或共享存储出错时 fail-closed，避免静默降级为多进程各自计数。移除 REST 单独重复限流，直接声明 `limits` 依赖。
- 修复后：相同工具探针第1–5次接收，第6次返回频率限制；实际 REST + Orchestrator/ReviewOrchestratorAgent 服务混合测试证明同账号总额度共享、重新登录仍计入同账号、其他账号独立，存储错误及生产缺少 Redis 均阻止创建。B01/B02/B04/Worker/团队后端相关回归 183 passed。
- 边界：SQLite/测试存储与模型桩；没有连接生产 Redis、没有部署后验证多进程计数。配额从已认证的审查创建尝试开始计，业务参数错误尝试也占额度，沿用原 REST 入口限流时机。
- 状态：候选修复完成，生产共享存储配置和真实入口待发布后验收。

## F02 团队确认操作目标冻结

- 修复前复现：新增组件测试在改代码前3/3失败。确认弹窗等待期间同团队从一个失败任务变为两个时，API 实际收到新增的第二个 task_key；切换到其他团队后调用漂移到新 team_id/task_keys；切换账号后仍用弹窗前的用户意图提交。原始复现文件现保留为回归 `frontend/src/components/ai/AgentTeamWindow.target-freeze.test.ts`。
- 修改：在打开确认/输入框前固定 team_id、session_id、账号ID、认证令牌和失败 task_keys；确认完成后逐项比对当前可见目标，窗口关闭、团队/会话切换、账号切换或重新认证时取消请求。同步给不可逆取消团队按钮加入同样的目标固定。失败重试弹窗展示冻结的任务清单，并收集后端要求的新执行策略，逐任务键提交；策略至少8个Unicode码点，且不能复用任务原指令。
- 修复后：同团队后台出现新失败项只提交确认时的task_keys；团队/会话/账号/令牌切换均不调用重试服务；取消确认中切团队不调用取消服务；短策略被阻止。过期任务此前在前端属于可重试状态、后端却返回“没有可重试的失败任务”，该原状态测试先按旧实现失败，再将 `expired` 加入服务端可重排队集合后通过。团队 UI/时间/卡片相关 5 个测试文件 32 passed；后端 B01/B02/B04/团队组 183 passed；`vue-tsc && vite build`成功。
- 边界：组件与服务使用本地测试存储/桩，无生产页面真实点击或真实账号、网络竞态验证；候选改动尚未发布。
- 状态：候选修复完成；生产真实点击与发布后复测仍待整体验收阶段。

## O01 证书状态检查路径

- 修复前生产证据：`evidence/production-readonly-observations.md`记录最近6条 `certificate_status` 均失败，执行器访问 `/opt/prism-releases/<release>/deploy/certbot/conf/...`，而 `cr_frontend` 的只读挂载来自 `/opt/code-review/deploy/certbot/conf`。公开证书有效期到2026-12-29，`prism-cert-renew.timer` 为 enabled；未把监控路径错误误报为证书过期或续期失败。旧合成探针 `evidence/reproduce_certificate_path.py` 在修改前复现：配置`/persistent/certbot/conf`但实际解析到`/release/deploy/certbot/conf`。
- 修改：`deploy/prism_ops_executor.py` 的 `certificate_status` 使用统一解析函数，优先读取 systemd 实际继承的 `CERTBOT_CONF_DIR`，没有环境值时读取当前部署 `.env`；空值/缺省按 Compose 的 `./certbot/conf` 回落，相对路径按当前 compose/deploy 目录解析，绝对路径保持不变。未复制证书/私钥，也没有运行续期。
- 修复后：配置文件缺省、空值、普通相对目录、`../`相对目录、进程环境覆盖文件值、release A/B 切换共6个样本都将 openssl 指向预期目录；运维执行器 Python 回归21 passed，shell/deploy 集成脚本全通过。复测输出：`evidence/复测-O01-证书路径.json`。
- 边界：所有修复后探针均为命令桩且不打开证书文件。候选代码尚未部署，未能从实际生产 Unix-socket 运维入口重跑 `certificate_status`；部署后须确认结果路径、命令 exit code 和有效期。
- 状态：候选修复完成；真实生产入口复测随发布验收执行。

## A01 上下文压缩事实保真

- 修复前复现：审计场景中用户先前给出“统计时区是 Asia/Taipei，月末退款按原始交易月份记账。”；压缩器读取了全部来源、逐条引文合法，最终模型输入仍未包含该事实。原复现证据为 `evidence/复现-A01-压缩事实遗漏.jsonl`，事实进入压缩器但未进入最终输入。另新增长历史复现：>1200字符无句界的旧用户消息带有该规则，用户当前问题明确询问月末退款时区/月份，修复前最终模型输入遗漏事实。
- 修改范围：`backend/app/services/context_fidelity.py` 为短的无标记用户陈述建立逐字账本；对有句界的长消息保留可完整放入账本的句子；对无句界超长历史按当前问题词面召回相关用户原文片段，并保留来源索引、明确其未经事实核验。`deepseek_responses_runtime.py` 在生成压缩摘要和命中旧缓存时均附加召回事实；新查询不会被旧语义缓存挡住。为避免噪声导致摘要预算膨胀，长而无句界的普通内容不整条复制，词面候选需至少3个匹配词项，最多4个来源；已有账本或摘要已包含的事实不会重复追加。账本/召回超过预算时仍拒绝压缩投影，不截断。摘要格式版本升至3，策略版本更新为`agent-transcript-v4-user-fact-ledger`以淘汰旧格式缓存。
- 修复后同场景：同一遗漏事实和长历史问答复现均在最终模型输入中；新查询命中旧缓存也会动态加入相关原文。另覆盖来源索引、片段长度上限、噪声消息不复制、重复事实去重、账本超预算 fail-closed、历史格式缓存重建。`test_deepseek_responses_runtime.py` 与 `test_context_fidelity.py` 合计83 passed；`evidence/复测-A01-压缩事实保真.jsonl` 显示 `result_status=completed`、`fact_in_final_model_input=true`。
- 百万token边界：现有“超过一百万估算token”测试通过，但输入由本地模型桩处理；它证明分块/来源覆盖/预算逻辑的规模行为，不证明真实供应商接收一百万token，也不证明语义无损或模型完整理解。当前长历史召回依赖词面重合，模糊提问或没有可辨识线索时仍由模型语义摘要承担，无法作全语义完整性保证。
- 状态：本地候选修复完成。没有真实外部模型/生产调用，尚未发布；不得宣称AI在任意超长上下文中绝不遗漏或已被证明完整理解。

## A02 委派依赖版本冻结与真实执行节点

- 修复前复现：同一个父Agent发布包绑定同一委派Skill/checksum，子目标当前发布指针从v1改到v2后，父Agent解析出的上下文随之由鉴权职责漂移成数据库事务职责。原始探针 `evidence/复现-A01-压缩事实遗漏.jsonl` 中 `context_changed=true`；另加完整发布工坊回归，修复前稳定失败于 `after.skill_context == before_context`。
- 修改范围：发布审批后生成不可变release manifest时，递归收集Skill版本、checksum、sequence依赖和自定义子Agent release/version/package checksum/template checksum，并锁定目标Agent行；限制依赖闭包128个节点和深度8，校验所有版本checksum、发布状态和循环。release checksum覆盖该快照；任务执行核对快照manifest与不可变release一致。回滚新release保留原有依赖快照；旧包没有委派快照时，仅在形成新回滚包时冻结可核验的当前闭包。
- 真实执行：`DeclarativeReviewAgentDefinition`携带已冻结自定义执行节点。`invoke_published_agent`先按release/version/checksum调用自定义子Agent，对同一源码做完整证据校验，再将子Agent摘要、问题和覆盖信息作为带来源上下文交给父Agent；结果包含`delegated_runs`且usage累计。每次最多8个子Agent、沿用批准的最大深度2；循环在模型调用前拒绝，任一子调用失败则父任务不报完整。内置Agent当前保留为明确的职责模板，不会被声称已单独执行。
- 修复后：子Agent v1升级到v2后，旧父release仍引用v1；冻结目标被禁用时 fail-closed；父manifest中的子release checksum被篡改时解析失败。模型桩测试确认实际调用次序`child → parent`、传入精确release/version/checksum、子结论进入父输入；循环样本在调用模型前阻断。Agent工坊、发布上下文、运行、管理审批、Agent团队服务/调度6个测试文件共211 passed。复测输出 `evidence/复测-A02-委派版本冻结.jsonl`。
- 兼容边界：已有旧父release仍保持不可变；缺少冻结表时，每次调用入口解析并固定子Agent版本，并记录`legacy_resolved_at_invocation`。这避免单次运行中途漂移和旧Agent意外中断，但升级后的新调用仍可能采用新子版本，跨运行不声称完全可复现。release级固定依赖需通过工坊重新测试、管理员审批并重新发布父Agent；未自动改写历史release或绕过审批。
- 边界：所有模型调用均由桩替代；未连接生产模型、未做真实费用或并行负载验收。委派当前按冻结DAG深度串行执行，最多8个自定义子Agent；内置专家职责还不是实际委派节点。
- 状态：新发布包及自定义Agent实际委派候选修复完成；旧包仅单次运行冻结、跨运行依赖仍需重新发布治理；尚未发布。

## A03 高风险重试确认闭环

- 修复前复现：清单报告了原团队的失败重试把改道方案追加到 `instructions`，导致已确认指纹变化，而 dispatcher 正确阻止执行；确认事件只在创建团队时写入，用户无法在原团队上批准新尝试。新回归在修改前失败于 `preview_retry_team` 服务接口不存在，确认了原团队没有重试预览入口。
- 修改范围：新增同账户 `POST /agent-teams/{id}/retry/preview`，在团队行锁范围重算所选失败起点、依赖阻断及会被重置的已完成后继，模拟实际新尝试输入，返回任务风险、原因和绑定团队ID/账号ID/任务图/策略的 `plan_sha256`。重试接口在同一锁内再次计算；高风险计划缺少摘要或摘要过期均以409拒绝，任何任务状态均不变。确认后追加 `supervisor.retry_reauthorized` 事件，确认者只取当前登录用户；dispatcher 只接受团队归属人确认事件中精确匹配的高风险指纹。
- 指令与尝试：人工及自动重试策略保存在独立 `_execution_strategy` 元数据，业务 `instructions` 原文不再追加失败文本或改道策略。授权指纹包含该执行策略；重试图中的每个实际重跑节点（含 expired、blocked 和上游变化后重置的 completed 后继）都会获得独立尝试指纹，旧授权不可用于下一次失败后的新尝试。
- 前端与小菱：前端先收集策略、调用预览，再列出完整受影响任务、当前状态、依赖、风险和原因；所有重试都由用户对预览范围做最终确认，高风险显示危险确认样式，长列表可滚动。确认期间仍使用 F02 冻结的 team/session/账号/token/task_keys，确认后再由服务端校验摘要。小菱工具先返回风险预览；带摘要的执行调用被监督器升级为需当前账号确认，Orchestrator 只在收到已批准的执行上下文后调用重试服务。
- 修复后复测：服务/API测试覆盖缺少确认、摘要过期/策略改变不落库、账户隔离、精确确认事件、真实 dispatcher 指纹执行、同一任务再次失败需新指纹、原始指令不变及小菱未经监督批准不能续跑。相关后端服务/API/监督器组 226 passed（2个依赖弃用警告）；前端 API/冻结目标组件 12 passed，确认文案断言同时覆盖任务状态与依赖；`vue-tsc && vite build`、目标 Ruff 与 ESLint 检查通过。证据摘要见 `evidence/复测-A03-重试确认.json`。
- 边界：均为隔离 SQLite 和执行器/模型桩，未测生产 MySQL 行锁、多进程并发或真实浏览器生产账号；没有真实模型或外部副作用。候选未发布，不能声称生产已修复。
- 状态：候选修复完成；按序下一项为 F01/F03/F04/F05 异步一致性。

## F01/F03/F04/F05 异步状态一致性

- 修复前复现：将正确行为断言直接放入受控真实组件测试后，7项失败：用户小菱旧团队响应覆盖新完成状态、团队详情局部失败丢卡片、管理员端同样存在旧响应覆盖、IssueHub旧项目响应覆盖新筛选、403后旧问题仍显示、沙箱项目A副本覆盖项目B，以及沙箱初始加载卸载后安装轮询并再次读取。
- 修改：用户端与管理员端团队刷新分别维护请求代际，并在会话切换、账号/令牌变化和卸载时失效旧代际。团队列表成功返回时按服务端列表更新成员关系；详情局部失败保留仍在列表内团队的上次成功详情并提示快照未同步，成功空列表继续清空。IssueHub固定发起时筛选条件，仅最新请求可以写状态；401/403/404及对应业务码清空旧问题、总数和选择，普通网络故障保留旧快照并标注上次成功的筛选范围。沙箱修复副本请求绑定项目和请求代际；卸载守卫覆盖初始数据与 worker await，卸载后不注册全局事件或轮询。
- 修复后复测：受控前端组件的6场景连续独立运行三轮，每轮6/6通过；管理员端迟到详情场景另运行三轮，3/3通过。正式组件回归 `AgentChatDrawer.test.ts` 49、`AdminCopilot.test.ts` 48、`IssueHub.recovery.test.ts` 9、`SandboxWorkstation.test.ts` 10，共116 passed。`vue-tsc && vite build`、目标ESLint及 `git diff --check` 通过。日志和结构化摘要见 `evidence/复测-F01-F05-第*.log`、`evidence/复测-F01-管理员端-第*.log` 与 `evidence/复测-F01-F05-异步一致性.json`。
- 边界：组件/API均为本地 Vitest 桩，未调用生产 API或真实浏览器。F04只证明陈旧副本不会污染当前候选，后端项目归属检查仍负责最终拒绝，本次没有证据证明存在跨项目执行或越权。F02和F06完成后已对8个相关前端测试文件作整合复跑（183 passed）；旧只读复现脚本断言的是缺陷仍存在，修复后按期望行为测试判定，不把历史反向断言列为回归失败。
- 状态：候选修复完成；按序进入 B03 文件并发保存。

## B03 文件并发保存

- 修复前复现：隔离 SQLite 临时库中保留旧 SQLAlchemy `CodeFile` 实例，另一会话先提交新版本，再通过真实 `PUT /api/code-files/{id}` 保存旧草稿；3/3响应均为 HTTP 500/code 50000。新增双会话回归在实现前2/2失败，服务尚无版本前提。
- 根因与修改：写操作先以文件记录定位项目，之后按项目行→文件行固定顺序加锁；获得项目锁后用当前读刷新 `CodeFile`，并重新确认文件状态和项目归属。编辑器提交打开时的 `expected_version`；版本过期或缺少版本字段均返回 HTTP 409/code 40904，不创建版本、不覆盖服务端内容。前端的本地草稿不重置，给出复制、刷新和手动合并提示。版本历史恢复是用户主动选择的覆盖操作，但也使用相同项目/文件锁序，从最新版本递增。
- 修复后复测：真实路由探针连续运行3轮，每轮3个过期版本与3个缺失版本号请求，共18次均为40904、没有500；并发服务回归覆盖过期草稿不写入、版本号连续递增、历史内容与文件一致、SHA-256对应、旧ORM快照后的重命名及软删除保留最新内容。代码文件服务/权限隔离/并发组345 passed；前端资源权限和编辑器18 passed；`vue-tsc && vite build`、目标ESLint、Ruff、`git diff --check`通过。结构化证据：`evidence/复测-B03-文件并发保存.json`；逐轮API日志：`evidence/复测-B03-API-第*.log`。
- 边界：本机未安装/运行 MySQL/MariaDB，Docker 处于关闭状态。因此没有执行隔离 MySQL 双连接 barrier，不能据 SQLite 测试推断 `SELECT ... FOR UPDATE` 在 MySQL REPEATABLE READ 下的真实等待后刷新行为。全程没有访问生产数据库或生产数据，候选未部署。按顺序清单该 MySQL 验证是 B03 未关闭项。
- 状态：应用候选修复及 SQLite/API/UI 回归通过；MySQL 集成验收待隔离数据库可用后补做。下一顺序项为 A04 团队波次调度，执行时不将 B03 标为全环境验收完成。

## A04 团队波次调度

- 修复前复现：旧调度器在一批worker全部完成前不会再领取任务。事件探针确认短任务已结束但后继在慢兄弟运行期间未领取，只有慢任务释放后才领取。原输出：`evidence/复现-A04-wave屏障.log`。期望测试先红：`test_dispatch_once_refills_idle_worker_before_slow_sibling_finishes` 在实现前因1秒内未领取后继失败。
- 修改：调度器以 `wait(..., FIRST_COMPLETED)` 观察完成集；一批内任一future完成就统计结果、回滚和清除调度Session快照，再按团队轮转重新claim到空闲worker槽。仍保留全局worker并发上限、每次dispatch最大claim预算、lease时长与scheduler周期；没有降低权限/租约约束。
- 修复后复测：新测试以短/长任务分属两个团队，确认短任务后继在另一团队慢任务仍阻塞时已claim；事件场景连续三轮通过。已有三独立任务并发与真实持久队列/依赖执行测试通过。团队调度器和任务服务共153 passed，覆盖失败归约、部分汇总、取消、重试及lease/CAS服务规则；目标 Ruff、`git diff --check`通过。结构化证据：`evidence/复测-A04-波次调度.json`。
- 边界：时序验证用本地线程事件与服务桩，没有真实模型/生产队列、多进程调度争抢或生产延迟数据；候选未部署。B03 MySQL锁语义依然未实测。
- 状态：A04应用候选修复完成；按序进入 A05 会话检查点存储。A04生产/多进程验收留待发布后执行。

## A05 会话检查点存储

- 修复前证据：100轮、每轮新增10,000字符的SQLite样本把唯一历史1,004,190 bytes复制进检查点，累计50,736,535 bytes（50.52倍）；恢复100条历史耗时约0.1秒。证据：`evidence/复现-A05-累积checkpoint放大.log`。
- 首轮实现与复测：增加会话级追加 transcript 账本，checkpoint保存游标与整段摘要；为现存 inline checkpoint 提供首次写入时惰性合并。100轮复测为checkpoint 37,382 bytes、账本JSON 1,003,690 bytes、合计1,041,072 bytes（1.04倍），历史100项恢复且顺序一致。
- 独立复核增补：发现两个 API 历史快路径只验游标，不验合法JSON被改写；并发现旧 inline checkpoint 首次追加时整段重复写入。先添加篡改回归和旧记录追加回归并确认旧实现失败，再加逐消息SHA-256和读取校验、在旧 transcript 为现有会话历史前缀时只追加后缀。现复测中 store.load、服务端历史与可见历史对摘要篡改均拒绝；旧历史两条＋新增一条只恢复三条且顺序不变。
- 持久化规模：项目内 `estimate_tokens` 估算1,110,893 token的合成历史，1,100条追加1条，checkpoint 757 bytes、JSON账本4,442,553 bytes、消息摘要70,464 bytes，逻辑payload放大1.015倍；1,101项完整恢复，早/中/晚锚点与跨用户隔离通过。该数是项目估算器而非供应商tokenizer，输入由模型桩/SQLite处理，不证明真实模型完整理解。
- 迁移与回归：059新建append-only账本；060以250行为批次为已有JSON消息补SHA-256并约束非空。隔离SQLite迁移模拟对503条中文旧行实际调用升级逻辑，503/503摘要匹配；迁移图revision ID长度和060接续059均通过。Responses集成107 passed、迁移链测试2 passed、团队/归档/巡检服务43 passed、百万估算token语义压缩规模测试1 passed；独立复核109项主集成/迁移断言通过。目标Ruff、py_compile通过。详细探针：`evidence/复测-A05-累积checkpoint压缩.log`、`evidence/复测-A05-百万token-checkpoint.log`、`evidence/复测-A05-完整性兼容.log`。
- 证据边界：存储倍数只计 checkpoint JSON、账本 JSON和摘要字符，不是数据库页/索引/WAL/binlog/副本占用。旧inline checkpoint兼容迁移不回收历史checkpoint原有空间。没有生产数据、生产MySQL、真实供应商调用或磁盘回收测试；候选未部署。
- 状态：A05本地候选实现与独立SQLite/API复核完成，按顺序进入O02性能检查。MySQL迁移、生产磁盘物理占用及恢复验收留待目标环境。

## O02 管理概览查询与后台轮询

- 修复前复现：ToolCallLog SELECT随 Agent 数量从1个样本的3条增长到10个的12条、40个的42条；AdminOverview 隐藏后推进60秒，系统状态请求从1次涨到13次且 SSE 未关闭。证据 `evidence/复现-O02-查询放大与后台轮询.log`。
- 修改范围：`admin_overview.py` 将 top_action 分组一次并建映射，今日聚合改半开时间范围；ToolCallLog/AiCallLog各新增复合时间范围索引及前向迁移061。`AdminOverview.vue` 把可见兜底间隔调至30秒，隐藏时停止轮询和SSE，可见时立即刷新、重连。UTC午夜及计数预算测试均冻结时钟，避免跨日不稳定。
- 修复后复测：后端活跃统计/总览/迁移组14 passed，前端AdminOverview 41 passed；1/10/40 Agent样本均为3条ToolCallLog SELECT、每Agent5次调用、用途common。SQLite每日志表加载20,000行，工具及AI实际范围聚合计划都选择新增索引；AI投影包含调用数、有效Token sum、未知用量sum及标签过滤。完整日志 `evidence/复测-O02-查询预算和可见性.log`。Vite生产构建、目标Ruff/ESLint、py_compile、迁移唯一head和diff检查通过。
- 独立复核：复跑14项后端、41项前端和20,000行EXPLAIN探针；另将模型日志默认写入时钟切到2027年，固定2026年样本仍通过。确认AI SQL探针与ORM聚合投影等价、061 ID长度及迁移链合规，无新阻断。
- 边界：SQLite计划不是MySQL生产计划，未评估索引DDL锁、生产延迟、并发负载或真实浏览器后台网络行为；已发出的请求不能通过页面隐藏撤回。今日口径继续为UTC。没有生产读写、部署或发布。
- 状态：O02本地候选修复与独立复核完成；按清单顺序F06全文展开随后完成，汇总见下节。

## F06 团队详情全文展开

- 修复前复现：只读组件探针以170字符消息和末尾证据标记确认窗口只显示前160字符加省略号，尾部标记不在界面内，且没有展开入口；证据见 `evidence/frontend-readonly-repros/team-window.test.ts` 及该目录三轮复现记录。期望行为测试在旧实现上先失败，确认缺少全文展开控件。该现象仅限团队窗口展示，不证明模型或服务端截断。
- 修改范围：`AgentTeamWindow.vue` 为长消息提供独立展开/收起和“复制完整内容”；以Unicode码点裁剪预览，完整消息保留原格式并在窗口内滚动。JSON/对象按完整JSON渲染；错误证据支持滚动查看。复制API失败/不可用时自动展开完整内容供手动复制。展开状态按消息隔离，团队/会话切换清理；异步复制回调绑定目标团队、会话、账号和token，旧目标的迟到成功/失败不得污染新目标（包括复用message_id）。移动样式限制窗口和内容高度，并保留换行与长词折行。
- 修复后测试：全文组件、目标冻结与时间回归本次复跑3个文件共26 passed；测试覆盖原170字符末尾标记、长中文、JSON尾键、逐消息折叠、复制成功/失败、切换目标、复用ID的剪贴板竞态和长错误详情。独立复核在精确样本新增前运行25项，并额外只读走查异步函数7个分支：无Clipboard API、当前拒绝，以及团队/会话/账号/token改变或窗口隐藏后的迟到结果；未发现阻断。F01-F06整合前端8文件183 passed；目标ESLint、`vue-tsc && vite build`、`git diff --check`通过。详细记录 `evidence/复测-F06-全文展开.log`。
- 证据边界：手机适配由CSS源码断言与jsdom组件状态覆盖，不是真实375px浏览器/真机视觉验收；Clipboard API是mock，没有实测浏览器权限弹窗；没有测试真实模型长上下文或服务端输出/持久化截断。本次仅证明前端已收到的长内容能够完整展示/复制。没有访问生产、部署或发布。
- 状态：按检查顺序14项中的本地候选代码与组件验收已完成；外部环境未闭环项继续列于TODO（尤其MySQL锁/迁移、真实手机/生产入口和供应商长上下文验证）。

## R1-01 全局LLM配置意图与小菱会话审批闭环（2026-10-05）

- 生产来源只读追溯（2026-10-04 18:40 UTC）：审批记录的 `agent_code=manager`，归属最高管理员账号 `user_id=1` 的 `admin` 管理端会话；run 于 2026-09-11 19:22:55 UTC 创建，审批于 19:24:07 UTC 创建。对话中保存的用户消息是“模型名请使用 deepseek-flash。旧模型名 deepseek-v4-flash、deepseek-v4-flash-vision-exp 仍可调用，但对应模型已下线，请求将由 DeepSeek-V4.1-Flash 模型提供服务，并按 Flash 价格计费。”随后 Agent 成功读取能力目录、当前配置和模型注册表，再请求 `llm.config.update(model=deepseek-flash)`。这些记录解释了 Agent 的触发链，但用户本人否认发送该消息；账号会话不证明自然人身份，附近审计日志无记录，不能归因具体设备/操作者，也不能确定原意是任务模型还是平台全局默认。审批仍为 `pending/critical`，run 仍为 `waiting_approval`，无批准人、无对应工具调用日志，执行账本没有全局配置更新。该调查只读，未批准、驳回或修改配置。
- 修复前复现：隔离 API 测试构造一个与已持久化 `AgentResponseRun` 绑定的 Responses 审批，向通用审批接口提交批准，旧实现返回200并把审批标为 `approved`，关联 run 仍为 `waiting_approval`，造成终态分裂。期望断言在修复前失败。原审批行 #234 没有被用于回归或改写。
- 修改范围：小菱管理员提示与运维知识明确区分“当前任务选用模型”和“修改平台共享/默认模型配置”，仅有后者的明确请求才允许调用 `llm.config.update`；意图不清时先向用户确认。后端通用审批决策拒绝对仍关联持久化 Responses run 的待处理事项单独批准或驳回，要求回到原小菱会话续跑；审批列表批量标记此类事项，避免逐行查询，界面显示“返回小菱对话处理”。同时检查 `request_json.run_id` 与 `resource=response_run:<id>` 两处关联，任一方命中持久 run 即阻断，兼容两个字段不一致的损坏/旧记录。
- 修复后复测：本地 API 对批准、驳回及 payload/resource 双向不一致、两边均指向关联 run 的8个组合均返回400，审批保持 `pending`、run 保持 `waiting_approval`；旧式无持久 run 的审批仍可通过（200）。小菱会话审批前端 handler 不再弹确认、不调用通用决策API。最新完整后端结果见该小节对应证据；完整前端137文件/1706测试、Ruff、ESLint、`vue-tsc`、Vite构建通过。
- 边界：提示词只能降低模型误判，不能证明模型绝不产生错误工具请求；真正的高风险配置执行仍需审批，session-bound审批必须回到原会话。生产 #234 的模型配置没有批准/驳回或改动；本次未用真实模型请求验证提示遵循率。生产发布后只复核部署版本与审批状态，不用真实 #234 做批准/驳回试验。
- 状态：候选代码修复和隔离回归通过；生产发布及发布后只读核验完成前，不标记为已上线。
