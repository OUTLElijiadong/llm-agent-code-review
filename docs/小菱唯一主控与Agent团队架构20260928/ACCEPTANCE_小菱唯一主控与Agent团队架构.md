# 小菱唯一主控与 Agent 团队验收记录

## 代码与服务端授权复现

| 验收项 | 预期 | 实测 | 证据 | 状态 |
|---|---|---|---|---|
| 普通会话直接向子 Agent 发 `task.request` | 拒绝且无任务执行 | 已有回归测试覆盖 | `test_agent_mesh_service.py` | 通过（自动化） |
| 普通会话直接创建 Agent Team | 拒绝且无团队写入 | 已有 API 回归测试覆盖 | `test_agent_teams_api.py::test_agent_teams_public_create_requires_xiaoling` | 通过（自动化） |
| 隐藏的 monitor/operations 跨 surface 目标 | 目录、派发/领取与运行处理器均拒绝 | 已有负向与正向 RBAC 测试覆盖 | `test_agent_mesh_service.py`、`test_agent_mesh_dispatcher.py` | 通过（自动化） |
| 创建 Agent 的作者自己批准/驳回发布包 | 403，审批和 release 不变 | 修复前两种动作都返回 200；修复后两例通过 | `test_agent_release_decision_scope.py::test_agent_author_cannot_decide_own_release` | 通过（自动化） |
| 小菱身份 | 两个 surface 返回同一 `chat_assistant/小菱` | 测试已存在 | `test_surface_persona_separation.py` | 通过（自动化） |
| 主控别名不作为子目标 | `chat_assistant` 是唯一主控；`manager`/`orchestrator` 不可作为 Agent Team/Mesh 目标 | 运行目录过滤、Mesh 目标拒绝、唯一主控契约已复核；修复前 `agent:orchestrator` 返回 `needs_configuration` 而非明确的 `session_only`，新回归复现后加固并通过；旧管理员对话路由为 410 | `test_agent_runtime_v2.py`、`test_agent_mesh_service.py`、`test_agent_contracts.py`、`test_admin_copilot_api.py`、`test_agent_mesh_dispatcher.py::test_orchestrator_is_a_protected_session_only_target` | 通过（自动化，非生产模型验收） |
| 固定审查 Agent 引句与行号 | 每条问题必须有正整数行号；完整引文须逐字出现在当前源码分片，且该引文的实际起始行等于报告行号 | 修复前伪造证据、普通错行、行号为 0、重复首行多行引文错位、首尾填充空格均可被接受；修复后均拒绝，源码原样的缩进引文通过 | `backend/app/services/published_agent_tools.py`、`backend/tests/unit/services/test_published_agent_context.py` | 通过（候选服务端自动化） |
| 临时 Agent 引句与上下文压缩 | 发现须带逐字引句；代码引句与所报原始行一致；压缩片引文逐项核对原文；伪造引句或不完整压缩不得完成 | 伪造引句、错行引句、伪造压缩摘录和不完整压缩均失败关闭；压缩后真实尾部引文能生成完成态发现；裸 CR 行范围和行号验证通过 | `backend/app/services/temporary_agent_runtime.py`、`backend/tests/unit/services/test_temporary_agent_runtime.py` | 结构与引用校验通过；模型摘要是否完整理解语义仍不可由这些测试证明 |
| 团队并行/失败汇总/临时 Agent | 并行、作用域隔离、审计和结果结构有效 | 当前候选后端全量复跑 5284 passed、5 skipped、2 xfailed、6 warnings，251.35 秒，5291 collected；包含风险门与 MCP 绑定故障注入 | 下方“监督子 Agent 与风险门追加验收”；pytest 全量命令 | 通过（本地自动化；非生产模型验收） |
| 运维子 Agent 的层级上下文压缩 | 每个原始分片保留有序来源 ID 与可逐字核验引文；只做一轮摘要；最终输入仍超预算时必须失败关闭，不能继续让模型重写合并摘要 | 修复前的层级合并曾接受伪造引文，最终诊断会被调用；修复后移除二次模型摘要层，单轮结果仍超预算时返回 `semantic_budget_exhausted` 且不调用最终诊断。定向文件 3 passed；全量后端 5202 passed、5 skipped、2 xfailed、5 warnings（历史运行记录） | `backend/tests/unit/agents/test_operations_context.py`、`backend/app/agents/operations_agent.py` | 通过（本地自动化控制流；摘要语义真实性仍未能确定性证明） |

