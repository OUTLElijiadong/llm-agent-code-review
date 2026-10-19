# 沙箱精确 HTTP 白名单验收记录

日期：2026-10-19

## 4.1.11 原始实现代码验收记录

- 后端 schema：本地黑盒请求必须携带 1–20 条精确 GET 规则；4.1.11 原始实现还强制包含无查询参数的 `GET /`，生产证据确认该限制过严，当前修订已移除，尚未重新部署。
- Worker：独立验证同一契约，将规范计划纳入请求摘要，并只生成固定 runner 环境字段；无法由请求覆盖命令、镜像、挂载或额外环境变量。
- Runner：启动目标前接管并清除编码计划；只做 TCP 就绪检查，再按计划向固定 loopback origin 发 GET；不跟随重定向、不记录正文；跳过归档中的启动补丁、PoC 与 Agent 测试脚本。日志截断时后端拒绝回执。
- 报告：服务端逐项核对固定计划和 runner 回执；包含目标、状态、字节数、耗时、断言状态及覆盖缺口；明确声明白名单外路径和 AI 动态用例未覆盖。
- 前端：仅在本地黑盒显示精确请求编辑器；验证失败禁止提交；结果用表格展示，不输出原始回执或响应正文。
- 原验收记录中的 runner 摘要曾与源码记录不一致。2026-10-19 对活动生产树 `/opt/prism-releases/974ba0d68bd1ea3baa29bc50b6a7fdb383418070/deploy` 只读核验：Worker 源文件 SHA-256 为 `43117cbfa138b0441e00bc473be53f729ef3dfd1f952092f8ed082d9d515882c`；五个活动 profile 镜像 ID 均与 `profiles.json` 固定摘要一致，镜像 runner SHA-256 均为 `b441df2dc23e274a68b41a7a1befed8a0979429598c07c2d04cd2e7994d9a9a1`，与 4.1.11 源码和 Compose 默认值一致。历史记录不代表当前活动镜像状态。

## 4.1.11 原始实现阶段的历史检查（不覆盖本次修订）

| 检查 | 结果 |
|---|---|
| `frontend/src/views/sandbox/SandboxWorkstation.test.ts` | 原始实现阶段记录为 27 项通过 |
| 前端生产构建 | 原始实现阶段记录为 `vue-tsc && vite build` 成功 |
| 沙箱 HTTP scope schema 测试 | 15 项通过 |
| 报告和 Worker 回归 | `test_sandbox_report_publish.py` + `test_prism_sandbox_executor.py`：178 项通过；出现 1 条 Python `crypt` 弃用警告 |
| Worker scope 独立验证 smoke | 通过；接受有效精确计划，拒绝编码路径和查询越界 |
| runner 本地 HTTP 集成 | 通过；仅出现 `/` 与 `/healthz?name=allowed` 两个请求；目标进程未获得白名单环境变量；归档启动补丁和 AI 脚本未运行 |
| runner 非法多层编码查询集成 | 通过；返回 `scope_invalid`，应用启动标记不存在 |
| 五种运行时镜像构建 | 原验收记录称 Python、Node、Java、Go、PHP 全部构建成功且产物为 `linux/arm64`；历史记录中的摘要值不一致。2026-10-19 已从当前活动生产 Worker 核实五种镜像均为 `linux/amd64` 并内含 4.1.11 runner 摘要 `b441df2d…`；历史本机构建仍不覆盖本次修订镜像 |
| Docker 内 runner 正向/负向验收 | PHP 镜像隔离容器中白名单请求成功；多层编码路径返回 `scope_invalid` 且未产生任何 HTTP 请求 |
| 固定 PHP LFI 提交 runner 烟测 | `purva-patel/Local-File-Inclusion-Demo@0e0e47093ac866b9a579a0c3fb61a3a6410355a0`：禁网、只读根文件系统下仅发送 `GET /`、`GET /vulnerable.php?page=about.php`、`GET /vulnerable.php?page=README_FILES.txt`、`GET /vulnerable.php?page=nonexistent.php`；状态均为 200。只记录状态、响应字节数和耗时，不保存正文；未做内容语义断言 |
| Python `compileall`、`bash -n`、`git diff --check` | 通过 |
| Worker 五镜像摘要核对 | 当前活动生产五种 profile 的 ID 与 profile digest 一致，镜像内 runner 摘要均为 `b441df2d…` 并与 4.1.11 活动源码相同；修订版的 `fa6afdd1…` 尚未构建 |

