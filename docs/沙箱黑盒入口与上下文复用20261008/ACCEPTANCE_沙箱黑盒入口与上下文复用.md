# 沙箱黑盒入口与上下文复用验收

## 当前状态（2026-10-09）

- 生产已运行 v4.0.97 / SHA `c1243cf70b891fee18cb883b3b3b64523ed4679a`。正式 `deploy.sh all --revision c1243cf70b891fee18cb883b3b3b64523ed4679a` 于 `2026-10-08T17:43:04Z` 退出码 0；HTTPS `/healthz`、`/readyz`、运行镜像标签、后端 `APP_RELEASE`、前端活动构建信息和发布账本均一致。发布包大小 `107,153,532` bytes，SHA-256 `da080d2e04f6da1e21b5b26ba37c73a855b003fd1a427a0145f1f55f5fb91a23`。
- 发布前门禁曾因根盘可用空间低于 12 GiB 而暂停。用户批准的清理范围是删除保留集以外的旧 Prism 发布镜像标签；按此范围移除了 v4.0.92 前后端标签。`cleanup.sh --cache-until 168h` 另报告 BuildKit 缓存回收 0B。之后还单独执行 `docker builder prune --filter until=6h`，回收 1.424 GB 并使正式容量预检通过；此命令比 168 小时预览更宽，会清理较新的缓存，不能视为原批准范围内操作。部署后 `ops-check` 为 `can_continue=true`、无阻断项，但磁盘 94% 为 degraded；最后可用 `12,078,252 KiB`（约 11.52 GiB），低于下一次部署的 12 GiB 门槛约 493 MiB。部署后又按保留策略移除了 v4.0.93 前后端镜像标签，没有扩大到日志、备份、卷或旧发布树清理。
- 备份 gzip/SHA/元数据核验通过；隔离恢复读取 104 张表且 Alembic head 一致，临时恢复 schema 已清理。`cr_testdb` 仍运行，挂载卷仍在使用且未删除。Alembic 当前为 `062_audit_log_action_length (head)`。Backend、Frontend、MySQL、Redis、ClamAV 与 decoy 健康。
- 部署时真实前端 `vue-tsc` 与 Vite 构建通过（3,742 modules，约 1m10s）；生产入口 `/assets/index-DiQRwzjS.js` 引用的 buildInfo 资产 `/assets/buildInfo-BZVwvzJD.js` 通过 HTTPS 直接核验为 v4.0.97 / SHA `c1243cf70b891fee18cb883b3b3b64523ed4679a`，构建时间 `2026-10-08T17:34:36Z`。同源 HTTP→HTTPS 返回 308，HTTPS 健康/就绪检查通过。尝试使用已打开浏览器做页面真实点击时，Cua 状态读取两次超时，因此本轮没有完成登录后的视觉或角色业务流验收，不把 HTTP 冒烟当作浏览器全流程验收。
- v4.0.96 发布后通过普通 Chrome 管理员会话真实点击，在 bWAPP/PHP 隔离沙箱重复执行两次黑盒和一次白盒。三份任务结果回执均记录 worker exit 0、`cleanup_confirmed=true`；其清理范围字段称容器和任务目录已移除（本轮未另行执行主机侧容器检查）。两次 `/login.php` route smoke 均 HTTP 200。三条任务的 AI 上下文阶段均被模型服务 HTTP 402 (`billing_error`) 拒绝，动态用例 0 / skipped，最终均为 `verification_status=partial`。生产界面三条任务的进度事件均显示“多 Agent 测试审查报告已生成（角色 0/4）”；下载的第一条任务 Markdown 报告记录确定性黑盒检查通过、AI 动态补充未执行、模型增强因 `billing_error` 停止，但未记录角色计数，故角色数的证据来源仅为当时界面事件。确定性部分通过不代表 AI 黑白盒通过。逐任务证据见 `证据/v4096-production-sandbox-retests-20261009.json`。
- HTTP 402 只能确认本次模型服务计费/额度请求被拒，不能仅凭此认定账户余额为零。需要模型服务管理员检查供应商计费/额度后，再真实复测 AI 动态黑白盒；本轮未更改供应商账户或凭据。
- 磁盘只读诊断（`du -x`）：`/opt/code-review/backups` 约 46 GB、`/opt/prism-releases` 约 36 GB、`/var/lib/docker` 约 49 GB、`/var/log/journal` 约 3.9 GB、`/tmp` 约 4.6 GB。当前 94% 磁盘告警的主要占用位于备份、发布树、Docker 数据、系统日志和历史 bundle；除已批准的两个旧镜像标签外，本轮未删除这些内容。

## v4.0.98 角色数据范围修复候选与生产角色抽样（2026-10-09）

### 生产复现与根因

- 生产当前仍为 v4.0.97 / SHA `c1243cf70b891fee18cb883b3b3b64523ed4679a`；Safari 普通窗口页脚显示 v4.0.97。管理员真实点击角色管理中的审查员“数据范围”后，页面出现“参数校验失败”和“数据范围加载失败”，并关闭对话框。
- 对照该 release 源码：`RoleManage.vue` 在加载项目选项时请求 `GET /projects?page=1&page_size=200`；`backend/app/api/v1/projects.py` 的 `page_size` 契约为 `Query(20, ge=1, le=100)`。两者不兼容，足以触发 FastAPI 参数校验拒绝；浏览器网络面板未保留，因此不把具体 HTTP 状态码当作已抓取证据。
- 候选修复按 API 上限每页读取 100 条，完整遍历所有页并验证分页元数据、项目 ID 与总条数；任何分页不完整都会阻止显示部分项目。角色范围默认值、权限和保存路径不变。修复建立在已部署 v4.0.97 提交上，拟发布版本为 v4.0.98。

### 角色与账号真实点击抽样