## 本地全量回归

| 检查 | 本轮结果 | 证据边界 |
|---|---|---|
| 后端全量 | **5284 passed、5 skipped、2 xfailed、6 warnings**（251.35 秒；5291 collected） | `/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/.venv/bin/pytest backend/tests -q --no-cov --disable-warnings --tb=short`；候选 worktree，本次包含风险监督、MCP 绑定与批准失效回归。只证明本地候选自动化，不代表生产运行态 |
| 证据/主控定向回归 | 本轮 95 passed、3.21 秒；独立复跑 95 passed、2.80 秒 | `/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/.venv/bin/python -m pytest backend/tests/unit/services/test_agent_mesh_dispatcher.py backend/tests/unit/services/test_published_agent_context.py backend/tests/unit/services/test_temporary_agent_runtime.py -q --no-cov`；候选目录，HEAD `cb14cd11e1a8e3383fef3a6374aa3a13345acb00` |
| 前端全量 | 122 个测试文件、1376 passed | Vitest；不代表真实浏览器可视验收 |
| 前端 lint / build | `npm run lint`、`npm run build` 均退出码 0 | 候选前端；构建产物未发布 |
| Python 静态检查 | Ruff、compileall 均通过 | 候选后端 |
| 差异检查 | `git diff --check` 通过 | 候选工作树 |
| Compose 配置解析 | `docker compose config --quiet` 退出码 0 | 使用 `.env.example` 占位配置；没有验证生产 `.env` |
| 部署脚本回归 | 发布绑定 19 项、部署失败恢复场景、运维执行器 15 项通过 | 本地脚本与单测；不代表已在生产主机执行发布 |
| Docker 镜像构建与环境文件边界 | 构建前后端候选镜像，且不向构建上下文传入 `.env*` | `.env*` 排除检查通过；前后端 `v4.0.22-candidate` 镜像均已在本机构建完成，镜像 ID 与字节数见本节追加验收 | 本地候选构建通过；未启动容器、未推送镜像、未部署 |

## 固定 Agent 生产流程

| 验收项 | 状态 | 证据 |
|---|---|---|
| 审查员真实创建草稿 | 已完成 | 审查员登录后通过 Agent Studio 真实点击提交 #247–#252，共 6 项；其中 #252 为 `auth_boundary_reviewer` v2，其余五项为首次发布 |
| 静态结构校验 | 已完成（仅结构） | 六项均通过 `input_schema`、`issue_schema`、`skill_graph` 契约检查；每项 0 Skill 依赖，申请能力为无；不是模型质量测试 |
| 管理员独立审批 | 暂缓发布 | 六项均为全站固定、只读代码审查 Agent；审批后可读取用户提交的代码，每个代码分片约新增一次模型调用。候选已加入服务端引文/行号校验，但生产最近可证版本仍是此前观察到的 v4.0.21、该校验未部署；当前不把六项发布到缺少此硬校验的生产运行时 |
| 发布后目录与版本核对 | 未完成 | 尚无本轮 release |
| 真实项目代码模型调用 | 未完成 | 发布后才执行 |
| 小菱发起真实多 Agent 团队 | 未完成 | 发布后用真实账号会话验证 |

## 风险与证据边界

