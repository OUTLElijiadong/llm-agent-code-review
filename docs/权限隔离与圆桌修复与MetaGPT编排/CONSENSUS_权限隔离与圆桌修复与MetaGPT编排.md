# CONSENSUS - 权限隔离与圆桌修复与MetaGPT编排

> 任务名：权限隔离与圆桌修复与MetaGPT编排
> 创建时间：2026-06-25
> 阶段：Align（对齐阶段）- 共识文档
> 前置：ALIGNMENT_权限隔离与圆桌修复与MetaGPT编排.md

---

## 一、需求描述（最终确认）

### 1.1 WebSocket 线上修复
线上服务器（81.70.251.90 / lijiadong.cn）圆桌讨论功能出现 "WebSocket 连接失败,正在尝试重连..." 故障。需 SSH 登录服务器排查 Caddy 日志、后端日志、docker 容器状态、证书有效性，定位根因并修复，恢复 101 Switching Protocols 握手成功。

### 1.2 数据隔离改造（管理员全局 / 普通用户按项目成员关系）
- **管理员**：Agent 中心、安全中心、仪表盘、项目列表、审查任务、问题列表均显示所有账号数据
- **普通用户**：只显示自己作为 owner 的项目 + 作为 reviewer 被加入的项目相关的数据
- **新增 project_member 表**：建立项目-成员关系（owner/reviewer）
- **修复 SSE 事件流泄露**：Agent events SSE 按用户过滤，不再广播全局事件

### 1.3 审查员界面数据隔离
- 审查任务列表/详情页：成员可见同项目任务，不可见非成员项目任务
- Agent 中心：admin 看全局 Agent 调用统计，普通用户看自己的调用统计（维持现状）+ project_member 扩展
- 安全中心：admin 看全局安全态势，普通用户看自己可见项目的安全态势

### 1.4 MetaGPT 风格编排层（新增上层不破坏现有）
- 新增 `Environment` + `Role` + `Message` 抽象层，位于现有 Orchestrator 之上
- 通过 `BaseAgentRoleAdapter` 包装现有 14 个 BaseAgent，零修改现有类
- 提供 `Environment.from_discussion()` 工厂方法，圆桌讨论可走新编排层
- `DiscussionOrchestrator` 保留作回退，不删除
- `EventBridge` 桥接 Role 间消息到 AgentEventBus，前端 SSE 不受影响

### 1.5 双端同步
- 本地改动通过 git commit 提交
- SSH 服务器执行 `git pull && docker compose up -d --build`
- 数据库迁移通过 Alembic 自动执行

---

## 二、验收标准（具体可测试）

### 2.1 WebSocket 修复验收
| 验收项 | 验证方法 | 通过标准 |
|--------|---------|---------|
| WS 握手 | `curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: test" -H "Sec-WebSocket-Protocol: prism-auth" http://81.70.251.90/api/ws/discuss/probe_subprotocol` | 返回 101 Switching Protocols |
| 前端连接 | 浏览器登录后发起圆桌讨论 | 不再出现"连接失败,正在尝试重连" |
| Caddy 日志 | `docker logs cr_frontend --tail 50` | 无 WS 代理错误 |
| 后端日志 | `docker logs cr_backend --tail 50` | 出现"[WS] 讨论连接已接受" |

### 2.2 数据隔离验收
| 验收项 | 验证方法 | 通过标准 |
|--------|---------|---------|
| project_member 表 | `docker exec cr_mysql mysql -u root -p$MYSQL_ROOT_PASSWORD $MYSQL_DATABASE -e "DESC project_member"` | 表存在，字段完整 |
| 管理员视角 | admin 登录查看 Agent 中心/安全中心/仪表盘 | 显示所有账号数据 |
| 普通用户视角 | user 登录查看 Agent 中心/安全中心/仪表盘 | 只显示自己 owner + member 项目数据 |
| 成员可见项目 | userA 创建项目，userB 被加入为 reviewer，userB 查看项目列表 | 可见 userA 的项目 |
| 非成员不可见 | userC 未被加入 userA 项目，userC 查看项目列表 | 不可见 userA 的项目 |
| SSE 隔离 | userA 发起审查，userB 订阅 /agents/events | userB 不收到 userA 的事件 |
| 写权限 | userB（reviewer）尝试修改/删除 userA 的项目 | 403 Forbidden |