- 通过 Safari 普通窗口分别登录管理员、审查员、普通用户；未使用无痕窗口，也未创建、修改或删除业务数据。
- 管理员角色列表显示固定角色 `user`、`reviewer`、`admin`、`super_admin`，无单独审计员角色；权限点目录显示 61 项。审查员授权中 `review:start`、`review:view`、`audit:view` 已勾选，用户/角色管理与 `server_ops` 权限未勾选。
- 审查员和普通用户分别直接访问 `/admin/access`，均被应用重定向至 `/403` 并显示“访问被拒绝”；未见 404。审查员侧栏有“操作审计”，普通用户侧栏没有。两角色仪表盘本次分别显示 1 个和 12 个可见项目；这是两账号当时界面的抽样，未据此宣称全站数据隔离已穷举证明。
- 两角色的小菱“我的对话”列表分别显示各自的会话标题，当前抽样没有看见对方标题。未打开历史正文、未发送新消息；这只能证明本次列表抽样未见串号，不能覆盖全部历史和 API 路径。

### 候选验证与生产门禁

- 修复按项目 API 上限每页请求 100 条，并继续读取后续页，直到收到完整且稳定的总数；响应页码/页大小/总页数不匹配、重复项目或项目数不完整时拒绝打开部分数据，保留已保存范围和权限逻辑不变。
- 红绿证据：v4.0.97 基线仍请求 200 条时，最初的定向回归为 `1 failed, 5 passed`；将单页请求改为 100 后为 `6 passed`。随后补齐跨页与分页中断样本，最终 `RoleManage.test.ts` 为 `10 passed`，覆盖第 101 个项目可选和分页不完整时关闭对话框并报错。
- 最终候选前端 `vue-tsc` 与 Vite 生产构建通过，转换 3,742 个模块；前端全量测试 `142` 个文件 / `1,857` 项通过；变更文件 ESLint 通过。没有后端源代码变更，未重跑后端全量测试。
- 生产只读核验（2026-10-08T18:31:51Z 至 18:32:15Z）：`/healthz`、`/readyz` 均为 v4.0.97 / SHA `c1243cf70b891fee18cb883b3b3b64523ed4679a`。主机根盘已达 95%，可用 `11,195,920,384` bytes（约 10.43 GiB）；正式发布要求至少 `12,884,901,888` bytes（12 GiB），短 `1,688,981,504` bytes（约 1.57 GiB）。活动 release `ops-check` 已为 `status=error`、`can_continue=false`、`blocking_checks=[disk]`；磁盘达到 95% 临界值，发布与写入禁止。
- 依用户此前批准的旧镜像标签范围重新预览 `cleanup.sh --keep-release-images 2 --keep-release-states 10 --cache-until 168h`：当前与上一发布、最近两版均受保护，本次无可删除的旧 Prism 发布标签；此轮未执行清理。UTC 18:31:53 主机快照显示根盘仅余 `11,195,920,384` bytes，距 12 GiB 发布门槛差 `1,688,981,504` bytes，磁盘已达 95%；UTC 18:32:15 release `ops-check` 为 `can_continue=false` 并阻断 `disk`。v4.0.98 尚未打包或部署；不得降低门槛或越过已授权的旧镜像标签清理范围。
- 根盘短时增长的主要来源已定位：UTC 18:08:56 至 18:31:53 已用空间增加 `900,755,456` bytes；`prism-backup.timer` 于 18:19:22 触发计划备份，活动 `prism-backup.service` 执行 `backup.sh --reason scheduled` 并以状态 0 结束。窗口中新建的 `/opt/code-review/backups/code_review_20261008T181923Z_c1243cf70b89.sql.gz` 占用 `822,181,888` allocated bytes（文件逻辑大小 `822,175,988` bytes），约为该增长的 91.28%；其余 `78,573,568` bytes 暂不能精确归因。UTC 18:35:23–18:35:43 的 20 秒样本只增加 `40,960` bytes。数据库文件只看到 mtime 在窗口内，缺少窗口起点大小，不据此归因；没有读取备份内容或删除任何文件。备份自动保留配置仍待只读核对。
- DeepSeek 开放平台本轮未发起模型请求或付款。此前生产 PHP 黑白盒的 402 结果仍需在供应商计费/额度恢复后重测；本轮不把确定性 runner 的成功等同于 AI 黑白盒通过。
- 字段化浏览器和主机证据见 `证据/v4098-role-scope-and-production-acceptance-20261009.json`。

## v4.0.96 候选本地验证（2026-10-08）

- 后端完整 `tests/unit`：`4606 passed, 6 skipped`，243.27 秒；6 项依赖本机 Redis，未运行。测试报告 944 条告警，主要为既有弃用与 pytest 收集告警。
- 改动 Python 文件 Ruff、`compileall`、`git diff --check` 通过。全仓 Ruff 还报告 63 条未改文件 lint 问题，因此不把全仓 Ruff 标为通过。
- 独立候选审查补充定向路径 `475 passed`，并验证用量记录并发样本；与全量单元套件属于候选代码证据，不是生产模型调用证据。
- 本段为发布前候选验证记录：当时磁盘门禁未通过，尚未部署。获批旧镜像标签清理和 168 小时 BuildKit prune（0B）未释放足够空间；随后另行执行了范围更宽的 `until=6h` 缓存清理，再通过容量门禁并发布。各动作的范围与状态见本文件“当前状态”。
- 候选本地验证不等于生产 AI 动态审查成功；发布后真实生产样本均保持 `partial`，原因和覆盖边界详见本文件末尾。

## 历史验收范围（v4.0.94 阶段）