- 反幻觉控制降低没有证据的错误结论，不可能保证模型零错误；必须看真实调用输出与源码引用。独立 Agent 二次复现确认行号为 0 与多行重复前缀错位两类旧漏洞现均拒绝；正确多行起始行、CRLF、裸 CR 均通过 helper 校验。
- 临时分析输出已加强为逐字 `evidence_quote`，源码定位还须核验引文确实出现在同一文件同一行；压缩摘要必须附可回查原文引句。固定审查 Agent 对完整引文的起始行做同样核验。该机制能拒绝伪造引用，不能证明模型对真实引句的解释必然正确。
- 任务语义到子 Agent 的总体路由规则仍由小菱遵循提示词；服务器硬校验账号、surface、会话/团队租约、Agent 可执行状态、输入与项目/文件范围，但尚无适用于所有自然语言任务的意图—Agent 语义一致性判定。本轮没有对真实对话执行系统性误路由样本，属于未覆盖风险。
- 两个旧“内测安全”固定 Agent 当前调用统计均为 4 次、成功 2 次、失败 2 次。生产卡片没有呈现失败详情，失败原因未知；本任务未覆盖也未覆盖它们。
- 工坊的“通过”是结构/能力校验，不是模型效果验收。
- 审批列表显示每个固定 Agent 每个代码分片增加约一次模型调用；界面没有展示绝对费用，当前不能报出金额。
- 一次新建前端 Agent 时首个结构检查遇到网络错误，未落地重复草稿；重试后只存在一份且检查通过。
- 公开候选分支的隐私门禁：GitHub API 在 2026-09-28 确认 `codex/v4.0.4-production-fixes` 指向 `cf697d0048ce0bcccb77e2aef04512701921df7f`；该远端提交树中两份文件（圆桌验收与账号角色对齐文档）含审查员手机号，共 2 处。当前本地工作树 4 处已脱敏，精确扫描为 0；本地 HEAD 历史快照和相对远端的 13 个未推送提交仍留有旧文本。没有推送。历史清理需单独确认强制更新，且不能保证 GitHub 缓存、克隆或派生副本同步删除。
- 2026-09-28 只读 `healthz` 与 `readyz` 均返回 HTTP 200，确认生产版本/release 为 v4.0.21 / `f5f8855b0d54ba28ded201055078713aa9df8c24`。候选源码版本 v4.0.22。此前 Safari 辅助功能树曾显示旧运维入口，但没有随验收件留存截图；本轮未复核该入口。候选已把管理员对话统一为小菱，但未部署。
- 旧 `admin_copilot_service` 与 `AdminCopilotAgent` 没有生产 API 或 UI 调用者；其公开旧 API 固定返回 410，当前管理页通过 Responses 接口调用小菱。该 Python 模块仍可被内部代码直接导入并触发模型规划，且旧工具功能是否都由新链路等价承接尚未逐项审计，因此本轮保留并标为待治理的死路径，不宣称仓库内已完全清除旧管理 Agent 实现。
- 生产审批列表里已有 #239 `auth_boundary_reviewer` v1；本轮没有替换其已发布版本。候选修改不等于生产代码已生效。
- 发布制品必须来自已通过上述全量验证的完整候选提交，并核验精确 SHA；生产只读健康检查仍为 v4.0.21，候选尚未部署。

## 管理员真实界面逐项复核（2026-09-28）

此前以 Safari 中 `outle 超级管理员` 会话只读打开六个待审批发布包；未点击批准或驳回。主代理逐项通过 Safari 辅助功能树读取包详情、职责提示词、模型参数和结构检查结果；未留存截图，因此现场 UI 细节不能从此验收件独立重放。曾有独立只读队列交叉核对，但没有归档其任务标识或报告，不据此宣称独立结论可复核。本轮再次打开生产登录页时，浏览器 CUA `getTab` 在 30 秒后超时，未刷新审批队列，也未执行生产点击；本段列表状态沿用此前只读观察。六个包在此前观察时均为高风险、0 Skill 依赖、无额外能力申请，静态契约检查通过；管理页面对每个包标注每个代码分片约增加一次模型调用，未展示绝对费用。

| 审批单 | 真实 UI 中核对的职责与约束 | 结果 |
|---|---|---|
| #252 鉴权与账号隔离审查员 v2 | 核验角色、资源、会话隔离和审批版本绑定；要求真实路径/行号和触发条件；禁止未验证结论 | 只读复核；待新运行时部署后再批准 |
| #251 Agent 编排审查员 v1 | 核验主 Agent 身份、子 Agent 路由、临时 Agent 生命周期、团队 DAG/并行/失败汇总和压缩完整性；提示词禁止执行操作 | 只读复核；待新运行时部署后再批准 |
| #250 数据完整性审查员 v1 | 只看输入的模型/迁移/事务/序列化证据；明确不访问数据库、网络、命令或修改代码 | 只读复核；待新运行时部署后再批准 |
| #249 前端交互审查员 v1 | 检查状态/竞态/错误态/窄屏/对齐/焦点；明确不访问网络、调用工具或修改代码 | 只读复核；待新运行时部署后再批准 |
| #248 后端业务逻辑审查员 v1 | 检查状态机、不变量、事务和异常分支；排除鉴权专项重复；明确只读且不联网、不执行命令 | 只读复核；待新运行时部署后再批准 |
| #247 可靠性审查员 v1 | 检查超时/重试/幂等/租约/队列/取消/恢复和观测证据；禁止写操作、外联和修改代码 | 只读复核；待新运行时部署后再批准 |

