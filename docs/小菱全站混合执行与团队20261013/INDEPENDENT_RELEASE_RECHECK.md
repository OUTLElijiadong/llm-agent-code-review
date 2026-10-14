# 小菱全站混合执行与团队独立复核

观察日期：2026-10-13。生产检查为只读；复核报告与 MySQL、发布证据追加在候选提交之后。v4.0.99 的受支持发布门已通过，但真实 API 与 Safari 业务验收尚未通过/完成，因此不代表整体功能验收通过。

## 结论

首次独立预检时，工作树 HEAD `86cfb1a9a2d665f77c94fc21fc4d8b9109b04e0b` 只包含 v4.0.98 的三份旧发布文档，后端、前端和 Alembic 063 尚有 105 个路径未提交；这是当时的历史快照，不能继续作为当前候选状态描述。

随后形成的最终候选为 `8f719975ba9398f078d2204e02d56a385e7ee92c` / v4.0.99。发布后本次独立复核在服务器重新计算 bundle 为 108,176,170 bytes、SHA-256 `0a8b7110b68be7e94ea0d33d8b7ab65af41f959012569c0c292c74eeedaf5f15`，均与预期一致；运行 Compose/ledger、镜像 ID、migration 063、health/ready、备份隔离恢复和公网 buildInfo 也已复核。详见[发布证据 JSON](evidence/production/release-v4099.json)。主代理随后报告 v4.0.99 的真实 HTTP 验收发现 API 错误状态被旧 Nginx 模板转换为 200 HTML，管理员未知 run 响应触发 JSONDecodeError；该版本仍不能宣称真实 API/权限验收通过，Safari 真实账号验收也尚待完成。

早期预检记录的生产快照为v4.0.98 / `22404e44a05682e55740abe5a294da66761e2148`、数据库062；它仅证明当时旧版本运行状态。随后99的发布运行态已按上一段另行独立读取，旧预检结论不覆盖99及后续修订的实际状态。

## 需求范围与完成声明

ALIGNMENT、CONSENSUS 已将原始“网站所有功能由小菱代操作”收敛成有权限边界的混合执行：按现有能力注册表执行当前账号可访问的页面能力、复用已有或任务内临时执行/核验成员、按原权限和风险策略处理，并在真实业务完成后定位页面与刷新。登录/注册、秘密输入和文件上传/下载继续使用本人或已有专用控件/工具；这不等于所有网站控件都能无人操作。

因此，交付不能写成“全站全部功能已无人值守完成”。较准确的完成声明应限于“已接入注册能力的页面操作链及其授权、回执和刷新路径已实现”，并同时列出尚未接入的页面能力、专用输入流程和真实浏览器验收缺口。最终候选能力表按当前文档计为 125 项用户能力、142 项管理能力；两侧包含重复项，不能相加后宣称为独立功能覆盖数。早期 114/134 是更早的注册表快照，不能与最终能力表混用。

早期预检时，任务文档仅记录346项基线，未吸收本轮范围和失败修复过程；此项随后由主代理更新到 ACCEPTANCE，记录最终定向351项、前端1897项及99发布后的API复验阻断。早期文档快照不作为当前交付状态。

## 候选本地证据

下表中的 `/tmp/prism-*` 早期日志是 86cf 后、最终提交前的工作树快照，保留为历史证据，不能自动改标成最终候选结果。最终候选 `8f719975...` 的测试摘要由主代理另行报告：后端定向 351 passed（11 个文件）、前端 1,897 passed（146 个文件）、vue-tsc/Vite build 通过；Luna 定向 176 项、三视口 E2E 每视口三轮和 build 通过。上述摘要尚未由本子代理重新读取原日志；发布后证据仍需单独复核。