上述测试和镜像构建结果属于 4.1.11 原始实现阶段，不覆盖本次新增的“根路径可选”修订。当前修订已另行运行对应的前端与后端回归，具体结果见下方；镜像仍需在正式候选发布树重建并核验。

## 当前修订的静态检查

- `deploy/sandbox/runner.sh` 当前 SHA-256 为 `fa6afdd11e17c45feef91cc73b2903b25cd1583e9a391e8ac3a7ae50cc7e2ad1`；静态核对确认五个运行时镜像默认值均与此摘要一致。该核对只证明源码配置一致，不证明镜像已构建或生产 Worker 已更新。
- 后端 schema/service、Worker、runner 和前端中的根路径必选逻辑已检查并移除；白名单仍要求 1–20 条精确 GET 请求，结果摘要在无根路径时指向首条已配置请求。
- 前端 `SandboxWorkstation.test.ts`：27 项通过；前端 `vue-tsc && vite build`：通过。
- 后端 `test_sandbox_http_scope.py` 与 `test_prism_sandbox_executor.py`：106 项通过，含 1 条 Python `crypt` 弃用警告。
- Python AST 解析、`bash -n deploy/sandbox/runner.sh`、五个 Compose profile 的 Runner SHA 一致性校验、`docker compose config --quiet` 和 `git diff --check` 均通过。
- 此处仅记录本地候选代码验证；五种修订版镜像、生产部署与真实产品页验收仍待执行。

## 活动生产发布树只读核验（2026-10-19）

- 活动发布树为 `/opt/prism-releases/974ba0d68bd1ea3baa29bc50b6a7fdb383418070`，不是旧 checkout `/opt/code-review`。旧 checkout 的 HEAD、`.env` 与旧账本仍指向 `f114033bb76af393e5ef595ca8b824ebde0de62a`；不能用它的巡检结果判断活动生产状态。
- 活动版本为 `4.1.11`。Backend/Frontend 运行镜像、`APP_RELEASE`、活动发布账本和 `.env` 均绑定提交 `974ba0d68bd1ea3baa29bc50b6a7fdb383418070`，容器镜像 ID 与活动账本一致。
- 从活动树运行只读 `ops-check.sh` 返回 `status=ok`、`can_continue=true`；发布、备份、容器、Alembic、HTTPS、磁盘和内存检查均通过。本轮无需修改生产账本或 `.env`。
- 活动 Worker 的 systemd 工作目录与执行脚本均指向 974 发布树。五种活动 profile 镜像内 runner SHA 均为 `b441df2d…`，并与活动版本源码一致。
- 当前修订 runner SHA 为 `fa6afdd11e17c45feef91cc73b2903b25cd1583e9a391e8ac3a7ae50cc7e2ad1`。要让修改在生产生效，正式发布时必须重建并固定五种运行时镜像，同时更新 Worker 和前后端；仅更新前后端不足以完成修复。
- 产品版本已设为 `4.1.12`，代码与任务文档已提交为固定候选；当前尚未构建生产镜像、部署或运行修订后的生产黑盒任务。

## 生产发布与真实产品端验收