上述六项是代码审查专员，不包含运维 Agent。候选里已存在内置 `operations` 子 Agent，无需再造一个：服务端将它限制在唯一超级管理员的 admin surface；团队中的运维任务只读；直接写操作需通过小菱的结构化运维工具和既有审批链。相关权限/审批有自动化测试覆盖，本轮没有在生产执行运维写操作。此前 Safari 辅助功能树曾显示旧“贾维斯 · 全局运维”入口，但没有随验收件留存截图，本轮未重新核验，也未点击；不据旧入口标签推断其能力。候选把主控身份和管理对话统一到小菱，但生产尚未部署。

## 发布状态与复核边界

运维压缩修复代码提交为 `080aaeadc856c02b3addeca76a924d27fa3d6363`，前端构建上下文忽略规则提交为 `cb14cd11e1a8e3383fef3a6374aa3a13345acb00`；`867905d` 是此前全量验证代码基线。候选源码版本仍为 `4.0.22`。此前 GitHub API 观察到候选远端提交树及本地未推送历史含审查员手机号；本轮未推送或改写历史。Docker Hub 拉取曾超时，但本轮使用本地缓存完成前后端镜像构建。生产 SSH 公钥认证此前失败，本轮未重试。没有部署；本轮只读调用生产 `healthz`/`readyz`，未调用生产写 API，也未执行固定 Agent 的生产模型效果验收。Safari 管理 UI 只读打开审批列表不算生产 Agent 调用。上述本地自动化通过结果不能替代生产验收。

## 黑白盒、修复与渗透链路追加验收（2026-09-28）

| 验收项 | 修复前复现 | 候选验证结果 | 状态与边界 |
|---|---|---|---|
| 内嵌 runner 黑盒首页状态码 | 本地服务对 `/` 返回带正文的 404/500 时，旧逻辑可能只凭正文判通过 | 真实启动本机临时 HTTP 服务：200 通过；404、500 均返回失败。`test_deploy_verify_runner_regressions.py::test_embedded_blackbox_runner_requires_success_http_status` | 通过（本地 shell runner 回归；非生产 HTTP 探测） |
| Go 白盒静态检查 | 模拟 `go vet` 退出码 7 曾被 `|| true` 吞掉 | 用伪造 `go` 可执行程序返回 7，runner 现在返回失败并记录 `go vet: static analysis failed`；回归用例通过 | 通过（故障注入；没有在生产 Go worker 上执行） |
| 小菱团队调用 TestVerifier | 若未把组合测试模式和修订号传入，可能只跑默认/错误源码 | 定向 dispatcher 测试验证团队任务使用 `test_mode=combined`、传递 `source_revision_id=41`，并核对 `source_sha256` 返回字段 | 通过（团队路由契约的 mock 自动化；未启动真实 worker，也未完成真实模型团队会话） |
| 远程黑盒目标授权 | REST 不能由请求体布尔值自称已确认；跨账号、项目、目标、模式和重放都必须拒绝 | REST 创建必须消费服务端按账号/项目/完整 URL/模式绑定、5 分钟有效、只能消费一次的票据；直接布尔值、跨账号、换项目/目标/模式、过期和重放均被测试拒绝。Agent Team 有精确参数的当前调用点击审批，但获批后仍把 `remote_target_authorized=true` 写入团队任务，内部沙箱不消费同一票据；重试范围/票据统一性未验 | 部分通过（候选自动化；外部目标未探测） |
| 修复源码后复测 | 子 Agent 直接写源码会混淆提案、审批与执行责任 | 小菱提示词要求临时修复 Agent 只读给出锚定补丁，小菱经 `code_files.update` 与审批写入，随后重读新版本并交给 TestVerifier 复测、核对哈希；当前测试只核对编排约束，未作真实模型端到端修复 | 部分通过（安全流程被明确；真实模型修复闭环待验） |
| 正式渗透测试 | 远程黑盒 smoke 不等于有授权的渗透测试 | 小菱只能创建委托草稿；启动前须权限检查、当前会话确认，并经 `/pentests` 范围/时窗授权及独立审批；既有 `test_pentest_service.py` 覆盖未授权不能启动 | 自动化边界通过；没有创建生产委托或发起主动探测 |

### 监督子 Agent 与风险门追加验收（2026-09-28）