- 当时生产：v4.0.94 / SHA `3ccd1c3d783b3c0bed24fe5bf4016beb72194beb`。
- 当时候选：v4.0.95（精确来源引文提示；本地测试已完成，等待生产发布与原场景复测）。
- 生产复现任务：bWAPP / PHP 黑盒 `sbx_f55815302ddf42859d1c4291`，记录保留。
- v4.0.90 修复后真实复测：bWAPP / PHP 黑盒 `sbx_f5aff86c030a4cceb61d516d`，记录保留。
- v4.0.91 再复测：bWAPP / PHP 黑盒 `sbx_b0e1448463804dcea6a696f3`，记录保留。
- v4.0.92 发布后真实复测：bWAPP / PHP 黑盒 `sbx_eb4ec5deea23402bbdd0fd8f`，记录保留；确定性路由通过，AI 压缩失败，只判部分通过。
- 用户要求生产黑盒、白盒真实复测；远程目标留空，使用隔离 Worker。

## 修复前实际生产证据

- v4.0.89 的生产任务包含 259 个文件、481 个源码片段。PHP 入口探测命中 `/login.php`，来源为 `php_entrypoint`，HTTP 200，确定性路由测试通过。
- 同一任务的 AI 动态用例生成因完整源码上下文压缩超过共享 300 秒预算而跳过，生成数为 0；最终为“部分通过”，不能认定 AI 动态审查通过。
- Safari 进度时间线显示已进入“回收环境”，随后报告“黑盒测试完成”；本次尚未从下载的 JSON 回执独立确认 `cleanup_confirmed` 字段，因此将机器级回收回执留作未核验项。
- 证据来自生产 UI 的任务结果与进度事件；它证明该任务出现了超时，不能据此推断所有项目或所有运行时都失败。

## v4.0.90 生产复测发现的具体原因

- 任务 `sbx_f5aff86c030a4cceb61d516d` 在 481 个源码片段、259 个文件上发起 138 次源码压缩调用，压缩阶段耗时 `300,213 ms` 后因共享预算耗尽中止。
- 同用户、同 Agent 标签与该任务时间窗的用量账本显示：55 次模型响应 `finish_reason=length`，4 次请求读超时，78 次成功，另 1 次响应未返回最终内容。分析成功响应结构时，362 条引文中 358 条是对应源码的精确子串，其中 19 条超过旧 80 字校验上限（最长 128 字）。
- 最终 fail-closed 原因是模型输出多次超限并递归拆分后，来源引文校验仍无法满足；压缩没有被伪装成通过。确定性 PHP 黑盒部分独立通过：`/login.php`，`route_origin=php_entrypoint`，HTTP 200；AI 动态用例数为 0，故整体仅部分通过。
- Worker 结果回执明确 `cleanup_confirmed=true`，容器状态为 `exited`，清理范围为容器及任务目录；该证据已从生产数据库结果字段核验。任务最终 worker 状态为 `succeeded`，不等于 AI 动态覆盖成功。

## 候选实现与本地验证

- 同一归并层的独立压缩批次最多 4 路并发；进程内信号量限制并行模型请求，等待配额计入共享 deadline，递归层仍按层推进。
- 每个批次使用独立 `contextvars.copy_context()` 在线程内执行，保留 AI 用量日志的 ContextVar 归因和 Engine-backed sessionmaker；不能只证明摘要完成而漏记模型调用/token。
- 保留摘要归并顺序、源码 SHA/来源 ID/覆盖校验、逐来源原文引文、受保护事实、每次压缩 512 次调用上限和 fail-closed 语义。
- 后端全量（v4.0.90）：`6602 passed, 6 skipped, 1196 warnings`，445.40 秒。6 项跳过为需要可用 Redis 的现有用例。
- v4.0.91 代码变更：单批最多 8 个来源/40,000 字符；模型摘要目标不超过 600 字；引文仍须逐字出现在对应原文，硬上限改为 160 字以容纳生产观测到的 128 字精确引文。来源 ID、SHA、受保护事实和 fail-closed 校验均保留。
- 修复后定向回归：320 passed，覆盖 481 片分层压缩、引文校验、PHP 沙箱 Agent 和 AI 用量归因。
- 后端全量（v4.0.91 候选）：`6604 passed, 6 skipped, 1196 warnings`，291.40 秒。
- Ruff 与 `git diff --check` 通过。`bash deploy/tests/test_scripts.sh` 通过：发布绑定 33/33、容量门禁、备份/恢复、回滚与故障注入矩阵通过。

## v4.0.91 生产复测：新的直接原因

