# ALIGNMENT - 权限隔离与圆桌修复与MetaGPT编排

> 任务名：权限隔离与圆桌修复与MetaGPT编排
> 创建时间：2026-06-25
> 阶段：Align（对齐阶段）

---

## 一、项目和任务特性规范

### 1.1 项目特性
- **技术栈**：FastAPI + Vue3 + DeepSeek + Docker Compose + Caddy
- **部署形态**：腾讯云 81.70.251.90，域名 lijiadong.cn，Caddy 自动 HTTPS
- **Agent 架构**：14 个 BaseAgent + 5 个静态 ReviewAgentProfile 画像，双轨并存
- **编排架构**：Orchestrator（中心化调度）+ DiscussionOrchestrator（圆桌讨论）双轨，互不调用
- **数据隔离现状**：全部基于 `user.role == "admin"` 二分判断，无 project_member 关系表

### 1.2 任务特性
- **同步要求**：本地与服务器（81.70.251.90）双端对齐，git+docker 方式部署
- **不破坏约束**：新增 MetaGPT 编排层不能修改现有 Orchestrator/BaseAgent/AgentEventBus/AgentRegistry 的对外契约
- **服务器访问**：SSH root@81.70.251.90（22 端口），授权直接排查

---

## 二、原始需求

用户原始表述（逐条）：
1. "审查员界面是否是只显示有关于他工作的内容" → 需确认并实现审查员视角的数据隔离
2. "在服务器上圆桌讨论WebSocket连接失败,正在尝试重连...,请帮我修复" → 修复线上 WebSocket 故障
3. "管理员显示的agent中心和安全中心应该是所有账号的数据,普通账号能显示对应普通账号的数据" → 数据隔离确认/修复
4. "多agent概念可以用metagpt的概念去宏观调控" → 引入 MetaGPT 风格编排层
5. "所有内容同步更新本地和服务器对齐" → 双端同步

---

## 三、需求理解（对现有项目的理解）

### 3.1 WebSocket 故障现状
- 错误文案来源：`frontend/src/utils/discussionStream.ts:100` 的 `ws.onerror` 回调
- 前端链路：`buildWsUrl()` 用后端 preflight 返回的 `ws_url`，通过 `Sec-WebSocket-Protocol: prism-auth,<token>` 鉴权
- 后端链路：`backend/app/api/v1/ws_discussion.py` 优先从子协议读 token，校验 session 归属后 `websocket.accept(subprotocol="prism-auth")`
- Caddy 代理：`frontend/Caddyfile:15-17` 已配置 `handle /api/ws/* { reverse_proxy backend:8000 }`
- 历史记录：`docs/圆桌讨论WebSocket线上修复/FINAL_*.md` 显示曾修复并验证通过（101 Switching Protocols），**当前为回归故障**
- 待排查点：需 SSH 登录服务器查看 Caddy 日志、后端日志、docker 容器状态、证书有效性

### 3.2 数据隔离现状（关键发现）
| 模块 | 现状 | 是否需改造 |
|------|------|-----------|
| Dashboard 5 接口 | 已隔离（admin 全局 / user 自己） | 需改为按 project_member 可见项目过滤 |
| Project 列表/详情 | 已隔离（Project.user_id） | 需加 project_member 成员可见 |
| Review 任务列表/详情 | 已隔离（ReviewTask.user_id） | 需改为按可见项目过滤，成员可见同项目任务 |
| Issue 列表/详情 | 已隔离（ReviewTask.user_id） | 同上 |
| Agent usage/overview/runtime/situation | 已隔离（AiCallLog.user_id） | 维持按调用者过滤（AiCallLog 无 project_id） |
| **Agent events SSE** | **未隔离（全局广播）** | **需按 user_id 过滤事件，关键漏洞** |
| Security dashboard-summary | 已隔离（_project_ids_for_user） | 需加 project_member 可见项目 |
| Security checklist | 静态规则，无需隔离 | 不改 |
| Agent runtime/summary | 全局 Agent 计数，无业务数据 | 不改 |

### 3.3 project_member 表现状
- **不存在** project_member 表（全量 grep 0 命中）
- 现有归属仅靠 `Project.user_id`（owner）
- `frontend/src/utils/roleHome.ts:1` 已定义 `UserRole = 'admin' | 'reviewer' | 'user'`，但后端 `User.role` 实际只用 `admin`/`user`

### 3.4 MetaGPT 编排层接入点（调研结论）
- 现有 `Orchestrator`（中心化调度）与 `DiscussionOrchestrator`（圆桌讨论）**互不调用**，双轨割裂
- `DiscussionOrchestrator.start_discussion` 的双 for 循环（轮次 × 发言者）+ `all_turns` 共享历史，已是 MetaGPT `Environment.run(k_rounds)` + `Message` 黑板的雏形
- **推荐接入点**：新增 `backend/app/agents/environment.py`，通过 `BaseAgentRoleAdapter` 包装现有 14 个 BaseAgent，不修改任何现有类
- **关键约束**：双实例策略（get_orchestrator vs get_request_orchestrator）必须保留；AgentEventBus/DiscussionBus 双总线架构保留；DiscussionOrchestrator 的 WebSocket 控制协议（pause/resume/stop/user_input）是前端硬契约

---

## 四、边界确认（明确任务范围）