- 生产版本：`4.1.11`；Safari 页面构建标记：`974ba0d68bd1ea3baa29bc50b6a7fdb383418070`（2026-10-19 12:18:46 UTC）。本次未再次部署或升级版本。
- 真实产品任务：`sbx_b12572a3c508401f9d305c16`，项目 `Local-File-Inclusion-Demo`，来源提交 `purva-patel/Local-File-Inclusion-Demo@0e0e47093ac866b9a579a0c3fb61a3a6410355a0`，执行源码指纹 `bda7b9e0aaebf6288b94461f29500b60ad8bd742ec8ace09a4cb1ab42df8c568`。任务在当前登录的 `outle` 普通用户页面中真实创建并显示；实际执行一次，耗时约 6.6 秒，最终状态为失败。
- 实际 HTTP 证据（本地 loopback，未设远程目标）：`GET /` 无 HTTP 响应，0 字节，81 ms；`GET /vulnerable.php?page=about.php` 返回 200，4182 字节，215 ms；`GET /vulnerable.php?page=README_FILES.txt` 返回 200，4736 字节，209 ms；`GET /vulnerable.php?page=nonexistent.php` 返回 200，3639 字节，174 ms。后面三条请求符合各自预期状态码；根路径必需探活未通过，因此任务整体失败。
- 故障定位边界：执行日志只证明根路径没有返回可解析的 HTTP 状态行；三个显式 PHP 路径随后均返回 200。应用进程自身输出未作为报告制品保存，现有证据不能进一步判定这是靶场根路由行为还是应用侧异常，不据此声称已查明内部原因。
- 执行约束与回收：结果回执标记 `basis=exact_http_allowlist`、`scope_enforced=true`、请求数 4、响应正文未保存、AI 动态测试状态 skipped、未调用模型；Worker 记录 `cleanup_confirmed=true`，容器和任务目录已回收。未执行任何白名单外请求或远程目标请求。
- 任务详情显示实际 HTTP 请求证据 4 项，并生成确定性 Markdown 测试报告和 5 个证据制品（JSON、日志、JUnit、SARIF、HTML）。Markdown 报告已下载并检查，标注整体未通过、实际状态码、范围约束和覆盖缺口；问题清单仍使用概括性失败描述。
- `README_FILES.txt` 返回 200 只证明状态码符合预期；正文未保存，不能据此判断其中内容或缺失文件处理语义正确。此次运行没有验证完整漏洞覆盖。

## 验收结论

活动生产版本仍为 `4.1.11`。这次真实产品任务失败，直接判定因素是根路径无 HTTP 响应而原实现把它设为必选；三条显式 PHP 路由均返回 200。当前源代码已修正为根路径可选，白名单仍要求至少一条精确 GET；回执摘要优先使用已配置的无查询根路径，否则指向第一条白名单请求。活动发布树的只读巡检全绿；旧 checkout 的巡检失败不代表活动生产故障。修订尚未部署、未运行功能测试、未重新构建镜像，也未再次运行靶场。此前任务仍为失败，不得称为“黑盒测试通过”或“靶场完整测试成功”。

## 4.1.12 生产部署与当前验收状态（2026-10-19）

- 修订已提交并部署：版本 `4.1.12`，提交 `80bac2cc56640d65477926f1820c2abbe90046ae`。部署脚本报告全量发布完成；活动生产版本标记与该提交一致。
- 生产 HTTPS `/healthz` 返回 `status=ok`、版本 `4.1.12` 和提交 SHA；`/readyz` 返回 `status=ready`，版本和 SHA 一致。只读 `ops-check.sh` 中发布、容器、备份校验、Alembic、HTTPS 和内存检查通过。
- `ops-check.sh` 状态为 `degraded`、`can_continue=true`：磁盘使用率 85% 达到告警阈值 85%，要求人工审阅清理。未运行清理脚本或删除数据；用户表示自行清理服务器磁盘。
- 4.1.12 本地验证：前端单测 27 项通过；后端沙箱 HTTP scope 与 executor 测试 106 项通过（1 条 Python `crypt` 弃用警告）；前端生产构建、Python AST、runner `bash -n`、五个 Compose profile 的 runner SHA 一致性、Compose 配置和 `git diff --check` 通过。生产端另完成前端构建和发布脚本健康检查。
- Safari 真实产品页显示生产版本 `4.1.12` 和提交 SHA。当前配置已选择项目 `Local-File-Inclusion-Demo`（固定提交 `0e0e47093ac866b9a579a0c3fb61a3a6410355a`）、PHP、黑盒模式、无数据库、测试目标、远程目标为空；已配置且前后端强制执行三条本机 GET 白名单：`/vulnerable.php?page=about.php`、`/vulnerable.php?page=README_FILES.txt`、`/vulnerable.php?page=nonexistent.php`，每条期望 HTTP 200。根路径未加入，未配置其他请求。
- 此刻上述新任务尚未提交或运行；当前动作确认待用户答复。不得把 4.1.11 的失败任务改写为成功，也不得把本次配置当作已产生 HTTP 请求或报告。