- 发布核验：生产 `/healthz`、`/readyz` 与 Safari 页面页脚显示 v4.0.91 / SHA `16fe30d0c279c569f20ff71091494a62a48167c2`；Backend、Frontend、Decoy、MySQL、Redis 与 ClamAV 健康。
- 任务 `sbx_b0e1448463804dcea6a696f3` 使用 bWAPP/PHP 黑盒，项目 157，源码 SHA `9fbf996afdacc7c6cb9871ecce0359ace6de0aceaa1eb9fd97ca82a2c4738409`，覆盖 259 个文件、481 个分片。远程目标为空，Worker 为生产隔离 profile。
- 任务结果：`/login.php`（`php_entrypoint`）HTTP 200；确定性黑盒 runner `exit_code=0`，容器与任务目录回收回执 `cleanup_confirmed=true`。AI 动态补充状态为 `skipped`、数量 0；结果摘要明确写“确定性黑盒测试通过；AI动态补充未执行”。因此只能判定确定性黑盒路径通过，不能判定 AI 动态黑盒审查通过。
- 沙箱压缩事件本身记载 `model_calls=153`、`elapsed_ms=300050`。同用户、Agent 标签和任务时间窗的调用账本也有 153 条记录：prompt 998,963 tokens、completion 270,617 tokens，合计 1,269,580 tokens；状态为 98 次成功、51 次 `finish_reason=length`、4 次读超时；截断记录里 47 次耗尽 2,048 token、2 次耗尽 4,096 token。AI 日志行的 `task_id`/`file_id` 均为空，因此 token 与失败分类是按用户、Agent 标签和时间窗关联的聚合值，不是逐行外键连接；沙箱事件独立确认的调用数和压缩耗时仍与该窗口吻合。
- 直接原因不是来源覆盖校验放宽不足：v4.0.91 将 2,048 作为首轮预算，截断后才升至 4,096；生产数据表明 2,048 的首轮失败大量制造重试延迟，而 4,096 首轮几乎都能完成。总压缩仍在 300 秒期限前无法覆盖全部源码，系统正确拒绝把不完整摘要交给动态测试 Agent。
- v4.0.92 修复为首轮直接使用 `min(4096, configured_output_tokens)`；保持来源 ID、SHA、逐来源引文、受保护事实、递归无损拆分、512 次调用上限、共享期限和 fail-closed 语义不变。新增回归断言：配置允许时首轮即请求 4,096，不先发已被生产证实易截断的 2,048 请求。
- 本地验证（v4.0.92 候选）：源码上下文定向测试 `25 passed`；后端 `backend/tests/unit` 为 `4539 passed, 6 skipped, 927 warnings`；Ruff 与 `git diff --check` 通过；`bash deploy/tests/test_scripts.sh` 发布绑定 `33/33` 且部署/回滚故障注入矩阵通过；前端 `npm --prefix frontend run build` 通过（3742 modules）。
- v4.0.92 bundle：`prism-v4.0.92-0bda311b.bundle`，106,729,969 bytes，SHA-256 `05fc8b56bb61fec6350a367d94bf4d0be7edca6e9c101700081e68b7afcec226`。
- v4.0.92 已于 `2026-10-08T08:28:54Z` UTC 发布；发布账本、运行镜像和健康状态核验通过。此时发布成功不代表 AI 动态黑盒覆盖成功，后续真实任务 `sbx_eb4ec5deea23402bbdd0fd8f` 仍复现了模型输出截断。
- 当前主机只读容量复核：根盘可用 `12,442,775,552` bytes（94%），低于默认发布门禁 12 GiB；部署候选前必须通过受控、可再生构建缓存清理满足容量门槛。不得删除历史任务、数据库、备份或回滚镜像来绕过门禁。

## v4.0.92 生产失败回执与根因

- 在普通 Chrome 的既有管理员登录会话中通过真实点击读取 `sbx_eb4ec5deea23402bbdd0fd8f` 结果；没有使用无痕窗口。任务参数为 bWAPP/PHP 黑盒、远程目标留空，生产隔离 Worker。
- 任务结果中的证据字段确认：`verification_status=partial`；`/login.php` 来源 `php_entrypoint`、HTTP 200；确定性执行 `exit_code=0`；AI 动态用例状态 `skipped`、数量 0；Worker 回执 `cleanup_confirmed=true`，容器已退出。该生产记录未删除。
- 本地保全的结果 JSON：`证据/v4092-sbx_eb4ec5deea23402bbdd0fd8f-result.json`，SHA-256 `a6d1fc716ae2cf49c66a3146612af70ff58abd1b4737679d55bdf5d14b160806`。
- 根因闭环：生产事件记载 131 次压缩调用、300,254 ms 后因模型输出长度上限失败；调用路径 `_context_call()` 没有传 `thinking`，而 `BaseAgent.call()` 对 `None` 不写请求体字段，DeepSeek 官方文档说明思考模式默认启用且隐藏推理 token 计入 `max_tokens`。因此这段仅需输出受校验 JSON 的结构化压缩把 4,096 输出预算让给隐藏推理，截断后分片重试又耗尽共享期限。依据见上方官方链接。

## v4.0.93 候选本地验证

- 先红后绿：新增断言要求 4,096 token 首轮压缩请求显式 `thinking=False`；修复前测试实际观察到 `None` 并失败，修复后通过。
- 压缩/沙箱定向套件：`349 passed`；后端全量 `backend/tests/unit`：`4539 passed, 6 skipped, 927 warnings`，245.94 秒。6 项因本机 Redis 未启用跳过。
- 前端 ESLint 通过；Vitest `142` 个文件、`1853` 项通过；生产构建通过，输出 3,742 个模块。
- 部署脚本验证：`bash deploy/tests/test_scripts.sh` 通过，release binding `33/33`，备份/恢复和部署故障注入矩阵均通过；`ruff check` 与 `git diff --check` 通过。
- 上述为本地候选证据，不代表已生产发布或已证明模型动态测试成功。生产容量门禁、发布账本、健康状态、两次黑盒与一次白盒结果须在下文逐项补录。

## 历史生产发布与复测记录（v4.0.90—v4.0.95）

v4.0.90 部署前只读核对：生产 `/healthz` 与 `/readyz` 均为 v4.0.89 / SHA `0c017fb700287bc45167c25eef2f210b64530e1c`；当时 Worker active，活动 release 路径与该版本一致。v4.0.90 发布后 Worker 仍从 v4.0.89 release 路径启动，但执行器源码 SHA 与 v4.0.90 候选源码 SHA 一致，profile pin 也一致；路径版本未对齐需明确保留这一边界。v4.0.91 部署后生产健康核验通过，但实际黑盒复测因压缩耗尽 300 秒而未执行 AI 动态用例。v4.0.93 已于 2026-10-08 发布；本轮候选为 v4.0.94，发布前必须刷新容量、备份/恢复和当前运行 SHA 门禁。