### 4.1 本任务包含
1. **WebSocket 修复**：SSH 排查 + 诊断 + 修复 + 部署验证
2. **数据隔离改造**：
   - 新增 `project_member` 表（project_id, user_id, role_in_project: owner/reviewer）
   - 改造 Project/Review/Issue/Dashboard/Security 接口，按 project_member 可见项目过滤
   - 修复 SSE 事件流泄露（Agent events 按 user_id 过滤）
   - 新增 project_member 管理 API（加入/移除成员）
3. **审查员界面隔离**：
   - 后端按 project_member 关系返回数据（无需前端角色判断改造）
   - Agent 中心、安全中心：admin 看全局，普通用户看自己的（维持现状 + project_member 扩展）
   - 审查任务页面：成员可见同项目任务
4. **MetaGPT 编排层**：
   - 新增 `environment.py` + `role.py` + `role_adapter.py` + `messages.py`
   - 通过 `BaseAgentRoleAdapter` 包装现有 14 个 BaseAgent
   - 提供 `Environment.from_discussion()` 工厂方法，DiscussionOrchestrator 保留作回退
   - 新增 `/api/agents/environment` 等观测接口
5. **双端同步**：本地 git commit → SSH 服务器 git pull → docker compose up -d --build

### 4.2 本任务不包含
- 不重构现有 Orchestrator 内部逻辑
- 不修改 BaseAgent.call 的 LLM 调用与重试机制
- 不修改 AgentRegistry.list_runtime 的前端契约
- 不删除 DiscussionOrchestrator（保留作回退）
- 不新增前端 project_member 管理界面（仅后端 API，前端管理界面后续单独实施）
- 不引入 Redis pub/sub 替换内存 EventBus（多 worker 部署后续单独实施）

---

## 五、疑问澄清（存在歧义的地方，已通过用户确认解决）

| 疑问 | 用户决策 |
|------|---------|
| 执行优先级 | 全部一起做，作为一个 6A 工作流交付 |
| "审查员"定义 | 按项目成员关系（owner/reviewer），新增 project_member 表 |
| WebSocket 排查方式 | 授权直接 SSH 登录 81.70.251.90 排查 |
| MetaGPT 实施深度 | 新增上层编排层，不破坏现有 orchestrator |
| 同步方式 | git+docker（本地 commit → SSH pull → compose build） |
| SSH 端口 | 22（默认） |
| project_member 实现 | 新增成员表（完整方案），含管理 API |

---

## 六、技术约束与集成方案

### 6.1 数据隔离改造约束
- 新增 `project_member` 表，字段：`id, project_id, user_id, role_in_project(owner/reviewer), create_time`
- 改造 `_project_ids_for_user` 通用函数：返回 owner 项目 ∪ member 项目的 ID 列表
- 写权限（update/delete project, delete/cancel task）保持"仅 owner 或 admin"
- 读权限（list/get project, list task, list issue）扩展为"owner 或 member 或 admin"
- SSE 事件流：在 `AgentEvent.payload` 标记 `user_id`，订阅时按 `payload.user_id == subscriber_user_id OR user.role == "admin"` 过滤

### 6.2 MetaGPT 编排层约束
- 新增文件：`environment.py`, `role.py`, `role_adapter.py`, `messages.py`
- `Role` 基类提供 `_react(message) -> Message` 接口
- `BaseAgentRoleAdapter` 包装现有 BaseAgent，转译 execute/scan 为 Action
- `Environment` 持有 `MessageQueue`，复用 DiscussionBus 模型
- `EventBridge` 把 Role 间消息流桥接到 AgentEventBus（前端 SSE 不受影响）
- 提供 `Environment.from_discussion()` 工厂，DiscussionOrchestrator 保留作回退

### 6.3 WebSocket 修复约束
- 不改变前端 `discussionStream.ts` 的子协议鉴权方式
- 不改变后端 `ws_discussion.py` 的控制协议（pause/resume/stop/user_input）
- 优先排查 Caddy 配置、证书、容器状态、后端日志
- 修复后需在服务器端验证 101 Switching Protocols

### 6.4 部署同步约束
- 本地改动通过 git commit 提交
- SSH 服务器执行 `git pull && docker compose up -d --build`
- 数据库迁移通过 Alembic 自动执行（新增 project_member 表）
- 保留回滚能力（git revert + docker compose down）

---

## 七、验收标准（初步，CONSENSUS 阶段细化）

1. **WebSocket**：线上圆桌讨论 WebSocket 连接成功（101 Switching Protocols），前端不再出现"连接失败,正在尝试重连"
2. **数据隔离**：
   - 管理员在 Agent 中心、安全中心看到所有账号数据
   - 普通用户只看到自己 owner 的项目 + 作为 member 加入的项目相关数据
   - SSE 事件流不再泄露他人任务进度
3. **审查员界面**：审查任务列表/详情页，成员可见同项目任务，不可见非成员项目任务
4. **MetaGPT 编排层**：新增 Environment+Roles 编排层，现有功能不受影响，圆桌讨论可走新编排层
5. **双端同步**：本地与服务器代码一致，docker compose 部署成功，所有服务 healthy
6. **测试**：新增 project_member 相关单元测试，现有测试不回归

---

## 八、下一步

进入 CONSENSUS 阶段，细化需求描述、验收标准、技术实现方案，生成共识文档。
