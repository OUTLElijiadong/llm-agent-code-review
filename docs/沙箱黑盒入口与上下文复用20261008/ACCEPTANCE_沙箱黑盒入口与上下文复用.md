# 沙箱黑盒入口与上下文复用验收

## 当前状态（2026-10-08）

- 生产当前为 v4.0.95 / SHA `b39d15fd69d9d184e9c1d9a3f4a0cf65feb2ffad`；v4.0.96 仍是候选，尚未发布。
- 最新只读容量证据采集于 `2026-10-08T14:42:17Z`：根盘 95%，可用 `10,688,143,360` bytes；发布门槛为 `12,884,901,888` bytes（12 GiB），短缺 `2,196,758,528` bytes（约 2.044 GiB）。按用户批准范围删除旧 Docker 发布镜像标签后仍低于门槛。获准的一次 BuildKit 缓存清理因 Docker 28.0.1 graphdriver 错误失败，缓存统计未变；没有清理 dangling images、回滚状态、数据库卷、备份或任务。
- `ops-check` 最新完整检查时间为 `14:23:38Z`，唯一阻断为磁盘且禁止发布/写入；其他服务与备份/迁移/HTTPS检查通过。`14:27:25Z..14:27:26Z` `/healthz` 与 `/readyz` 为 HTTP 200，仍运行 v4.0.95。完整只读记录见本目录 `证据/v4096-发布阻断-20261008.md`。
- 活动 builder `default` 的 308 条缓存记录共 `6,188,408,317` bytes；按 LastUsedAt >6h 统计有 247 条、`3,750,006,192` bytes。`buildx du --filter until=6h` 返回全部记录，附加 `private=true` 返回 0 条（但完整清单有 167 条 Private），筛选预览异常。一次已获准的 `docker buildx prune --builder default --force --min-free-space 12884901888` 在 Docker 28.0.1 / overlay2 graphdriver 上返回 `could not stat fs at : no such file or directory`，未释放缓存；该错误与 Moby #5821 / 修复 PR #49623 的已知问题一致。不得据此估算可释放空间；升级 Docker 需要维护窗口。
- 只读盘点显示占用主要分布于 `/opt` 106.41GB、`/var` 60.83GB、Docker 54.31GB、旧 `/opt/code-review/backups` 48.28GB、`/opt/prism-releases` 34.98GB、`/tmp` 4.94GB 和 journald 4.09GB；这些是占用位置，不代表可直接删除。MySQL 数据卷约 30.97GB，Docker 仅标记约 19KB 可回收。当前候选还没有生产验收。HTTP 402 是模型服务计费或额度拒绝，不能仅凭状态码认定余额归零。生产 AI 动态用例目前未生成，真实黑白盒整体尚未通过。

## v4.0.96 候选最终复测

- 后端完整 `tests/unit`：`4606 passed, 6 skipped`，243.27 秒；6 项依赖本机 Redis，未运行。测试报告 944 条告警，主要为既有弃用与 pytest 收集告警。
- 改动 Python 文件 Ruff、`compileall`、`git diff --check` 通过。全仓 Ruff 还报告 63 条未改文件 lint 问题，因此不把全仓 Ruff 标为通过。
- 独立候选审查补充定向路径 `475 passed`，并验证用量记录并发样本；与全量单元套件属于候选代码证据，不是生产模型调用证据。
- 生产磁盘发布门禁不通过且 `can_continue=false`。未运行生产 deploy、未做生产备份/恢复变更、未切换服务。旧镜像标签删除与一次失败的 BuildKit prune 尝试均未提供足够容量。候选 bundle 已只写入独立 tmpfs `/dev/shm`，远端大小、SHA-256、Git bundle 完整性和唯一 HEAD 均通过；尚未创建根盘 staging。
- 未完成发布后 Safari/Chrome 点击验收，也未证明生产 AI 动态用例能生成并执行；此前黑白盒动态用例为 0 的生产结果仍只判部分通过。

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

## 生产发布与复测

v4.0.90 部署前只读核对：生产 `/healthz` 与 `/readyz` 均为 v4.0.89 / SHA `0c017fb700287bc45167c25eef2f210b64530e1c`；当时 Worker active，活动 release 路径与该版本一致。v4.0.90 发布后 Worker 仍从 v4.0.89 release 路径启动，但执行器源码 SHA 与 v4.0.90 候选源码 SHA 一致，profile pin 也一致；路径版本未对齐需明确保留这一边界。v4.0.91 部署后生产健康核验通过，但实际黑盒复测因压缩耗尽 300 秒而未执行 AI 动态用例。v4.0.93 已于 2026-10-08 发布；本轮候选为 v4.0.94，发布前必须刷新容量、备份/恢复和当前运行 SHA 门禁。