| 证据 | 结果 | 能证明什么 / 不能证明什么 |
| --- | --- | --- |
| `/tmp/prism-site-final-safety.log` | 109 passed | 本地站点请求上下文、授权来源和服务层定向测试通过。 |
| `/tmp/prism-site-browser-integration.log` | 10 passed | 本地 Responses 与站点团队集成测试通过。 |
| `/tmp/prism-browser-final-backend-2.log` | 30 passed | 浏览器动作服务与团队集成定向测试最终通过。其前一轮 `/tmp/prism-browser-final-backend.log` 是 28 passed、2 failed：secret/file 用例复用了 inspect-call 的 `call_id`，先触发幂等参数冲突；更换为新的 file-input 后才实际验证拒绝文件。另一失败来自 SQLite 线程池不同连接使用内存库、缺少 `user_role` 表；本文件以 `StaticPool` 夹具解决，未改生产权限。 |
| `/tmp/prism-site-browser-full-frontend.log`、`/tmp/prism-xiaoling-final-frontend-suite.log` | 两次各 146 个文件、1895 passed | 候选前端单元/组件测试通过；不是实际用户浏览器验收。 |
| `/tmp/prism-layers-e2e-round-2.log` | 3 passed，375/768/1280 px；每个视口循环三次 | 本地 Chromium E2E 对菜单、聊天、确认窗、选择框的几何和点击命中通过。测试源码把 `/api/**` 全部拦截为 fixture，使用本地持久 browser profile，不访问生产，也不是 Safari。初轮三个视口因 `.stat-grid .stat` 选择器失败；第一轮修复后又因 `.chat-drawer` 未找到失败；最终一轮通过。 |
| `/tmp/prism-layers-unit-round-2.log`、`round-3.log` | 两轮各 5 文件、54 passed | 层级回归单测连续两轮通过。此前因资源 URL/变量提取失败的日志保留在 `initial` 和 `round-1`。 |
| `/tmp/prism-xiaoling-page-acceptance-round-{1,2,3}.log` | 三轮各 8 文件、181 passed | 页级/响应时间线定向单测重复通过；不代表全站业务操作或生产状态。 |
| `/tmp/prism-xiaoling-final-build.log` | `vite build` 成功，33.97 秒 | 本地前端生产构建完成；`package.json` 的 `build` 是 `vue-tsc && vite build`。日志不是生产镜像 SHA/镜像内容证据。 |

本地圆桌菜单截图保存在 `frontend/test-results/overlay-layers-玻璃页头圆桌、聊天、选择框与确认窗的真实几何-命中：{375,768,1280}px，重复3轮}/roundtable-{width}.png`。截图呈现的是 API fixture 的仪表盘；当前任务目录未保存生产 Safari 截图。主代理报告的生产 Safari 圆桌菜单遮挡复现必须作为旧生产缺陷单独记录，不能与这些本地截图混为候选生产验证。

本次已独立验证生产运行版本执行 migration 063、发布镜像/ledger 对齐、发布备份校验和在 `cr_testdb` 的 104 表隔离恢复。仍未由本子代理验证真实模型请求、认证 API 权限矩阵、普通 Safari 真实账号业务、跨账号隔离和业务读回持久化。主代理报告的 API 404/405 被转成 200 HTML 与未知 run JSONDecodeError 是当前明确阻断；不得用首页 HTTP 200、单测、模拟 E2E 或旧生产论坛列表替代这些验收。

## 新增隔离 MySQL 边界复核（2026-10-13）

测试目标是候选 [`agent_browser_service.py`](../../backend/app/services/agent_browser_service.py) 的 `request_action()` 实际 helper。使用生产主机上独立运行的 `cr_testdb`（MySQL 8.0.46，`REPEATABLE-READ`），候选 backend 源码以只读挂载进入临时容器；每轮由一个 Session 调用 `request_action()`，在首次 `refresh()` 明确观察到 `queued` 后，第二个独立 Session 调用真实 `claim_action()` 与 `acknowledge_action()` 提交 ACK。两个 Session 的 MySQL `CONNECTION_ID()` 不同。测试没有调用外部 API endpoint 或真实浏览器。