| 样本 | 版本 | 模式 | 任务 ID | 终态 | 关键结果 | 清理回执 |
|---|---|---|---|---|---|---|
| 修复前 bWAPP | v4.0.89 | PHP 黑盒 | `sbx_f55815302ddf42859d1c4291` | 部分通过 | `/login.php` 200；压缩超 300 秒，AI 动态用例跳过 | UI 显示完成回收；JSON 字段待核 |
| 修复后 bWAPP A | v4.0.90 | PHP 黑盒 | `sbx_f5aff86c030a4cceb61d516d` | 部分通过 | 481 片/259 文件；压缩 138 次调用后 300,213 ms 超时；55 截断、4 读超时；`/login.php` 200；动态用例 0 | 已核实 `cleanup_confirmed=true`，容器与任务目录清理 |
| v4.0.91 复测 | v4.0.91 | PHP 黑盒 | `sbx_b0e1448463804dcea6a696f3` | 确定性部分通过 | `/login.php` 200，runner exit 0；481 片压缩 153 次调用后 300,050 ms 超时；AI 动态用例 0 | 已核实 `cleanup_confirmed=true`、exit 0 |
| v4.0.92 样本 A | v4.0.92 | PHP 黑盒 | `sbx_eb4ec5deea23402bbdd0fd8f` | 部分通过 | 481 片；压缩 131 次模型调用、300,254 ms 后 `finish_reason=length`；`/login.php` 200；动态用例 0 | 待从结果回执独立复核 |
| v4.0.93 样本 A | v4.0.93 | PHP 黑盒 | `sbx_f207b9e313f5499cb0fd3ca1` | 部分通过 | 项目 157，归档 259 文件、来源 481 片；压缩 178 次/153,241 ms 后摘要校验失败，AI 动态用例 0；`/login.php` HTTP 200 | `cleanup_confirmed=true`，容器与任务目录清理 |
| v4.0.94 样本 A | v4.0.94 | PHP 黑盒 | `sbx_e32876716e62433cb3eeb8fc` | 部分通过 | `/login.php` 来源 `php_entrypoint` HTTP 200，确定性 runner exit 0；来源引文无法从对应来源原文核验，AI 动态生成 skipped/0，整体 `verification_status=partial` | `cleanup_confirmed=true`，容器与任务目录回收 |
| v4.0.95 样本 A | v4.0.95 | PHP 黑盒 | `sbx_9b4a64d5566b4f4184740e91` | 部分通过 | `/login.php` 200；确定性 runner 通过；AI 动态用例 0 / skipped | 已核实 cleanup |
| v4.0.95 样本 B | v4.0.95 | PHP 黑盒重复 | `sbx_35ef276ad6f340afb7906bf9` | 部分通过 | `/login.php` 200；确定性 runner 通过；AI 动态用例 0 / skipped | 已核实 cleanup |
| v4.0.95 样本 C | v4.0.95 | PHP 白盒 | `sbx_8eb995ad8a0d4b09b2fd0e0b` | 部分通过 | 确定性 runner 通过；AI 动态用例 0 / skipped | 已核实 cleanup |

历史记录中的 v4.0.94 已部署且生产健康。v4.0.95 随后发布，但后续三项生产样本的 AI 动态覆盖均未生成；不得声明黑白盒全通过。真实任务记录按用户要求保留。

## 未验证边界

- 481 片真实生产项目样本不等于实测 100 万 token 的外部模型理解能力；生产配置显示的上下文窗口值也不等于该能力证明。
- 6 项 Redis 用例在本轮因本地环境条件跳过；另未据此声明所有业务路径、所有语言运行时或所有浏览器通过。
- Java Runner 镜像此前未能更新，版本仍为旧 digest；本次 PHP 样本不能证明 Java 环境已恢复。
- 100 万 token 的模型理解能力没有真实生产证明；481 个源码片段样本不是等价证明。
- v4.0.96 已发布，备份/隔离恢复和本轮三项 Worker 清理回执已核验；AI 动态黑白盒仍因 HTTP 402 未完成。百万 token 外部模型理解能力、Java Runner、其他语言运行时及非 PHP 项目均未由本轮证明。


## v4.0.93 生产复测失败与 v4.0.94 修复

- 普通 Chrome 管理员会话真实提交任务 `sbx_f207b9e313f5499cb0fd3ca1`；bWAPP / PHP 黑盒、项目 157、归档文件数 259、源码上下文分片 481，源码 SHA `5a243f9c38038a3afa1fa3e17170519aa69a8e481bf9970354804d2af832df5e`。远程目标为空，使用隔离 Worker。任务记录保留。
- 生产沙箱事件与环境表关联一致：`model_calls=178`、`elapsed_ms=153241`；完整结果 JSON 记录 `verification_status=partial`、AI 动态生成 `skipped`/0、确定性黑盒 `/login.php` HTTP 200、runner exit 0、`cleanup_confirmed=true`，容器已退出。本地保全的字段化验收回执 `证据/v4093-sbx_f207b9e313f5499cb0fd3ca1-result-summary.json`，SHA-256 `dbb3681459e156e423ea91f672af4521c5b1a35048330e8a1e0278132032b5c4`；原始下载件 SHA-256 `b8ec7da24c62917c0cfb450a6d85e5475965ea82c33f193895ea9f7a45ad5010`，包含 worker 日志，不复制到验收文档。
- 管理员 `deployment_coordinator` 在该压缩窗口的 178 条日志与沙箱事件调用数一致；177 条为可解析 JSON，44 条摘要长度超过旧 1,200 字硬上限（最长 2,075），另有 29 条逐来源引文不符合精确子串/长度校验。引文失败仍必须触发重试或无损拆分，不能放宽。
- v4.0.94 将每个摘要的有限硬上限调为 2,400 字，并把提示目标调整为精简、优先入口/调用/鉴权/数据流/异常。上限覆盖已观测 2,075 字响应并保留边界余量；超过 2,400 字仍 fail-closed，来源 ID/顺序、逐来源精确引文、SHA、受保护事实、最终 `max_chars` 与 512 次调用限制均保留。分层归并下一层继续压缩这些有限摘要。
- 先红后绿：2,075 字合法摘要在旧阈值下复现失败；修复后 481 源片模拟分层归并 6 次调用完成。另模拟宽批次坏引文，坚持精确校验并在 24 次以内拆分收敛；2,401 字摘要仍拒绝。源码上下文与 C13 定向套件 `36 passed`。
- v4.0.94 候选后端单元全量 `4543 passed, 6 skipped`；前端 ESLint 通过，Vitest `142 files / 1853 tests passed`，Vite 构建 3,742 modules；Ruff、`git diff --check` 和发布脚本 `33/33` / 备份恢复与部署回滚故障注入通过。
- 生产 AI 调用日志以用户/Agent/时间窗聚合，日志外键 `task_id`/`file_id` 为空；沙箱事件的 178 次调用与任务精确关联，token、坏引文与摘要长度统计仅为同窗口账本聚合，不宣称逐条外键归因。

