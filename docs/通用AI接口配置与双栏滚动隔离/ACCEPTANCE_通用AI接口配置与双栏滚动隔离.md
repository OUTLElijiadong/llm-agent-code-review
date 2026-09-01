# Acceptance：通用 AI 接口配置与双栏滚动隔离

## 验收结论

本次通用 AI 配置、模型发现、可恢复错误处理和管理端双栏滚动隔离已完成，并发布到公网生产。配置页现在覆盖截图中的全部字段；系统默认密钥可在同端点安全用于模型发现和连接测试，停用或失效的历史覆盖不会遮挡默认配置，也可以由管理员明确清理。生产发布未改动业务数据。

## 需求验收矩阵

| 需求 | 证据 | 结果 |
| --- | --- | --- |
| 配置字段完整 | provider、active、Base URL、API Key、模型、超时、最大重试、温度、保存、测试和拉取模型均在 `LlmConfig.vue` | 通过 |
| 拉取模型 | 生产真实调用 DeepSeek `/models` 返回 3 个去重模型，`attempts=1`、`fallback=false` | 通过 |
| 连接测试 | 生产真实 Chat Completions 测试成功，模型 `deepseek-v4-flash`，`856ms`、`attempts=1` | 通过 |
| 上游错误回退 | 401/403/4xx 快速返回；429/5xx/超时有限重试；404/405/501 保留手工模型；响应提供 `retryable` 与 `next_action` | 通过 |
| 配置异常回退 | 停用、密钥不可解密、JSON 损坏或字段不完整时选择系统默认并返回 `fallback_reason`；同端点才复用系统 Key | 通过 |
| 密钥安全 | 前端只提交新输入或 `undefined`，服务端加密保存；响应/日志只显示掩码，生产日志近期错误样本为 0 | 通过 |
| 停用覆盖清理 | “恢复系统默认”对 `inactive`、凭据失效和历史坏配置均可见，确认后发送 `active=false, api_key=''` 清除旧密文 | 通过 |
| 左右滚动隔离 | `AdminLayout` 固定 `100dvh` 外壳，侧栏与内容区各自滚动，禁止滚动链传递；路由切换自动将内容回顶 | 通过 |
| 人机继续操作 | 加载、拉取、测试、保存和恢复失败均保留输入并显示下一步动作；取消确认不改变状态 | 通过 |

## 自动化验证

| 验证项 | 结果 |
| --- | --- |
| 后端全量 pytest | `2164 passed`，静默终端退出码 `0` |
| 前端全量 Vitest | 67 个测试文件、`394 passed` |
| 前端生产构建 | 3682 个模块转换，通过 |
| 前端 ESLint | 通过；仅有既有测试环境 Vue 注入提示，不影响退出码 |
| 后端 Ruff | `ruff check app tests` 通过 |
| 发布脚本测试 | `deploy shell and operations tests: PASS` |
| 补丁完整性 | `git diff --check` 通过 |

新增/强化的关键回归包括：模型列表空/异常/不支持、瞬时错误重试、端点 SSRF 与末端路径归一化、坏密文回退、同端点密钥复用、停用旧密钥不误用、停用覆盖清理、表单失败保留、模型拉取不发送掩码值、管理布局滚动契约。

## 生产证据

- 公网入口：`https://lijiadong.cn`。
- 版本与发布提交：`3.8.3 / 4b1711adf0e79a95eed35be3ab17605fe10869cf`。
- 发布时间：`2026-09-01T19:26:58Z`。
- `GET /healthz` 与 `GET /readyz` 均返回 `status=ok/ready`，版本和提交一致；HTTP 入口返回 308 并强制 HTTPS。
- 后端、前端、MySQL、ClamAV、Redis 均健康；MySQL `restart=1`（发布前既有计数）、`OOM=false`，发布后未新增重启。
- 生产数据库 89 张表，Alembic `046_finding_aggregation`；本次新建的隔离验证库已清理，历史 `prism_verify_20260819111518_18935` 按既有授权保留且未改动。
- 发布备份：`/opt/code-review/backups/code_review_20260901T192255Z_4b1711adf0e7.sql.gz`，约 382 MB，同时生成 `.sha256` 和 `.meta`；恢复验证在 `cr_testdb` 完成。
- 发布后后端最近 10 分钟 `error/traceback/critical/exception` 计数为 0。
- 受控清理复核：`cleanup.sh --apply` 已完成，当前/上一版镜像、数据库卷、备份和发布账本均保留；Docker 本轮新增回收 `0B`，根分区当前 `85%`（约 29 GB 可用），`ops-check` 标记为 `degraded` 但 `can_continue=true`、`blocking_checks=[]`。
- 系统默认有效配置来自 `https://api.deepseek.com`；掩码状态为已配置，真实模型发现返回 `deepseek-v4-flash`、`deepseek-v4-pro`、`deepseek-v4-flash-vision-exp`。
- FlyTrap 两个 systemd 服务均为 `inactive/disabled`，Prism 与运维开关均为 `false`，近期后端无 FlyTrap 重试日志；退役材料仍保留。

## 浏览器验收边界

生产浏览器已确认页面会先要求登录，并未使用或猜测管理员凭据；因此本轮无法在真实登录态完成“滚动后点击大模型配置”的视觉点击录屏。该项由 `AdminLayout.test.ts`、`LlmConfig.test.ts`、生产前端构建和路由回顶代码共同验证，不能把未登录点击写成已通过。获得正常管理员登录态后只需复核侧栏滚动、内容滚动和配置页首屏即可，不需要改代码或数据。

## 回滚与数据边界

- 生产使用同一提交构建后端/前端，上一发布账本仍可由 `deploy/rollback.sh` 回退。
- 本次没有执行生产数据库写入、没有清除历史退役材料、没有回显任何 API Key。
- 用户原有未跟踪文件 `.coverage 2` 未纳入提交；本地测试生成物已与代码隔离。
