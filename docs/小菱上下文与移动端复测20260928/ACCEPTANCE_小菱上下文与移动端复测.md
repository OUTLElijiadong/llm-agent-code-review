# 小菱上下文与移动端复测：验收记录

## 初始状态

- 生产初始版本：v4.0.22，SHA `2fa4dbae0f7c7de3620c4f40330c0f67616bf2aa`；候选版本为 v4.0.23，发布前复核生产状态。
- 真实手机视口：Safari 响应式模式 `390×844`、2 倍像素比。截图显示悬浮小菱按钮盖住“安全态势”卡片右侧 0 值及“24h 恶意文件”文字。
- 语义缺口：基线 `test_c13_semantic_coverage.py` 中两个 strict xfail 分别覆盖沙箱来源 ID 齐全但漏事实、Responses 来源锚点齐全但遗漏中段权限。尚未改候选代码前需先运行基线测试确认。
- 百万级证据边界：已有约 1.1M 应用估算 tokens 合成史与模拟模型测试；不表示真实 tokenizer、生产模型或完整会话 UI 验收。

## 候选修复与本地验证

| 项目 | 状态 | 证据/限制 |
|---|---|---|
| 手机端 AdminCopilot 遮挡及 Teleport 路由竞态 | 候选已修复；生产复测待做 | 入口移入管理页顶栏。独立复核在 `/admin` 槽位尚未挂载时复现 Vue Teleport 告警和入口不迁移；修复为目标出现后再迁移、等待期间保留 body 浮动入口。组件级路由/MutationObserver 回归通过。Vitest 1381 passed；Playwright 49 passed、9 skipped（58 项，含 320–1440px 多视口）；lint/build passed。视口自动化不是发布后 Safari 真机验收。 |
| 沙箱压缩保护用户约束 | 已复现并修复 | 修复前 strict xfail 有明确漏事实样本；按来源保护用户硬约束，预算不足明确失败。后端全量测试已覆盖。 |
| Responses 压缩保护中段权限 | 已复现并修复 | 修复前 strict xfail 有明确漏事实样本；压缩后核验保护事实和来源。后端全量测试已覆盖。 |
| 小菱主聊天 >1M 估算 token 压缩 | 合成模型测试通过；真实模型待验 | 1,100 条混合历史经平台保守估算超过 1,000,000 tokens；模拟压缩验证首/中/尾权限和更正保留、次序以及原文不变。该证据不代表真实模型调用。 |
| 多入口上下文压缩修复 | 候选本地回归通过 | 涉及主聊天、Responses、圆桌追问、临时 Agent 团队、白盒 source_context、全链审计、正式审查附加上下文、黑盒 exploit 背景；均以原来源/用户约束做账本，超预算失败关闭。黑盒修复仅覆盖 exploit 的 probe/recon 与长 finding 背景，不代表 pentest 其他阶段已验。 |
| 全量后端测试 | 通过 | `backend/.venv311/bin/python -m pytest tests -q -o addopts=''`：5363 passed、5 skipped、5 warnings，174.68 秒；此后仅补充测试用例，C12/圆桌定向复跑 68 passed。 |
| 后端静态检查 | 通过 | 本次修改文件 Ruff `All checks passed!`；`compileall -q app alembic/versions/036_performance_indexes.py` 通过；`git diff --check` 通过。 |
| 生产发布与复测 | 尚未开始 | 需真实模型压缩、备份/恢复门禁、精确 SHA 发布和发布后真实浏览器矩阵。 |

## 尚需的发布后证据

- 真实供应商调用执行超过百万平台估算 token 的内存内压缩；记录模型返回的 usage、分层压缩结果、首/中/尾事实问答。绝不把 API Key、测试长文本或凭据写入日志/数据库。
- 发布后健康、ready、release SHA、Alembic、运维巡检、HTTPS、5xx 日志和备份验证。
- Safari 真实点击管理员、审查员、普通账号；核验账号隔离、会话保存及顺序、刷新/异常退出恢复、权限拒绝、页面布局和无 404。生产聊天记录按用户此前授权保留。
- 逐屏核对圆桌进度、后台继续运行、晚到消息是否写入讨论上下文/最终结果，以及结束后五分钟追问窗口。

## 独立终审新增发现（2026-09-29）