- 用户确认的策略为：低风险和中风险自动放行；高风险必须询问当前账号。未知动作和监督链故障按拒绝处理。此处记录候选实现与自动化证据，不代表生产已经部署。
- 复用仓库已有 `agent:supervisor` 只读子 Agent 身份；工具入口、Agent Team 任务执行前后均调用同一确定性监督规则服务。模型不能自行声明风险级别或授予授权。团队创建审批绑定计划摘要、发起账号和高风险任务指纹；任务执行时由监督规则再次检查并核对精确指纹。子任务完成还要有结构化证据，缺证据不合并为成功。
- 风险处理：注册只读能力为低风险，已登记的项目级可逆写入/隔离测试与沙箱生命周期操作为中风险；项目删除、角色/权限、生产与外部目标、正式渗透、发布以及未登记能力为高风险或更高，要求当前账号确认。现有 RBAC、目标范围和专用审批仍独立生效，监督器不会覆盖拒绝策略。
- MCP 已登记只读源码查询可自动执行；项目隔离沙箱白名单中的创建/部署/关闭/延期自动执行。未知 MCP、外部 MCP、浏览器黑盒、声明需确认或高风险的能力必须确认。审批绑定端点摘要、认证头摘要、工具/Schema、权限和策略指纹；凭据不以明文进入审计。审批等待中绑定变化或工具被停用会使旧审批失效。
- 监督分类/绑定读取出错时写入拒绝结论并停止工具；监督审计不能持久化时也停止副作用。监督事件按账号、运行和调用恢复，不展示其他账号的同类审计记录。
- 独立复核指出一项未测风险：团队任务的 `supervisor.after` 持久化发生在子 Agent 已执行之后；若此时数据库写入失败，无法回滚已发生的外部副作用，也尚无专门的故障注入/独立补偿审计验证。调度器会尝试把任务记为失败，但不能据此声称该故障下后置审计完整或副作用可撤销；作为待办保留。
- 先前复现的 3 个默认风险放行问题已修复；针对性回归复跑 **327 passed**（包含风险表、团队审批、MCP 绑定变化、工具停用、监督异常 fail-closed、权限撤销和账号边界）。其中定向测试曾发现普通用户能力注册表符号名错误，修正后完整复跑通过。随后全量首次发现 2 个旧 MCP 测试替身未提供绑定详情；补齐替身契约并追加监督绑定故障注入后，最终后端全量为 **5284 passed、5 skipped、2 xfailed、6 warnings**（5291 collected，251.35 秒）。Ruff 与变更模块 `compileall` 通过。
- 这不是“零幻觉”证明：监督器检查服务端能力登记、权限、目标、审批和证据结构；不会确定性证明模型解释或摘要在语义上绝对正确。模型真实调用、生产真实点击、1M+ token 真实供应商用量与移动端仍需分别验收。
- `test_verifier` 可由小菱团队执行 `combined` 并携带同项目 `source_revision_id` 到隔离沙箱；dispatcher 自动化证明参数路由与 SHA 字段，但不证明真实 worker/模型运行。正式渗透仍须明确范围、有效时间窗和独立审批；黑盒 smoke 不等于渗透。

本轮定向回归命令共 18 passed、1 warning（17.92 秒）：`pytest tests/unit/services/test_surface_persona_separation.py tests/unit/services/test_deploy_verify_runner_regressions.py tests/unit/services/test_agent_team_dispatcher.py::test_test_verifier_team_runs_combined_whitebox_blackbox_on_requested_revision tests/unit/agents/test_sandbox_agent_context.py::test_test_verifier_description_does_not_overclaim_real_penetration -q --no-cov --disable-warnings`。`prism-backend:v4.0.22-candidate` 镜像 ID `sha256:9a2b51f251cb8fc6ceec37c241f630b15f6826cecc4ed3eed9e9e413a1ece4d1`、149,886,239 字节；`prism-frontend:v4.0.22-candidate` 镜像 ID `sha256:83e617e62e88f04eb4e7b6a3829ff57c00d3d58c56e0d9bdfd4f690f4c305f74`、30,120,884 字节。只构建了镜像，没有运行或发布。

生产只读 `healthz` 与 `readyz` 均返回 HTTP 200；响应版本和 release 为 `4.0.21 / f5f8855b0d54ba28ded201055078713aa9df8c24`。本轮没有生产登录态点击、Agent 模型调用、远程黑盒请求或渗透测试，因此本地候选通过不等于生产功能已验收。