## v4.0.94 发布后的真实失败复现与 v4.0.95 修复

- v4.0.94 已于 `2026-10-08T09:54:55Z` 构建并部署，生产健康接口与普通 Chrome 页脚显示版本 `4.0.94`、SHA `3ccd1c3d783b3c0bed24fe5bf4016beb72194beb`。部署前备份、隔离恢复、迁移 `062_audit_log_action_length`、HTTPS 与健康检查均通过。
- 真实任务 `sbx_e32876716e62433cb3eeb8fc` 使用 bWAPP/PHP 黑盒、项目 14、源码 SHA `fad1234a7c235e37e941f1fc2d68b39549fcb662003ed8c5547306b805b1729d`。确定性 `/login.php` 路由为 HTTP 200，runner exit 0；AI 动态用例生成 `skipped`、数量 0，覆盖状态 `partial`。Worker `exit_code=0`、`cleanup_confirmed=true`，容器 `exited`，任务记录保留。
- 结果下载件 6,383 bytes，SHA-256 `44a424f942e53d0ff65a6f01e0fb166e397cff8e0b8c467b806ac1445b8d4c54`；字段化摘要保存在 `证据/v4094-sbx_e32876716e62433cb3eeb8fc-result-summary.json`，不含 Worker 原始日志。
- 失败原因：源码上下文完整性核验因“源码分片引文无法从对应来源原文核验”未通过。此前 v4.0.94 已消除本轮输出长度失败，但模型没有稳定逐字回显来源引文，压缩器保持 fail-closed，未把确定性 route smoke 伪装为 AI 动态测试成功。
- v4.0.95 在每条来源材料增加程序抽取的 `source_quote_hint`，要求模型原样回显；这是提示改进，不是校验放宽。校验仍使用原始来源文本；源码上下文两文件定向测试 `37 passed`，后端全量 `4544 passed, 6 skipped, 927 warnings`，Ruff 与 `git diff --check` 通过；前端 Vite 构建 3,742 modules，部署脚本 release binding `33/33` 与备份/恢复、回滚故障注入通过。
- 生产巡检来源核对：系统定时服务的 `WorkingDirectory` 指向活动 v4.0.94 发布目录；最近一轮 `ops-check` 为 `degraded/can_continue=true`，release、容器、备份、迁移和 HTTPS 均通过，唯一降级为磁盘 94%。曾手动运行 `/opt/code-review/deploy/ops-check.sh` 得到 v4.0.72 旧账本不一致；该目录已非活动部署，不代表运行容器偏差。
- 发布空间曾约 10.9 GiB，低于默认 12 GiB 门槛。用户批准后执行活动发布脚本的受控清理，保留当前、上一及最近两版镜像和全部回滚状态，不动数据库卷、备份或测试任务。清理后可用空间 14,804,934,656 bytes（约 13.79 GiB），磁盘 93%；能否发布仍须以部署脚本即时容量预检与备份验证结果为准。

## v4.0.95 真实业务复测及 v4.0.96 候选

| 生产任务 | 模式 | 确认结果 | 结论 |
|---|---|---|---|
| `sbx_9b4a64d5566b4f4184740e91` | bWAPP / PHP 黑盒 | `/login.php` HTTP 200；确定性 runner exit 0；Worker cleanup 确认；AI 动态用例 0 / skipped | 部分通过，不是 AI 黑盒通过 |
| `sbx_8eb995ad8a0d4b09b2fd0e0b` | bWAPP / PHP 白盒 | 确定性 runner exit 0；Worker cleanup 确认；AI 动态用例 0 / skipped | 部分通过，不是 AI 白盒通过 |
| `sbx_35ef276ad6f340afb7906bf9` | bWAPP / PHP 黑盒重复 | `/login.php` HTTP 200；确定性 runner exit 0；Worker cleanup 确认；AI 动态用例 0 / skipped | 部分通过；与首个样本一致 |
| `sbx_40a277fe7fe242648f5f0082` | 额外无入口样本（语言待核） | 无 Web 入口候选；路由状态 0；runner exit 1；cleanup 确认 | 不适合作为 bWAPP 黑盒验收样本，单独记录失败；下载 JSON 未提供语言/runtime |

在 v4.0.95 历史样本中，曾有一条未保存 event ID 与逐任务关联的 HTTP 402 观察；不把该旧观察倒推到那些任务。v4.0.96 的三条新生产 JSON 则分别明确记录 `billing_error`，以各自结果字段判定。两阶段都不能仅凭 HTTP 402 推断余额归零。

候选 v4.0.96 对 402 执行不可重试分类、压缩并发队列快速停止、测试生成内外层停止重试，并增加 Python 标准库 `from-import` 成员真实导入校验。复核继续发现 PHP 并行语法修复会在账务失败后启动排队调用，Agent Mesh 会将文件读取账务错误重新排队，报告阶段账务异常也不会保存确定性报告；已增加语法修复任务级停止、Mesh 失败类型保留/死信和报告确定性回退。早先 5 个相关测试文件 `332 passed` 及 `tests/unit` `4565 passed, 6 skipped` 均发生在这三处改动之前；最终新增针对测试与全量回归正在进行。此前候选的前端及部署脚本通过记录不替代发布后验收。