| 样本 | 版本 | 模式 | 任务 ID | 终态 | 关键结果 | 清理回执 |
|---|---|---|---|---|---|---|
| 修复前 bWAPP | v4.0.89 | PHP 黑盒 | `sbx_f55815302ddf42859d1c4291` | 部分通过 | `/login.php` 200；压缩超 300 秒，AI 动态用例跳过 | UI 显示完成回收；JSON 字段待核 |
| 修复后 bWAPP A | v4.0.90 | PHP 黑盒 | `sbx_f5aff86c030a4cceb61d516d` | 部分通过 | 481 片/259 文件；压缩 138 次调用后 300,213 ms 超时；55 截断、4 读超时；`/login.php` 200；动态用例 0 | 已核实 `cleanup_confirmed=true`，容器与任务目录清理 |
| v4.0.91 复测 | v4.0.91 | PHP 黑盒 | `sbx_b0e1448463804dcea6a696f3` | 确定性部分通过 | `/login.php` 200，runner exit 0；481 片压缩 153 次调用后 300,050 ms 超时；AI 动态用例 0 | 已核实 `cleanup_confirmed=true`、exit 0 |
| v4.0.92 样本 A | v4.0.92 | PHP 黑盒 | `sbx_eb4ec5deea23402bbdd0fd8f` | 部分通过 | 481 片；压缩 131 次模型调用、300,254 ms 后 `finish_reason=length`；`/login.php` 200；动态用例 0 | 待从结果回执独立复核 |
| v4.0.93 样本 A | v4.0.93 | PHP 黑盒 | `sbx_f207b9e313f5499cb0fd3ca1` | 部分通过 | 项目 157，归档 259 文件、来源 481 片；压缩 178 次/153,241 ms 后摘要校验失败，AI 动态用例 0；`/login.php` HTTP 200 | `cleanup_confirmed=true`，容器与任务目录清理 |
| v4.0.94 样本 A | v4.0.94 | PHP 黑盒 | `sbx_e32876716e62433cb3eeb8fc` | 部分通过 | `/login.php` 来源 `php_entrypoint` HTTP 200，确定性 runner exit 0；来源引文无法从对应来源原文核验，AI 动态生成 skipped/0，整体 `verification_status=partial` | `cleanup_confirmed=true`，容器与任务目录回收 |
| v4.0.95 样本 A | v4.0.95 | PHP 黑盒 | 待发布 | 待测 | 原场景复测；要求完整压缩、AI 动态用例生成并执行、确定性路由通过 | 待核 |
| v4.0.95 样本 B | v4.0.95 | PHP 黑盒重复 | 待发布 | 待测 | 重复同一 PHP 黑盒场景，观察摘要校验、调用数和耗时是否稳定 | 待核 |
| v4.0.95 样本 C | v4.0.95 | PHP 白盒 | 待发布 | 待测 | 同一项目白盒扫描；核验 AI 动态用例、执行结果、源码覆盖和清理回执 | 待核 |

历史记录中的 v4.0.94 已部署且生产健康。v4.0.95 随后发布，但后续三项生产样本的 AI 动态覆盖均未生成；不得声明黑白盒全通过。真实任务记录按用户要求保留。

## 未验证边界

- 481 片真实生产项目样本不等于实测 100 万 token 的外部模型理解能力；生产配置显示的上下文窗口值也不等于该能力证明。
- 6 项 Redis 用例在本轮因本地环境条件跳过；另未据此声明所有业务路径、所有语言运行时或所有浏览器通过。
- Java Runner 镜像此前未能更新，版本仍为旧 digest；本次 PHP 样本不能证明 Java 环境已恢复。
- 100 万 token 的模型理解能力没有真实生产证明；481 个源码片段样本不是等价证明。
- v4.0.96 尚未发布；发布前仍须满足容量门禁并完成新备份/隔离恢复。发布后重复黑盒及白盒样本、AI 用量归因、Worker cleanup receipt 尚待逐项验收。


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

先前生产 UI/事件中有一次 HTTP 402 观察，但本地没有保存 event ID、采集时间或状态码原始回执。已下载的四份 JSON 结果不含该状态的逐任务关联，因此上述任务只据各自结果字段判定；402 仅作为未关联的计费/额度拒绝观察，不推断余额为零，也不将每个任务错误唯一归因于它。

候选 v4.0.96 对 402 执行不可重试分类、压缩并发队列快速停止、测试生成内外层停止重试，并增加 Python 标准库 `from-import` 成员真实导入校验。复核继续发现 PHP 并行语法修复会在账务失败后启动排队调用，Agent Mesh 会将文件读取账务错误重新排队，报告阶段账务异常也不会保存确定性报告；已增加语法修复任务级停止、Mesh 失败类型保留/死信和报告确定性回退。早先 5 个相关测试文件 `332 passed` 及 `tests/unit` `4565 passed, 6 skipped` 均发生在这三处改动之前；最终新增针对测试与全量回归正在进行。此前候选的前端及部署脚本通过记录不替代发布后验收。

发布状态仍为阻断：最新容量快照为 `2026-10-08T14:26:57Z` 根盘可用 `10,814,038,016` bytes，比 12 GiB 下限少 `2,070,863,872` bytes（约 1.929 GiB）；活动 `ops-check` 明确 `can_continue=false`。容量门禁、发布备份/隔离恢复和真实浏览器发布后验收尚未完成。不得绕过门禁，也不得把本地测试通过写成生产通过。
