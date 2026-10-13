# 小菱全站混合执行与团队独立复核

观察日期：2026-10-13。生产检查为只读；本报告不表示候选已发布或已通过生产验收。

## 结论

当前没有可发布的候选提交。工作树 HEAD `86cfb1a9a2d665f77c94fc21fc4d8b9109b04e0b` 只包含 v4.0.98 的三份旧发布文档；本次后端、前端和 Alembic 063 的实现仍有 105 个 `backend`、`frontend`、`deploy` 路径处于脏状态。`deploy/deploy.sh` 要求来源干净、revision 与 HEAD 完全相同，并且仅允许 `all`。因此，不能把 `86cfb1a9` 当作新功能发布 SHA；实际功能提交、版本号和发布制品哈希尚待确定。

生产仍运行 v4.0.98 / `22404e44a05682e55740abe5a294da66761e2148`，数据库 revision 为 `062_audit_log_action_length`。当前健康状态只证明旧版本运行正常，不能证明未发布的 063 候选。

## 需求范围与完成声明

ALIGNMENT、CONSENSUS 已将原始“网站所有功能由小菱代操作”收敛成有权限边界的混合执行：按现有能力注册表执行当前账号可访问的页面能力、复用已有或任务内临时执行/核验成员、按原权限和风险策略处理，并在真实业务完成后定位页面与刷新。登录/注册、秘密输入和文件上传/下载继续使用本人或已有专用控件/工具；这不等于所有网站控件都能无人操作。

因此，交付不能写成“全站全部功能已无人值守完成”。较准确的完成声明应限于“已接入注册能力的页面操作链及其授权、回执和刷新路径已实现”，并同时列出尚未接入的页面能力、专用输入流程和真实浏览器验收缺口。现有 114 项用户注册能力与 134 项管理注册能力包含重复项，不能相加后宣称为独立功能覆盖数。

任务文档仍把阶段标为“基线核验与先复现后修复”，只记录原有 346 项测试基线，没有吸收本轮代码范围、最新日志、E2E 失败修复过程或当前阻塞。它不是本轮交付验收记录，应在最终候选提交确定后更新。

## 候选本地证据

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

未找到本轮证据证明：生产版本执行 migration 063、生产备份的隔离恢复、实际发布镜像/账本对齐、真实模型请求、普通 Safari 中小菱执行业务并由用户/管理员/其他账号验证权限隔离与持久化。它们仍是发布后验收门，不可由上述单测、模拟 E2E、HTTP 200 或旧生产论坛列表替代。

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

**边界复核结果：** 当前候选的事务边界修复通过三轮真实 MySQL helper/双 Session ACK 复验，也通过独立 token_version 与 RBAC 撤权测试。该项不再单独阻塞候选发布；候选仍未发布，真实 API 路由、普通 Safari、真实业务读回、生产备份恢复、镜像/ledger/version 对齐等发布门仍未通过或尚未执行。

## 生产只读快照

- `cr_backend` 与 `cr_frontend` 都是 healthy，镜像标签分别为 `prism-backend:22404e44…`、`prism-frontend:22404e44…`；容器内 `/healthz` 与 `/readyz` 都报告 `4.0.98 / 22404e44…`。
- 生产 `alembic current` 是 `062_audit_log_action_length (head)`；新候选本地 `alembic heads` 是唯一 `063_agent_browser_actions (head)`。这表示 063 尚未在生产运行。
- `https://lijiadong.cn/` 返回 HTTP 200；这只是旧生产首页的网络冒烟，不是小菱功能验收。
- 根文件系统最新快照约 87% 已用、`25,434,161,152` 字节可用（约 23.68 GiB），高于发布脚本默认 12 GiB 余量门槛。它只是一刻快照，发布时必须重新检查。
- 容器 Compose `working_dir` 为 `/opt/prism-releases/22404e44a05682e55740abe5a294da66761e2148/deploy`。当前账本为该目录下 `.releases/current.env`，`RELEASE_STATE_DIR` 未在 dotenv 中设置，因此脚本默认使用 `deploy/.releases`。该账本指向 22404、v4.0.98、Alembic 062；其上一发布是 4.0.97 / `c1243cf70b891fee18cb883b3b3b64523ed4679a`。`/opt/prism-release-state` 存在但不是运行 Compose 树所用的账本目录，勿把它当作当前状态源。
- 当前账本引用的 2026-10-08 备份文件存在，大小 822,398,152 字节，记录的 SHA-256 与文件当前 SHA-256 匹配。该检查没有完成 gzip 全量校验或隔离恢复，也不是新候选的发布备份/恢复证据。`cr_testdb` 容器存在并运行，但没有健康检查；本轮独立隔离验证未使用备份恢复。新增测试随后在该容器中创建并删除了唯一命名的临时 schema，未访问生产 `cr_mysql`。