以上为 v4.0.96 发布前的历史阻断快照：当时容量门禁为 `can_continue=false`，根盘可用 `10,814,038,016` bytes。后续经授权的缓存回收后正式部署并完成备份/隔离恢复；当前生产状态以本文件开头和末尾的“v4.0.96 生产发布与真实复测”记录为准。


## v4.0.96 生产发布与真实复测（2026-10-08—09）

- 正式发布：`deploy.sh all --revision 73b4dcfdebee88f978bd6dc644501fed81422d64` 于 `2026-10-08T15:54:59Z` 退出码 0。bundle 为 `106,966,548` bytes，SHA-256 `cd6a3c485150bb75d2593d41a95104fc32d7fe19116d5cc0fa69107c306e2092`。运行镜像标签、后端 `APP_RELEASE`、前端构建信息、HTTPS `/healthz` 与 `/readyz` 的版本/完整 SHA 相符。
- 发布验收：备份压缩/哈希/元数据核验通过；隔离恢复 104 张表，Alembic head 匹配，临时恢复 schema 已从 `cr_testdb` 清理。Backend、Frontend、MySQL、Redis、ClamAV 和 decoy 健康。post-deploy `ops-check` 为 `can_continue=true` 且无阻断项，磁盘 94% degraded。最近可用空间 `13,510,909,952` bytes，高于 12 GiB 门槛 `626,008,064` bytes。
- 发布前门禁原因及处理：根盘低于发布门槛；用户批准清理旧 Docker 发布镜像标签，并在已批准的缓存清理边界内回收可再生 BuildKit 缓存（记录为 308→164 项、`df` 可用空间增加约 3.99 GB）。随后正式容量预检、备份/恢复和部署通过。未动数据库卷、备份和生产任务。
- 浏览器验收：普通 Chrome 既有超级管理员会话，未开无痕窗口；项目 bWAPP / PHP，测试目标为隔离沙箱，远程目标留空，记录按 72 小时保留。三份任务结果回执均记录 `cleanup_confirmed=true`、worker exit 0；清理范围字段称容器与任务目录已移除。本轮没有独立主机侧容器检查。结果下载件 SHA-256 与当时生产界面显示的 artifact 摘要前缀一致。

| 任务 ID | 模式 | 结果状态 | AI 动态用例 | 确定性执行 | Worker 清理 |
|---|---|---|---|---|---|
| `sbx_d430800657d2421d9c9f97e8` | bWAPP / PHP 黑盒 | `partial` | HTTP 402 / `billing_error`，动态用例 0 / skipped；界面事件显示角色 0/4 | `/login.php` 来源 `php_entrypoint` HTTP 200；runner exit 0 | 回执 `cleanup_confirmed=true`；范围字段称容器和任务目录已移除 |
| `sbx_d0a419678b134a18bb0a5562` | bWAPP / PHP 黑盒重复 | `partial` | HTTP 402 / `billing_error`，动态用例 0 / skipped；界面事件显示角色 0/4 | `/login.php` 来源 `php_entrypoint` HTTP 200；runner exit 0 | 回执 `cleanup_confirmed=true`；范围字段称容器和任务目录已移除 |
| `sbx_7f2d1411cafe47f592c3e1a5` | bWAPP / PHP 白盒 | `partial` | HTTP 402 / `billing_error`，动态用例 0 / skipped；界面事件显示角色 0/4 | 白盒 runner 执行通过，exit 0；日志含旧 NuSOAP PHP 弃用告警 | 回执 `cleanup_confirmed=true`；范围字段称容器和任务目录已移除 |

- 三条结果都没有把 AI 未运行伪装成通过；按本轮生产界面进度事件观察，后续 AI 修复与多 Agent 报告已停止，后续模型审查被跳过并生成确定性报告。事件原文和当时观察时间已记录在 JSON，但未留存截图、DOM 导出或事件 ID，结果 JSON 与 Markdown 也没有这组阶段事件字段，外部无法重放该 UI 观察。两条黑盒的 route smoke 通过只说明登录入口可达，不代表完成渗透或 AI 驱动的黑盒安全审查。白盒确定性执行退出 0 也不表示 AI 对整个源码完成审计。
- 逐任务字段化证据：[`v4096-production-sandbox-retests-20261009.json`](证据/v4096-production-sandbox-retests-20261009.json)，SHA-256 `c4aa558dd34de6d3f191ad2c7277e80fe665d8ae90a7fc595512a8e56203fe8f`。证据由普通 Chrome 下载的三个生产结果 JSON 字段化，未保留原始 worker 日志或源码内容；三条生产界面进度时间线的原文及观察时间也一并记录。生产界面下载的第一条任务 Markdown 报告保存在 `证据/sandbox-review-report-sbx_d430800657d2421d9c9f97e8.md`，SHA-256 `287a93b73e52c922aeb1d20ae282685be2bedbe66c929c481bf3cd0a0823e87f`；其正文确认 AI 动态补充未执行、生成 0 个动态用例以及模型增强因 `billing_error` 停止，但不包含 Agent 角色数。三条任务的 0/4 均来自生产界面进度事件观察，不是结果 JSON 或 Markdown 报告中的字段；未保存截图、DOM 导出或事件 ID，无法供外部重放。
- 本轮识别出的实际阻断：生产配置的模型服务在源码上下文压缩请求返回 HTTP 402，三条任务均同类失败。该码证明本次请求被计费/额度拒绝，不能单独证明余额为零；需模型服务管理员核验额度/计费，再重复 AI 黑盒与白盒。百万 token 模型上下文能力仍未实测。


## v4.0.97 HTTP 402 与多 Agent 报告完整性及生产发布验收（2026-10-09）

