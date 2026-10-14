# 修正版部署契约与证据范围独立复核

复核人：当前模型子代理；不是 Luna。首次源码检查时间：2026-10-13T14:54:55Z。工作树：`codex/sandbox-php-context`，首次检查时 HEAD 为 `8f719975ba9398f078d2204e02d56a385e7ee92c`（v4.0.99），未提交候选 `VERSION=4.0.100`。本轮不登录账号、不使用 GUI、不修改生产服务或业务数据。下文保留发布前源码判断，文末追加正式发布后的独立只读事实。

## 独立结论

发布后独立读取已确认 v4.0.100 / `6534493efbf7ec6eb0365b28019602cf8abd92d8` 正式运行，16 项发布基础核对均通过；独立匿名 HTTP 原错误类别三轮共 9 例均返回正确 JSON/状态。新备份完整校验与隔离恢复回执已核对。此结论限于发布身份和下述只读样本，不替代主代理执行中的三角色认证、普通 Safari、模型与业务持久化验收。Luna 没有完成生产点击。

发布前的候选 API 错误映射与部署环境字段修复源码语义成立，但单靠源码不能宣布发布或业务验收通过。v4.0.99 的发布基础门与实际 HTTP 业务验收属于不同证据：前者有发布回执，后者记录了 7 个失败样本，不能相互替代。Luna 的代码专项复核已形成报告，用户因 Luna 额度耗尽授权当前模型先完成真实点击，保留 Luna 待复核项。

## Nginx 契约