| 检查 | 实测结果 |
| --- | --- |
| 双 Session ACK 可见性 | 三个分别创建的临时 schema 中，三轮均按“首次 refresh=`queued` → 第二 Session 提交 completed ACK → 同一 helper 下一轮 refresh=`completed`”通过，耗时 442 / 453 / 448 ms。源代码在轮询等待后先 `db.commit()` 再 `db.refresh(row)`，验证该事务边界能让 ACK 对下一轮可见。此前旧源码的双 Session ORM 复现读到 `queued`，提交当前轮询事务后才读到新状态；那次复现证明原缺陷，不代表当前候选结果。 |
| token_version / 权限撤销 | 单独 schema 中，第二 Session 在首次 queued refresh 后递增用户 `token_version` 并删除该用户的 `AGENT_CHAT` 角色权限。helper 在 455 ms 内抛出 `ForbiddenError` 并将动作账本置为 `cancelled`；没有继续等待旧快照。 |
| migration 063 | 四个临时 schema 均从 `062_audit_log_action_length` 执行候选 Alembic upgrade 063、空表 downgrade 回 062、再 upgrade 063；均通过。验证唯一请求索引 `uq_agent_browser_request(request_id)`、非唯一复合索引 `ix_agent_browser_owner_session(user_id,surface,session_key,run_id)`。 |
| 大回执字段 | MySQL `INFORMATION_SCHEMA` 显示 `arguments_json`、`observation_json` 均为 `LONGTEXT`。候选 ORM 成功写入并读回含 120 个长标签控件、正文含 28,000 个中文/emoji 字符的 JSON：421,674 UTF-8 字节、164,474 字符，两个字段读回字节数完全一致且 `JSON_VALID=1`。这验证数据库容量，不代表浏览器/API 的请求体限制或完整业务链容量。 |
| 清理边界 | 所有本次临时 schema（ack1/ack2/ack3/revoke 与容量测试 schema）均已删除；0600 凭据临时文件已清除。测试只连接 `cr_testdb`，生产 `cr_mysql` / `code_review` 未改。 |

完整可机读记录（含候选源码 SHA-256、每轮结果和边界限制）见 [MySQL 复核证据 JSON](evidence/mysql-agent-browser-recheck-20261013.json)。

**边界复核结果：** 最终候选的事务边界修复通过三轮真实 MySQL helper/双 Session ACK 复验，也通过独立 token_version 与 RBAC 撤权测试。v4.0.99 已完成受支持发布，备份恢复、镜像/ledger/version、migration、health/ready、HTTPS 和 served buildInfo 门已通过；真实 API 错误语义、权限矩阵、普通 Safari 和真实业务读回仍未通过或尚未执行。

## 发布前生产只读快照（不是当前发布后状态）

以下仅保留 staging 启动前的历史只读快照。99发布后已另行只读核验并记录，不能用本段替代后续发布的实际状态。

- `cr_backend` 与 `cr_frontend` 都是 healthy，镜像标签分别为 `prism-backend:22404e44…`、`prism-frontend:22404e44…`；容器内 `/healthz` 与 `/readyz` 都报告 `4.0.98 / 22404e44…`。
- 生产 `alembic current` 是 `062_audit_log_action_length (head)`；新候选本地 `alembic heads` 是唯一 `063_agent_browser_actions (head)`。这表示 063 尚未在生产运行。
- `https://lijiadong.cn/` 返回 HTTP 200；这只是旧生产首页的网络冒烟，不是小菱功能验收。
- 根文件系统最新快照约 87% 已用、`25,434,161,152` 字节可用（约 23.68 GiB），高于发布脚本默认 12 GiB 余量门槛。它只是一刻快照，发布时必须重新检查。
- 容器 Compose `working_dir` 为 `/opt/prism-releases/22404e44a05682e55740abe5a294da66761e2148/deploy`。当前账本为该目录下 `.releases/current.env`，`RELEASE_STATE_DIR` 未在 dotenv 中设置，因此脚本默认使用 `deploy/.releases`。该账本指向 22404、v4.0.98、Alembic 062；其上一发布是 4.0.97 / `c1243cf70b891fee18cb883b3b3b64523ed4679a`。`/opt/prism-release-state` 存在但不是运行 Compose 树所用的账本目录，勿把它当作当前状态源。
- 当前账本引用的 2026-10-08 备份文件存在，大小 822,398,152 字节，记录的 SHA-256 与文件当前 SHA-256 匹配。该检查没有完成 gzip 全量校验或隔离恢复，也不是新候选的发布备份/恢复证据。`cr_testdb` 容器存在并运行，但没有健康检查；本轮独立隔离验证未使用备份恢复。新增测试随后在该容器中创建并删除了唯一命名的临时 schema，未访问生产 `cr_mysql`。

## 发布后状态与剩余验收门