- 独立复核在 Vue 3.5.30 + jsdom 复现 `/admin` out-in 路由过渡时 Teleport 目标缺失；目标后来出现也不会自动修复入口位置。候选采用短时 `MutationObserver` 等待槽位出现，等待期间 Teleport 保持在 `body`；目标到位后迁移并停止观察，离开管理页也会停止观察。
- 新增真实组件回归覆盖非管理路由 → 移动端管理路由 → 页头槽位延迟挂载；检查按钮从浮动位置迁入槽位，且没有 Teleport 目标告警。更新候选全量 Vitest 为 1381 passed，前端 lint 与生产构建通过。
- 这是候选组件级证据；生产 Safari 移动视口重放尚未执行，不据此宣称生产 UI 已验收。

## 生产百万级语义压缩与反向隔离复测（2026-09-29）

### v4.0.26 生产发布

- 生产发布前版本 v4.0.25，SHA `a614f1109b3b4af3618e26e178c5cc80dd529722`；发布 SHA `98f52b80ff31cf365e1e89afe657473f8e6026d6`，版本 `4.0.26`。
- 修复 Responses 语义压缩中模型重复/乱序生成来源标记导致的拒绝：模型返回的来源 ID 与逐字引文仍严格验证；锚点标记由服务端按原始来源顺序重建。缺失/重复来源 ID、缺失或伪造逐字引文继续失败关闭。
- 本地全量后端：`5367 passed, 5 skipped, 5 warnings`（227.61 秒）；压缩器定向用例 `51 passed`；Ruff、`compileall`、`git diff --check` 通过。
- 发布脚本在 `cr_testdb` 隔离恢复验证通过（103 张表、Alembic `058_roundtable_sessions`），新建备份 `/opt/code-review/backups/code_review_20260928T211448Z_98f52b80ff31.sql.gz`，440,178,780 字节，SHA-256 `05a5dc5d8cda0c893a6974ea46909c2fb7b52154fdbcc84431d23cf91a65e4f1`。发布完成时间 2026-09-28 21:21:09 UTC。
- Backend 与 Frontend 镜像均绑定同一 SHA；Alembic `058_roundtable_sessions`；部署冒烟及独立巡检通过。`ops-check` 于 21:21:40 UTC 返回 `ok`：MySQL、Redis、ClamAV、Backend、Frontend 健康，磁盘 80%、内存 41%，备份 hash/gzip 有效，HTTP→HTTPS 为 308。公网 `/healthz`、`/readyz` 均为 200，返回版本 `4.0.26` 和上述 SHA；`/`、`/login`、`/admin/operations?section=overview` 均返回 200。

### v4.0.26 真实供应商百万级语义复测

两组均在生产 Backend 容器内使用生产当前模型配置 `deepseek-flash` 和原生 Responses SSE；合成对话仅驻留进程内 `InMemoryCheckpointStore`，未写入生产业务聊天/任务/项目数据。

| 场景 | 平台保守估算的原始对话 | 供应商真实调用与 usage | 来源完整性与召回 |
|---|---:|---|---|
| 原始单角色复现 | 320 条 × 3,000 字符；1,920,152 tokens | 43 次响应，其中 42 次语义压缩；输入 635,544、输出 23,156 tokens；全部 completed 且 usage 齐全 | 原始输入前缀逐字节保留；286 个压缩来源对应 286 个唯一标记；早/中/晚检索码都出现在摘要和最终答复 |
| 混合角色扩展 | 256 条 × 3,000 字符；1,536,289 tokens，user/assistant 混合 | 33 次响应，其中 32 次语义压缩；输入 499,176、输出 18,008 tokens；全部 completed 且 usage 齐全 | 原始输入前缀逐字节保留；222 个压缩来源对应 222 个唯一标记；三枚全新早/中/晚检索码都出现在摘要和最终答复 |

“tokens”是平台当前非 ASCII 按每字符 2 tokens 的保守确定性估算；它证明送测历史超过百万平台估算预算，**不是 DeepSeek 本地 tokenizer 计数**。表中供应商 input tokens 是分块压缩请求与末端回答的累计用量，分别为 635,544 和 499,176；不宣称单个 Responses 请求输入超过百万 tokens。每次 SSE 响应均 completed 且有 usage，压缩来源标记数量等于来源数、无重复，语义锚点在最终回答中精确召回。

### 发布后真实 UI 范围