已逐一读取 `frontend/nginx.conf.template` 中 8 类代理入口：healthz、readyz、WebSocket、v1、代码/归档上传、头像上传、目录上传、通用 api。每类都有本层 `error_page 413` 与 `error_page 502 503 504`，并显式 `proxy_intercept_errors off`。依照 [Nginx error_page 文档](https://nginx.org/en/docs/http/ngx_http_core_module.html#error_page)，当前层存在 error_page 时不会继承父级映射；[proxy_intercept_errors 文档](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_intercept_errors) 说明关闭拦截时透传上游错误。由此推导候选会保留后端原始 HTTP 状态及 JSON，不再将后端 404/405 改成首页 200 HTML。Nginx 自身的容量拒绝改为 413 JSON，自身上游故障改为 503 JSON。

SPA `try_files ... /index.html` 与 server 级浏览页面回退保持原样。这个结论只覆盖已读的代理入口及本次修复，未宣称所有静态资源、畸形请求或全部 URI 的语义均已验证。`/assets/` 缺失资源的既有 HTML 回退未在本轮改动；不能把“保留 SPA 回退”扩写为“所有 HTTP 错误均 JSON”。

主代理的隔离 Nginx 回执 `evidence/nginx-contract-v4100.json` 记录 3 轮共 18 例，404/405/401/413/503 JSON 与 SPA 200 HTML 按预期通过。本子代理读取并核算回执，未执行这些隔离请求，也未将其当作生产前端验收。初读回执时 SPA 行的 `json_valid=true` 与 HTML 类型不相符，已交主代理纠正。主代理通过修正驱动后再次真实执行 18 项；本子代理复读最终回执，三条 HTML 的 `json_valid=null`、`body_matches_expected_contract=true`，并绑定测试时点、模板哈希及测试镜像 ID；不再存在字段混用。

## 部署身份链

候选 `deploy/docker-compose.yml` 在 backend 的显式 `environment` 中新增 `BACKEND_RELEASE`、`FRONTEND_RELEASE`，从受支持发布事务导出的同名变量取值。`deploy/deploy.sh` 先解析并核对完整 HEAD/SHA、版本与干净上下文，再显式导出 APP_RELEASE、APP_VERSION、BACKEND_RELEASE、FRONTEND_RELEASE；因此新显式字段能覆盖继承 dotenv 的旧标签，不依赖发布后才执行的 dotenv 校准。frontend 的镜像标签与 Vite 版本/SHA/构建时间仍来自该发布事务。

`stage_release.sh` 用当前活动 base SHA 校验账本、完整 bundle 字节数/哈希、解包 HEAD 与目标 VERSION，并继承活动目录的受保护环境与证书。脚本静态语法检查通过。静态检查不能代替最终镜像、容器四项环境键、ledger、migration、health/ready 与 served buildInfo 的发布后对齐；这些必须针对 v4.0.100 的最终提交再次读取。

## 已核对的证据与文档偏差

- `evidence/production/release-v4099.json` 明确标记 v4.0.99、SHA `8f719975…`、基础部署成功，但整体功能验收 pending。backend 两个旧非主环境字段有明确偏差记录，没有被隐去。
- `evidence/production/api-roles-baseline-v4099.json` 的 23 个 HTTP 场景为 16 通过、7 失败，顶层 passed=false；9 个现有聊天会话所有者/请求者组合读回样本通过，仅证明所列历史会话读回隔离，不能扩写为新对话写入、顺序、刷新持久化全覆盖。
- `evidence/final-local-tests.json` 明确 local_only，后端 351 项为 11 个定向文件，前端 146 文件/1897 项；不合并重叠回归数、不当作生产业务测试。
- `FRONTEND_LAYER_AUDIT.md` 明确本地 fixture、375/768/1280 与每视口 3 轮；没有声称所有生产页面、所有角色已通过。Luna/Safari 一节仍是待办。
- 初读时发现需更新的当前状态段：ACCEPTANCE 末段写“未提交/未发布/活动生产4.0.98”；ALIGNMENT 的“当前事实”实际是任务初始快照；INDEPENDENT_RELEASE_RECHECK 已追加 99 发布证据，但仍保留未标历史时点的“最后已读快照发生在发布前”和“任务文档仅346项”等旧表述。这些属于已确认的文档过时，不应当作新的生产漏洞；已通知主代理统一标注历史/当前时点。主代理随后已将 ACCEPTANCE 的发布门段更新为 99 基础门通过、100 修订和真实验收待办，其余最终文档仍以主代理完成后的版本为准。

## 本轮源码绑定

| 文件 | SHA-256（本轮读取时） |
| --- | --- |
| frontend/nginx.conf.template | `5d1a13020ab86d6a95b579f9a5f9fc4a55338ad5dc456ec29305bce533af8d2e` |
| deploy/docker-compose.yml | `aca149a3a876c88f5ec5eb73abbdef0747d529699be6b90fab1c42eccf5ee9c6` |
| deploy/deploy.sh | `7204bd9bb6eff2be4c782d5222a31326b644a1530d4dbb744d1467125cfe07a9` |
| deploy/lib/common.sh | `cf69530a9ba007dd31f7f86f8f7a8870d0f1314256c000bd7c082f1f43a85d47` |
| 本任务 stage_release.sh | `317fb10a2621e284f7659ea2ece1e52fc0744843473ae6f885a94a907d80bb89` |

## 发布前待补核计划（现已完成下述基础核对）

收到 v4.0.100 最终 SHA 与部署完成回执后，只读核对活动 Compose 目录/账本、前后端镜像 ID、四项 backend release 环境、health/ready、migration 和公网 buildInfo；核算最终 API 原失败场景及新增样本回执。普通 Safari 的点击与模型业务链由主代理执行，本子代理不能宣称自己或 Luna 完成了该生产复验。

## v4.0.100 已形成制品（发布前）

主代理通知的最终源码为 `6534493efbf7ec6eb0365b28019602cf8abd92d8`；本子代理已在工作树直接核对 HEAD 与该 SHA 一致。主代理通知 bundle 为 108,236,013 bytes、SHA-256 `14198bed3f61df3a3e3f7277c43bcf24eab62ffdae65a2353e50c472db825dd6`，服务器 staging 校验通过且受支持 all 发布已启动。这是发布前/进行中通知，本子代理尚未独立重算服务器 bundle，也尚未收到部署完成回执，不能据此宣布生产已切换。

## v4.0.100 发布后独立只读核对

主代理通知正式发布完成后，本子代理分别在约 2026-10-13T15:42Z、15:46Z 通过 SSH 直接读取运行态、文件完整哈希、发布日志和公网响应。可机读回执：[release-v4100.json](evidence/production/release-v4100.json)，采集开始时间为15:46:01.245094Z。以下结果均来自本子代理直接采集；发布恢复动作本身由受支持部署事务执行，本子代理复核其完整日志与产物，未另行恢复生产库。

| 核对项 | 独立实测 |
| --- | --- |
| Git / 活动 Compose / 账本 | HEAD、RELEASE_SHA、BACKEND_RELEASE、FRONTEND_RELEASE 均为 `6534493efbf7ec6eb0365b28019602cf8abd92d8`；APP_VERSION=`4.0.100`；Compose 目录为该 SHA 的 `deploy`，current ledger 位于同目录 `.releases/current.env`。 |
| Backend 四项环境 | APP_RELEASE、BACKEND_RELEASE、FRONTEND_RELEASE 均为目标完整 SHA；APP_VERSION=`4.0.100`。99 的旧非主字段残留已消除。 |
| 镜像 / 容器 | Backend 标签为目标 SHA，image ID=`sha256:af852f631d8add2ed76eeaf443093ba992edcf8634d75a2b5274627178529fb1`；frontend 标签为目标 SHA，image ID=`sha256:90c8b55b0064e9115dd18cf14d8351468078c6e30e3dd6080af6af2653602d15`；两者 healthy 且与 current ledger 一致。Backend 内容 ID 与99相同，backend源码未在100变动；同内容镜像复用不表示标签或运行身份漂移。 |
| 就绪 / 迁移 | 公网 healthz、readyz 均 HTTP200 JSON、v4.0.100/目标SHA；`alembic current` 为唯一 `063_agent_browser_actions (head)`。 |
| 公网构建信息 | 当前首页引用 `/assets/buildInfo-N4wkHPii.js`，HTTP200 JavaScript，251bytes、SHA-256=`269495acbb5a6dcc5b56b61c0342dc89f7c7e0e74047723682e45d3f037f55fb`；内容为4.0.100/目标SHA，构建时间15:08:18Z。 |
| 线上 Nginx | 运行镜像内模板 SHA-256 与本轮源码 `5d1a13020ab86d6a95b579f9a5f9fc4a55338ad5dc456ec29305bce533af8d2e` 一致；运行 `nginx -t` 成功。 |
| 完整制品 | 服务器 bundle 重算为108,236,013bytes、SHA-256=`14198bed3f61df3a3e3f7277c43bcf24eab62ffdae65a2353e50c472db825dd6`，完全匹配预期。 |
| 新备份完整性 | `/opt/code-review/backups/code_review_20261013T150837Z_6534493efbf7.sql.gz` 为831,479,698bytes；重算、sidecar与meta均为SHA-256=`2da025a44b4f387b29e11494a02e9af635a169aa5f12dc6b6177b4ba5b962374`；`gzip -t` 成功。 |
| 完整隔离恢复 | 发布日志15:11:48Z在 `cr_testdb` 创建唯一临时库，15:14:18Z记录105表/063完整恢复通过；meta为同表数与版本。本子代理另直接查询 information_schema，确认该恢复临时schema数量为0，已经清理。不是生产库降级或还原。 |
| 发布事务终态 | `/var/log/prism-deploy-v4100-6534493e.log` 记录15:17:41Z `all` 发布成功。current/previous ledger分别为100/99。 |
| 独立匿名 HTTP 重复样本 | 三轮分别 GET 未注册API路径、GET登录POST入口、GET需登录的browser pending：每轮404/405/401均application/json且可解析，共9例通过。本子代理没有用认证账号执行根的权限矩阵。 |

以上16项程序化核对均为true。ClamAV与decoy均healthy；ClamAV复用99创建的依赖容器，Compose创建标签仍指99，这是未变更依赖的容器历史，不能误称当前前后端发布树错误。根盘约89%使用，最终回执采样时可用22,394,343,424bytes，高于12GiB发布门；仅记录该时点容量，不声称长期容量已足够。

**保留范围：** 本子代理没有登录/GUI/模型调用或生产业务写入；全站逐页面业务、审批、聊天新内容写入/顺序/刷新持久化由主代理继续验收，且必须逐项记账。匿名原状态类别通过只证明Nginx契约的这组样本，不是完整权限矩阵或所有入口全通过。Luna生产点击待复核项保持未完成。

## 2026-10-14：生产点击新增问题与候选修复

主代理在普通 Safari 的 v4.0.100 真实账号点击中报告：圆桌菜单已浮在卡片上方（[真实生产截图](evidence/production/safari-user-roundtable.png)），外部点击可以关闭；连续 Escape 三轮及先取得 WebKit 键盘焦点后的复验仍未关闭。此生产 GUI 观察由主代理执行，本子代理没有使用生产浏览器。源码中的 `hasHigherDialogThanChoices` 原先只检查 dialog 自身的 display/visibility，没有确认布局矩形或 display:none 祖先；关闭后留在 DOM 的 Element Plus dialog 子节点可能因此错误挡住菜单 Escape。

### 先复现，再最小修复

- 新增单元回归，旧源码的 display:none 祖先内高层 dialog 连续三轮均未关闭菜单；独立零布局矩形样本也失败。初轮日志共6 failed，其中另2个 visibility 夹具错误地给后代显式 visibility:visible，却仍按不可见期待，后续已纠正，不把这2个夹具失败写成原生产缺陷。
- 最终可见性判断要求节点已连接、有非空 getClientRects、不在 hidden/aria-hidden/inert 祖先下；dialog 的最终计算 visibility 必须可见，且自身/祖先不能 display:none。按 [W3C CSS visibility 规范](https://www.w3.org/TR/CSS22/visufx.html#visibility)，visibility:hidden 可由后代显式 visibility:visible 覆盖，不能粗暴拒绝所有 visibility:hidden 祖先。新增此显式可见高层 dialog 保留优先权的红测，纠正初版候选的一次过度过滤后通过。
- 原真实可见高层模态仍阻止底层圆桌消费 Escape，不夺焦点；jsdom 此样本显式模拟非空布局矩形。隐藏 overlay 不再阻塞 Escape，关闭后焦点返回圆桌触发器。
- 主代理另发现圆桌房间缺少模态语义。新增 Panel 回归在旧模板失败，随后仅给 `.discuss-room` 增加 role=dialog、aria-modal=true、aria-label="Agent 圆桌讨论"；不改浏览器桥实现。桥接测试验证现有最高 dialog 范围能观察圆桌“关闭/追问”控件，并排除被遮挡 main 的发布按钮与保存提示。这个适配缺口不能写成已验证的越权或破坏性漏洞。

### 最终候选证据

| 验证 | 实际范围和结果 |
| --- | --- |
| 单元/组件 | GlobalDiscussionHost、AgentDiscussionPanel、siteBrowserBridge 3文件共68 passed；包含上述新增样本与已有真实高模态边界。`/tmp/prism-roundtable-visibility-final.log` SHA-256=`f930fa1691763f37e9b36dc50ffe001c43f2732f8b130910136807e624558e47`。 |
| 本地 fixture E2E | 普通持久 Chrome profile、所有 `/api/**` 为fixture；375/768/1280三个视口分别三轮，共3个测试通过（37.6s）。现在每轮加入真实 display:none overlay / display:block 子dialog结构后按Escape并核对焦点，不再点击触发器绕过此分支。每视口还验证真实可见5001层模态存在时菜单保留、不夺焦点。`/tmp/prism-roundtable-visibility-e2e-final.log` SHA-256=`c76af1a665bd4f8da7c053acb728278ce3b879e1e12a50cf05bd5de390b414ca`。 |
| 静态检查 | 6个修改前端文件定向ESLint通过；git diff --check通过。 |
| 未覆盖 | 本地Chrome及单测不能代替Safari；这次候选尚待主代理批量形成v4.0.101提交、发布并在相同生产账号/页面重跑Escape三轮和圆桌观察链。不会把v4.0.100基础门通过写成新增GUI缺陷已经生产修复。 |

本次仅改授权的 GlobalDiscussionHost.vue/test.ts、AgentDiscussionPanel.vue/test.ts、siteBrowserBridge.test.ts、overlay-layers.spec.ts，以及本独立报告。没有改浏览器桥实现、生产配置、权限或业务数据；原始生产和本地候选证据分开保留。