1. **v4.0.99 受支持发布成功。** 部署日志记录 `all` 发布成功、SHA `8f719975ba9398f078d2204e02d56a385e7ee92c`、Alembic `063_agent_browser_actions`；服务器制品字节数/SHA 与预期完全一致。实际 Compose 目录是 `/opt/prism-releases/8f719975ba9398f078d2204e02d56a385e7ee92c/deploy`，运行账本是该目录 `.releases/current.env`，不是旧 `/opt/prism-release-state`。
2. **部署基础门已通过。** current/previous ledger、前后端镜像 SHA 和 image ID、APP_RELEASE/APP_VERSION、backend healthz/readyz、frontend healthy、公网首页和 served buildInfo、Alembic head 均已核对。ClamAV 容器 healthy，发布后磁盘可用 23,682,772,992 bytes（88% 使用率）。运行 backend 容器 `BACKEND_RELEASE`、`FRONTEND_RELEASE` 环境键仍残留 22404 值；主 APP_RELEASE/APP_VERSION、镜像、ledger、已校准 `.env` 一致。这是非主身份 env_file 字段的运行时遗留，记录供后续清理/核实，不把它误报成镜像漂移。
3. **发布备份恢复门已通过。** 本次备份为 `code_review_20261013T074818Z_8f719975ba93.sql.gz`，830,881,273 bytes，gzip 校验通过，sidecar 与重算 SHA-256 均为 `1adf6a0a2d4cc0cfee2b4200bc4091c165095eedcb9a9297192dc0a9cc7bca6d`。发布日志确认其恢复到隔离 `cr_testdb` 的临时库，共 104 张表、revision 062；这是迁移前备份恢复验证，不是将生产库回滚。
4. **API/真实业务验收仍阻断。** 主代理报告 v4.0.99 的匿名 401 正常，但 API 404/405 被 Nginx 的 `proxy_intercept_errors`/SPA `error_page` 映射成 200 HTML，未知 run JSON 响应触发 JSONDecodeError。主代理正修复并计划 v4.0.100；该修复版本的真实 API/权限矩阵须重新验证，不能沿用 v4.0.99 部署成功结论。
5. **Safari 与真实账号验收未完成。** 本子代理未登录或使用 GUI。需要在修正版上由已授权流程验证普通用户/管理员及第二账号的操作、权限拒绝、审批、业务读回/持久化、页面刷新和失败/排队状态；论坛旧列表、生产首页 200、fixture E2E 不构成替代证据。
6. **保留回滚边界。** `deploy.sh` 回滚只切应用镜像，不会 downgrade/restore 数据库。迁移后若新表已写入，063 的 downgrade 会拒绝删除非空账本；先评估上一应用对额外表的兼容性，不能为恢复旧应用而清除新账本记录。

## 独立复核阻断清单

- [x] 最终候选 SHA `8f719975ba9398f078d2204e02d56a385e7ee92c` / v4.0.99 已形成；86cfb1a9 仅为历史预检快照。
- [x] 候选语义版本、构建镜像及 backend `APP_RELEASE` / `APP_VERSION`、served frontend buildInfo 一致；runtime backend 仍带旧非主身份 `BACKEND_RELEASE` / `FRONTEND_RELEASE`，见偏差记录。
- [x] 隔离 MySQL 063 upgrade / 空表 downgrade / re-upgrade 通过，JSON 列为 LONGTEXT，大回执读回成功。
- [x] 新候选 MySQL `REPEATABLE-READ` 双 Session ACK 可见性：真实 helper 三轮通过；token_version/RBAC 撤销取消通过。
- [x] 本次备份 checksum、gzip、隔离恢复与 104 表/062 revision 检查完成。
- [x] bundle 服务器字节数/SHA-256、容量、active ledger 路径通过。
- [x] 完整 `all --revision <SHA>` 部署完成，运行镜像、release ledger、数据库 revision、served buildInfo 与 health/ready/HTTPS 一致。
- [ ] v4.0.99 API 错误语义已发现阻断并由主代理修复中；需在后续候选重新验证匿名 401、404/405 JSON、未知 run 错误响应和角色权限矩阵。
- [ ] 普通 Safari 的真实账号业务场景重复三轮；论坛 44/43/42 的旧生产只读列表不作为新候选证据。
- [ ] 更新 ACCEPTANCE、FINAL、TODO，明确哪些页面能力或专用输入仍未覆盖。