- Safari 真实刷新并恢复已登录超级管理员 `outle`，移动视口 `390×844`、2 倍像素比。管理员工作台会话只显示管理员当前消息和回复；未出现之前普通账号/审查员验收标记。根页面和管理页路由正常加载，未观察到 404。
- 在该管理员会话内真实发送反向隔离问题。小菱回复当前会话未包含普通/审查员私聊内容，并承认本轮不足以证明全局隔离；同时回复中有一句“消息寻址范围被限定为当前账号”的架构判断，没有本轮工具或源码证据，故记录为**未核实的模型断言**，不作为隔离通过证据。
- 独立本地隔离回归：`test_private_agent_account_isolation.py` 与 `test_private_audit_log_isolation.py` 共 `68 passed`，覆盖私人 SSE、澄清、WebSocket、圆桌会话和私密操作审计详情，不因管理员身份读取他人私人内容。该回归证明受测服务契约，不替代每个生产账号的全部页面/API矩阵。
- 390×844 截图中的管理员页面与小菱对话未见横向溢出；长消息在气泡内换行并可滚动。此版本仅修后端，移动截图是单一页面的发布后抽查，不代表全站逐控件/全页面移动端验收。

### v4.0.27 生产发布与真实管理员复测

针对上述实测的无证据架构推断，在主控通用指令中增加“用户限定只看当前会话且禁止工具时，不得从账号、surface、工具说明或一般权限规则推断后端架构/账号隔离；无本轮证据只说明未知”的边界，并新增 admin/user 两个 surface 的指令回归。v4.0.27 于 2026-09-28 22:11 UTC 发布，正式发布门禁、健康及 HTTPS 冒烟通过。Safari 真实管理员会话重放后，小菱回答“当前会话无法证明”，明确撤回此前未经证实的寻址范围推断；这只验证回答约束，不构成账号隔离通过证据。

### v4.0.27 生产长上下文失败复现

- 生产模型 `deepseek-flash`、平台保守估算 `1,920,699` tokens、321 条消息（320×3,000 字符历史 + 最终查询）。第一次运行在 35 次 provider response 后失败；第二次同场景补充了逐次终态记录，在 11 次 provider response 后失败（completed 6、incomplete 5），供应商给出的不完整原因均为 `max_output_tokens`，逐次输出预算最高扩至 16,384 tokens。结束时 runtime error 为“语义压缩未完整结束”；没有输出半截最终答案。
- 两次运行的原始输入前缀均完整保留，摘要未提交检查点的业务持久层，合成消息和 InMemoryCheckpointStore 仅在进程内。第二次真实 usage 累计 input `238,717`、output `45,033` tokens；这不是单次输入量。8 条×3,000 字符的单块定向供应商请求独立 completed，output `909` tokens，说明截断由完整长历史多块运行触发，不能从单块成功外推。
- 根因范围已确认是长历史压缩响应达到供应商 `max_output_tokens` 上限，运行按 fail-closed 结束；长引文/摘要膨胀是实现层防护对象。修复候选增加短引文提示与服务端 64 字符校验，并在预算扩展重试仍截断后按完整来源集合递归拆块、按原序合并。
- 本地 runtime 定向套件 `53 passed`，Ruff、compileall、diff check 通过；新测试覆盖 overlong quote 被拒绝和多层输出截断拆块后来源标记完整。该结果仍是本地结构测试，v4.0.28 生产模型复测待发布后执行。

### 仍未覆盖

- 结束后五分钟可追问、后台圆桌消息进入上下文/最终结果，以及完整 reviewer/user/admin 页面、按钮、权限拒绝和异常退出 404 矩阵，本轮未重新逐项重测；沿用早前验收记录时需明确其版本和日期。
- 生产 Safari 只重载并检查管理员工作台单一移动视口；本次不是全站设计规范逐屏验收。
- v4.0.26 上第一轮压力脚本曾把成功回答追加到 checkpoint transcript 误当成输入被改写；修正为只比较原始输入前缀后，同一场景和混合角色场景均通过。首轮不用于负面产品结论或正式验收统计。
- v4.0.27 首次部署因 Frontend Dockerfile 中 Node `--max-old-space-size=1536` 堆上限耗尽而自动回滚至 v4.0.26。随后候选将构建堆上限提高为可配置默认 `2048` MiB；生产主机单独构建约 103 秒、`vue-tsc` 与 Vite 均通过。再次运行正式 `deploy.sh all` 成功发布 v4.0.27。新备份 `/opt/code-review/backups/code_review_20260928T220502Z_e001ecc4e17f.sql.gz`，420 MiB，SHA-256 `42b9e4a855edb4e1da9856dbc3e2eb5bb1c27d616ef4b002d8a1f034b1807073`；隔离恢复通过（103 表、`058_roundtable_sessions`）。