### 2.3 审查员界面验收
| 验收项 | 验证方法 | 通过标准 |
|--------|---------|---------|
| 审查任务列表 | reviewer 登录查看审查任务列表 | 可见自己 owner + member 项目的任务 |
| 审查任务详情 | reviewer 访问同项目任务详情 | 200 OK，可查看 |
| 跨项目隔离 | reviewer 访问非成员项目任务详情 | 404 NotFound |

### 2.4 MetaGPT 编排层验收
| 验收项 | 验证方法 | 通过标准 |
|--------|---------|---------|
| 新增文件 | 检查 environment.py/role.py/role_adapter.py/messages.py | 文件存在，import 无误 |
| 现有功能不回归 | 运行现有测试 `pytest backend/tests/` | 全部通过 |
| Environment 工厂 | 调用 `Environment.from_discussion(session_id)` | 返回 Environment 实例 |
| 圆桌讨论走新层 | 通过新编排层发起圆桌讨论 | 讨论正常进行，WebSocket 控制协议不变 |
| EventBridge | 圆桌讨论过程中前端 SSE | 工位卡点亮正常 |

### 2.5 双端同步验收
| 验收项 | 验证方法 | 通过标准 |
|--------|---------|---------|
| git 一致 | 服务器 `git log -1` 与本地一致 | commit hash 相同 |
| docker 部署 | `docker compose ps` | cr_backend/cr_frontend/cr_mysql 均 Running |
| 健康检查 | `curl http://81.70.251.90/healthz` | 200 |
| 数据库迁移 | `docker exec cr_backend alembic current` | head 版本 |

---

## 三、技术实现方案

### 3.1 数据库层（新增 project_member 表）
```sql
CREATE TABLE project_member (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    project_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    role_in_project VARCHAR(20) NOT NULL DEFAULT 'reviewer' COMMENT 'owner/reviewer',
    create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_project_user (project_id, user_id),
    INDEX ix_pm_user (user_id),
    INDEX ix_pm_project (project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```
- 通过 Alembic 迁移创建表
- 现有项目的 owner 自动写入 project_member（role_in_project='owner'）作为数据迁移

### 3.2 通用可见项目过滤函数
新增 `backend/app/services/project_member_service.py`：
```python
def get_visible_project_ids(db: Session, user: User) -> tuple[list[int], str]:
    """返回当前用户可见的项目 ID 列表 + 范围标识
    admin → 全局项目，scope='global'
    非 admin → owner 项目 ∪ member 项目，scope='self'
    """
```
改造以下 service 的过滤逻辑：
- `project_service.list_tasks` / `get_project` / `update_project` / `delete_project`
- `review_service.list_tasks` / `get_task_detail` / `list_task_issues` / `start`
- `issue_service.list_issues` / `get_issue` / `update_status`
- `dashboard_service._scope_filter` / `_valid_task_ids`
- `security_service._project_ids_for_user`

### 3.3 SSE 事件流隔离
改造 `backend/app/api/v1/agents.py` 的 `/agents/events` 端点：
- 订阅时传入 `user_id`
- `AgentEventBus` 的 `subscribe` 方法增加可选 `user_filter` 参数
- 发布事件时在 `payload` 中标记 `user_id`（由 `review_service._emit_review_event` 填充）
- 订阅端 yield 前过滤：`event.payload.get('user_id') == user_id OR user.role == 'admin'`

### 3.4 project_member 管理 API
新增 `backend/app/api/v1/projects.py` 的子路由：
- `POST /projects/{id}/members` - 加入成员（仅 owner/admin）
- `DELETE /projects/{id}/members/{user_id}` - 移除成员（仅 owner/admin）
- `GET /projects/{id}/members` - 成员列表（owner/member/admin 可查）

### 3.5 MetaGPT 编排层
新增文件：
- `backend/app/agents/messages.py` - Message 基类，DiscussionTurn 改为其子类（向后兼容）
- `backend/app/agents/role.py` - Role 基类，提供 `_react(message) -> Message`
- `backend/app/agents/role_adapter.py` - BaseAgentRoleAdapter 包装现有 BaseAgent
- `backend/app/agents/environment.py` - Environment + EventBridge