### 复现与修复范围

- 生产基线只读核验：`/healthz` 为 v4.0.96、SHA `73b4dcfdebee88f978bd6dc644501fed81422d64`。之前三条 bWAPP/PHP 沙箱任务（两次黑盒、一次白盒）均为 `partial`，源码上下文阶段收到 HTTP 402 / `billing_error`，AI 动态用例为 0 / skipped；黑盒 `/login.php` HTTP 200 和白盒 runner exit 0 只证明确定性子流程，不代表 AI 审查通过。402 不能单独证明供应商余额为零。
- 代码复现发现：普通聊天会对 HTTP 402 重试；旧 DeepSeek 入口没有把 402 明确成不可重试错误；Responses 编排会因错误正文提到模型名而触发模型降级，造成额外付费请求可能；沙箱报告完成状态只看部分计数/角色状态，失败时可能仍将模型报告候选保存和发布。
- 修复后 402 被单次归类为不可重试 `billing_error`；Responses 保留状态码用于内部分类，但清空上游响应正文，不将供应商私有错误字段带入进度/用量审计，也不会按错误正文切换模型。多 Agent 完成需要 4 个必需角色、仅允许系统角色列表、最多 7 个角色、成功状态和执行/总数/清单精确一致，同时原始模型报告候选非空。任何条件不满足时，报告制品与发布内容都使用确定性报告，并将清单错误或空报告写入完成事件与摘要状态。
- 无效角色清单报告泄漏先复现为 5 个失败样本；修复后验证已保存 artifact 和发布报告都不含未验证模型候选。空候选有单独回归，确认两处输出均使用确定性内容且事件解释 AI 报告为空。

### 候选验证结果

- 后端全量：`6685 passed, 6 skipped, 1213 warnings`，306.09 秒。6 项因本机 Redis 依赖未启用而跳过，不计作通过。
- 402、降级和沙箱报告相关 7 个测试文件：`123 passed`。独立复核再次运行沙箱核心 8 项：`8 passed`；仅使用 mock，没有请求真实模型或生产服务。
- Ruff 检查所有变更的 Python 文件通过；`git diff --check` 通过。
- 发布脚本：release binding `33/33`，备份配置、恢复、部署回滚与故障注入矩阵通过。
- 未改前端；本轮修改集中于 LLM 402 处理、多 Agent 报告完成/发布门和对应后端测试。

### 正式生产发布结果

- 正式 `deploy.sh all --revision c1243cf70b891fee18cb883b3b3b64523ed4679a` 于 `2026-10-08T17:43:04Z` 退出码 0。镜像 ID：Backend `sha256:15da32848dfba405e7730e687575281cd15aec9c0b413573af16ff49cec2bf92`；Frontend `sha256:57b71a200f24fb25ddba266d6aaf0f77d41fb47e669f34df4ed88ece8a783e42`。发布账本时间 `2026-10-08T17:42:40Z`，记录 `TARGET=all`、备份路径及 Alembic 版本。
- HTTPS `/healthz` 返回 `{"status":"ok","version":"4.0.97","release":"c1243cf70b891fee18cb883b3b3b64523ed4679a"}`；`/readyz` 返回 `ready` 且版本与 SHA 相同。运行中后端环境、前后端镜像、活动前端 `buildInfo`、发布账本及 Alembic `062_audit_log_action_length (head)` 一致。HTTP→HTTPS 返回 308。
- 发布前备份 `code_review_20261008T173452Z_c1243cf70b89.sql.gz` 的 gzip、SHA、元数据及隔离恢复校验通过；恢复出 104 张表，迁移版本匹配。`prism_verify_*` 临时 schema 已删除；`cr_testdb` 容器仍运行约 8 周，挂载卷仍在使用且未删除。
- 正式运维巡检 `2026-10-08T17:48:20Z` 为 `degraded`、`can_continue=true`、`blocking_checks=[]`。发布账本、关键容器、备份、Alembic、HTTPS 均为 `ok`；唯一降级为磁盘使用 94%（告警阈值 85%，临界阈值 95%），可用 `12,078,252 KiB`。部署前容量门禁已通过；部署后可用空间低于下一次发布的 12 GiB 门槛约 493 MiB。
- 成功部署后按先前确认的保留规则删除 v4.0.93 Backend/Frontend 两个标签；对应两个镜像 ID 已被 Docker 确认删除。后续 `cleanup.sh --cache-until 168h` dry-run 仍列出 958.2 MB 可回收 BuildKit 缓存，但其未满 168 小时，因此未继续清理。Docker 存储统计（删除标签前）为镜像 15.34 GB（13.38 GB 可回收）、缓存 958.2 MB；没有据此执行全局 prune。
- 缓存范围记录：`until=6h` 的 1.424 GB 回收动作比原 `until=168h` 预览更宽；不将其写作 168 小时范围内的清理。后续缓存/旧发布树/日志/备份的额外删除需先核对范围并确认。
- 只读磁盘目录占用：备份约 46 GB、发布树约 36 GB、Docker 约 49 GB、系统 journal 约 3.9 GB、`/tmp` 约 4.6 GB（含历史 release bundle）。只删除了已获准的旧镜像标签；未清理备份、日志、`/tmp`、旧 release 工作树或任何数据库/数据卷。
- 生产浏览器真实点击未完成：两次 Cua `getState` 均超时。已完成的是 SSH 部署脚本验收、HTTPS/API 健康冒烟和静态资产版本核验；不把这些表述成登录后的浏览器视觉验收。未在 v4.0.97 上另起付费模型请求；之前三条生产 PHP 黑白盒样本均遇 HTTP 402，AI 动态覆盖仍需供应商侧恢复后真实复测。

详细机器可读记录见 [`v4097-http402-regression-20261009.json`](证据/v4097-http402-regression-20261009.json)。