## 发布前阻塞项与建议步骤

1. **先形成唯一候选。** 审阅并提交这 105 个代码/迁移/前端变更及相应验收文档；确认根目录 `VERSION`。当前为 `4.0.98`，与生产语义版本相同。提交完整 SHA 后再决定发布，不要用 86cf 文档提交代替实现候选。
2. **隔离 MySQL migration 与轮询边界已通过。** 063 在四个临时 schema 上 upgrade / 空表 downgrade / re-upgrade 通过，索引、LONGTEXT 大回执及 helper ACK/权限撤销场景均已核验。`downgrade()` 在账本表非空时会拒绝删除；应用回滚不自动降级数据库，必须保留此行为及表数据。
3. **准备完整制品和运行目录。** 以最终 SHA 生成 Git bundle，记录并在服务器重组后复核完整字节数与 SHA-256。将该 SHA 的干净源码、部署文件和既有生产 dotenv/证书/共享卷关系放入实际发布路径；保持 `.releases` 账本被新发布进程读取，核对真实 `working_dir`，不使用旧 `/opt/prism-release-state`。不把 dotenv 内容复制进报告或日志。
4. **重新检查资源和恢复门。** 发布前确认发布、Docker、备份目录各自仍至少有 12 GiB 可用且占用未触及 95% 阻断线；执行 `backup.sh --reason pre_deploy` 并对该次备份运行 `verify-backup.sh <备份文件>`，确认恢复目标为独立 `cr_testdb`，验证库不是 `cr_mysql`。不要用 10 月 8 日备份替代本次备份。
5. **仅用支持的精确发布入口。** 在干净的候选 SHA 工作树内从 `deploy/` 运行 `./deploy.sh all --revision <40位完整SHA>`；不要分开部署前后端，不手工 `compose up`。脚本会做备份恢复、容量、迁移、健康与 HTTPS 门禁；任一失败按脚本留存的 pending/previous 状态诊断，不自行清理。
6. **候选部署后逐项闭环。** 核对运行镜像摘要、`APP_RELEASE`、`APP_VERSION`、current/previous ledger、Alembic `current=head=063_agent_browser_actions`、health/ready、HTTPS 首页及全部新哈希前端资源。然后在普通 Safari 用受控测试账号真实点击，按普通用户/管理员及第二账号分别验证执行、当前账号/租约变更拒绝、高风险审批、真实业务读回、页面定位刷新、失败/排队不误报完成；关键确定性路径至少三轮。生产真实浏览器证据与本地 fixture 证据分开记账。
7. **保留回滚边界。** `deploy.sh` 的回滚只切应用镜像，不会 downgrade/restore 数据库。迁移后若新表已写入，063 的 downgrade 会拒绝删除非空账本；先评估上一应用对额外表的兼容性，不能为恢复旧应用而清除新账本记录。

## 独立复核阻断清单

- [ ] 代码与验收文档进入一个新的、干净、完整 SHA 候选；不能发布当前 86cfb1a9。
- [ ] 候选语义版本、构建镜像及前后端 `APP_RELEASE` / `APP_VERSION` 一致。
- [x] 隔离 MySQL 063 upgrade / 空表 downgrade / re-upgrade 通过，JSON 列为 LONGTEXT，大回执读回成功。
- [x] 新候选 MySQL `REPEATABLE-READ` 双 Session ACK 可见性：真实 helper 三轮通过；token_version/RBAC 撤销取消通过。
- [ ] 本次备份 checksum、gzip、隔离恢复与表/revision 检查完成。
- [ ] bundle 服务器重组后的字节数/SHA-256、容量、active ledger 路径通过。
- [ ] 完整 `all --revision <SHA>` 部署完成，运行镜像、release ledger、数据库 revision、资源与 health/ready/HTTPS 一致。
- [ ] 普通 Safari 的真实账号业务场景重复三轮；论坛 44/43/42 的旧生产只读列表不作为新候选证据。
- [ ] 更新 ACCEPTANCE、FINAL、TODO，明确哪些页面能力或专用输入仍未覆盖。