接入点：
- `orchestrator.py` 的 `get_request_orchestrator` 末尾增加可选 `attach_environment`
- `Environment.from_discussion(session_id)` 工厂方法复用 DiscussionOrchestrator 逻辑
- 新增 `/api/agents/environment` 观测接口（可选）

### 3.6 WebSocket 修复流程
1. SSH 登录 81.70.251.90
2. 检查 docker 容器状态：`docker compose ps`
3. 检查 Caddy 配置与日志：`docker logs cr_frontend`
4. 检查后端日志：`docker logs cr_backend`
5. 本地测试 WS 握手：`curl -i -N ...`
6. 根据日志定位根因（证书/配置/容器/代码）
7. 修复并重新部署
8. 验证 101 Switching Protocols

---

## 四、技术约束

### 4.1 不破坏约束（硬性）
1. 不修改 `Orchestrator.__init__` 的 `_init_agents` 签名
2. 不修改 `BaseAgent.call` 的 LLM 调用与重试机制
3. 不修改 `AgentEventBus` 的 publish/subscribe 签名（仅扩展可选参数）
4. 不修改 `AgentRegistry.list_runtime` 的前端契约
5. 不删除 `DiscussionOrchestrator`（保留作回退）
6. 不修改前端 `discussionStream.ts` 的子协议鉴权方式
7. 不修改后端 `ws_discussion.py` 的控制协议（pause/resume/stop/user_input）

### 4.2 代码规范约束
- 所有新增函数必须添加函数级注释（功能描述、参数说明、返回值类型及用途）
- 严格遵循项目现有代码风格（FastAPI + SQLAlchemy + Pydantic 模式）
- 复用项目现有组件和工具（如 `_scope_filter` 模式）
- API KEY 等敏感信息使用 .env 文件管理

### 4.3 测试约束
- 测试优先：先写测试，后写实现
- 边界覆盖：覆盖正常流程、边界条件、异常情况
- 现有测试不回归：`pytest backend/tests/` 全部通过

---

## 五、任务边界限制

### 5.1 范围内
- WebSocket 线上修复（含 SSH 排查与部署）
- project_member 表 + 管理 API + 数据隔离改造
- SSE 事件流隔离
- MetaGPT Environment+Roles 编排层（新增文件）
- 双端 git+docker 同步

### 5.2 范围外
- 前端 project_member 管理界面（后续单独实施）
- Redis pub/sub 替换内存 EventBus（多 worker 部署后续实施）
- 现有 Orchestrator 内部重构
- BaseAgent LLM 调用机制改造
- DiscussionOrchestrator 删除（保留作回退）

---

## 六、不确定性确认

所有关键不确定性已通过用户确认解决，见 ALIGNMENT 文档第五节"疑问澄清"。本任务无遗留歧义。

---

## 七、集成方案

### 7.1 后端集成
- 新增 `project_member` 表通过 Alembic 迁移
- 新增 `project_member_service.py` 提供通用过滤函数
- 改造 6 个 service 的过滤逻辑（project/review/issue/dashboard/security/agent）
- 新增 `agents/messages.py`, `role.py`, `role_adapter.py`, `environment.py`
- `orchestrator.py` 增加可选 `attach_environment` 方法

### 7.2 前端集成
- 业务页面无需改动（数据差异由后端按 token 返回）
- 可选增强：`user.ts` 增加 `isAdmin` 计算属性，页面头部显示"管理员视角"提示（非必需）

### 7.3 部署集成
- 本地 git commit → push（或服务器直接 pull 本地分支）
- 服务器 `cd /path/to/project && git pull && cd deploy && docker compose up -d --build`
- Alembic 自动迁移（backend 容器启动时执行 `alembic upgrade head`）

---

## 八、下一步

进入 Architect 阶段，生成 DESIGN 架构文档，包含：
- 整体架构图（mermaid）
- 分层设计和核心组件
- 模块依赖关系图
- 接口契约定义
- 数据流向图
- 异常处理策略
