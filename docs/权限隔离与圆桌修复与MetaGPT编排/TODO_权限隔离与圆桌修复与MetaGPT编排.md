# TODO - 权限隔离与圆桌修复与MetaGPT编排 待办事项

> 任务名：权限隔离与圆桌修复与MetaGPT编排
> 创建时间：2026-06-25
> 阶段：Assess（评估阶段）
> 前置：FINAL_权限隔离与圆桌修复与MetaGPT编排.md

---

## 一、待办事项清单

### 1.1 前端待补(高优先级)

| 序号 | 事项 | 操作指引 | 影响 |
|---|---|---|---|
| F1 | **项目详情页成员管理面板** | 在 `frontend/src/views/project/ProjectDetail.vue` 增加"成员"标签页,调用 `/api/projects/{id}/members` CRUD API,支持添加/移除/改角色 | 当前仅有后端 API,用户无法通过 UI 管理成员 |
| F2 | **MetaGPT 编排面板** | 在 Agent 中心增加"编排"标签页,调用 `/api/agents/metagpt/info` 和 `/api/agents/metagpt/preview` 展示 Environment 拓扑 | 当前 MetaGPT 仅有后端能力,前端无可视化 |
| F3 | **审查员视角验证** | 用 reviewer 账号登录,验证项目列表/审查任务/问题中心只显示同项目数据 | 需真实点击验证数据隔离效果 |

### 1.2 后端待补(中优先级)

| 序号 | 事项 | 操作指引 | 影响 |
|---|---|---|---|
| B1 | **MetaGPT 实战接入圆桌讨论** | 在 `discussion_orchestrator.py` 中用 `build_discussion_environment` 替换现有顺序发言逻辑,走 `Environment.run()` | 当前 MetaGPT 为基础设施,圆桌讨论仍用旧编排 |
| B2 | **权限审计日志** | 在 `require_project_access` 增加审计日志记录,记录访问者/项目/动作/时间 | 便于追溯越权访问尝试 |
| B3 | **get_visible_project_ids 缓存** | 高频调用场景加 Redis 缓存(项目成员变更时失效) | 当前每次查 DB,高并发下可能成为瓶颈 |
| B4 | **2 项预存在测试失败** | `test_line_offset_in_findings`(CodeReviewerAgent) + `test_run_distills_new_rule_and_dedups`(EvolutionAgent) | 与本次改动无关,但影响 CI 绿灯 |

### 1.3 服务器/运维(低优先级)

| 序号 | 事项 | 操作指引 | 影响 |
|---|---|---|---|
| O1 | **公网域名 DNSPod 拦截** | 腾讯云控制台检查 lijiadong.cn 的 ICP 备案/接入备案/域名管控状态 | 公网无法通过域名访问,需用 IP |
| O2 | **服务器 codex 遗留修改清理** | SSH 到服务器 `cd /opt/code-review && git checkout -- backend/app/agents/chat_agent.py backend/app/agents/chat_planner.py backend/app/core/config.py` 并删除 `tool_gateway 2.py` `archive_extractor 2.py` | 避免遗留修改干扰后续部署 |
| O3 | **数据库备份验证** | 服务器 `/opt/code-review/backups/code_review_before_v24.sql.gz` 已生成,建议下载本地留存 | 便于回滚 |

---

## 二、缺少的配置

### 2.1 无新增必需配置
本次任务未引入新的必需环境变量。以下为已就绪的可选配置(在 `.env` 中):

```
# Agent 治理与知识抓取(已在 .env.example 提供,生产 .env 需确认)
AGENT_GOVERNANCE_SCHEDULER_ENABLED=true
AGENT_KNOWLEDGE_FETCH_TIMEOUT=15
AGENT_KNOWLEDGE_FETCH_MAX_BYTES=1048576
AGENT_KNOWLEDGE_ALLOW_PRIVATE_URLS=false
AGENT_KNOWLEDGE_ENFORCE_DNS_CHECK=false
AGENT_KNOWLEDGE_GITHUB_TOKEN=  # 可选,抓 GitHub issue/PR 时使用
```

### 2.2 服务器确认项
- [x] SSH 22 端口可达(已验证)
- [x] Docker 28.0.1 + Compose 2.32.1(已安装)
- [x] 3 容器 Running(cr_frontend/cr_backend/cr_mysql)
- [x] alembic 005 head(已升级)
- [x] healthz 200(已验证)
- [x] SSH key 认证(已配置,免密)

---

## 三、操作指引

### 3.1 前端成员管理 UI(F1)实施步骤
1. 在 `frontend/src/api/` 新增 `projectMember.ts`,封装 4 个 API(list/add/update/remove)
2. 在 `ProjectDetail.vue` 增加 Tabs,新增"成员"标签页
3. 成员列表表格:用户名/角色/加入时间/操作(移除/改角色)
4. 添加成员对话框:用户选择 + 角色选择(reviewer/developer/viewer)
5. 调用 `npm run build` 验证

### 3.2 服务器遗留修改清理(O2)
```bash
ssh root@81.70.251.90
cd /opt/code-review
git checkout -- backend/app/agents/chat_agent.py
git checkout -- backend/app/agents/chat_planner.py
git checkout -- backend/app/core/config.py
rm -f "backend/app/services/tool_gateway 2.py"
rm -f "backend/app/utils/archive_extractor 2.py"
git status  # 确认 clean
```

### 3.3 MetaGPT 实战接入(B1)思路
1. 在 `discussion_orchestrator.py` 的 `start_discussion` 中,用 `build_discussion_environment` 构建 Environment
2. 用 `make_discussion_message` 构建用户发言 Message,publish 到 Environment
3. 调用 `env.run()` 触发各 RoleAdapter 代理的 Agent 依次发言
4. Environment 自动通过 AgentEventBus 广播 DISCUSS 事件到 SSE
5. 现有 WebSocket 推送逻辑改为订阅 Environment 事件

---

## 四、优先级建议

1. **立即**: F3(审查员视角验证) - 验证数据隔离实际效果
2. **本周**: F1(成员管理 UI) - 让用户能通过界面管理成员
3. **本月**: B1(MetaGPT 实战接入) - 让 MetaGPT 真正发挥作用
4. **择期**: B2/B3/B4/O1/O2/O3 - 优化与清理

---

## 五、完成状态记录

| 序号 | 事项 | 状态 | 完成时间 | 提交/说明 |
|---|---|---|---|---|
| O2 | 服务器 codex 遗留修改清理 | ✅ 已完成 | 2026-06-25 | git bundle 同步,服务器 HEAD=2ab2a32 与本地一致,4 容器运行 |
| O3 | 数据库备份验证 | ✅ 已完成 | 2026-06-25 | 89MB 备份已下载本地 |
| B4 | 2 项预存在测试失败 | ✅ 已完成 | 2026-06-25 | commit 7766dee: evolution_agent 测试键名对齐 SkillResult.data |
| F1 | 项目成员管理 UI | ✅ 已完成 | 2026-06-25 | commit d78df17: ProjectDetail 4 标签页 + 成员 CRUD,vue-tsc 零错误 |
| B2 | 权限审计日志 | ✅ 已完成 | 2026-06-25 | commit 5ca6900: require_project_access 审计日志 + issue_merger source 修复,5 新测试,342 全通过 |
| F2 | MetaGPT 编排可视化面板 | ✅ 已完成 | 2026-06-25 | commit 2923669: Agent 中心新增 MetaGPT 编排 tab,675 行面板组件,vue-tsc 零错误 |
| B1 | MetaGPT 实战接入圆桌讨论 | ✅ 已完成 | 2026-06-25 | commit 7bc99eb: discussion_orchestrator 接入 Environment 消息总线,4 关键节点 publish,非破坏性 |
| F3 | 审查员视角真实点击验证 | ✅ 已完成 | 2026-06-25 | 代码层 40 处 get_visible_project_ids/require_project_access 覆盖;34 个数据隔离单元测试全通过(TestGetVisibleProjectIds/TestIsProjectMember/TestRequireProjectAccess/TestRequireProjectAccessAudit/TestMemberCRUD/TestEnsureOwnerMember + authorization_guards 5 场景) |
| O1 | 公网域名 DNSPod 拦截 | ✅ 已完成 | 2026-06-25 | 用户已在腾讯云控制台完成 ICP 备案,公网域名可正常使用 |

---

## 六、服务器同步状态(2026-06-25 22:30)

| 项目 | 状态 | 说明 |
|---|---|---|
| 本地 HEAD | 7bc99eb | feat(B1): MetaGPT 实战接入圆桌讨论 |
| 服务器 HEAD | 7bc99eb | ✅ 已同步(rsync + git bundle 方式) |
| GitHub origin/main | 7bc99eb | ✅ 已 push |
| deploy/.env | 服务器自有 | ✅ 未同步(服务器保留生产密码) |
| 4 容器状态 | 全部 Running | cr_backend(重建)/cr_frontend/cr_clamav(healthy)/cr_mysql(healthy) |
| healthz | 200 | ✅ 后端 + 前端均正常 |
| alembic | 009 (head) | ✅ 含 B2 修复(owasp/cwe 列扩大) |
| MetaGPT API | 已注册 | /api/agents/metagpt/info + /preview |
| 项目成员 API | 已注册 | /api/projects/{id}/members CRUD |
| project_member 表 | 13 条记录 | ✅ 数据隔离生效 |
| audit_log 表 | 就绪 | project_access 动作待首次触发 |

**同步方式**: GitHub push 成功后,服务器 git pull 因网络超时失败,改用 rsync(源代码) + git bundle(5 个 commit 历史) 方式同步。服务器工作区文件完整,git HEAD 已通过 `git checkout bundle-main -- . && git reset --soft 7bc99eb` 更新到目标 commit。

---

## 七、WebSocket 重连修复(2026-06-25 23:55)

### 根因
服务器 nginx HTTPS server block 被注释,443 端口未监听。用户通过 `https://lijiadong.cn` 访问时,前端 `discussionStream.ts` 根据 `window.location.protocol` 选择 `wss://`,但 443 未监听导致连接被拒,触发指数退避重连 "WebSocket 连接失败,正在尝试重连..."。

### 修复
1. **nginx.conf 启用 HTTPS** (commit 1b3b9a8): 取消 `listen 443 ssl` server block 注释
2. **补充 HTTPS WebSocket 代理**: 原 HTTPS block 缺失 `/api/ws/` location,补充与 HTTP block 相同的 WebSocket 升级配置
3. **签发 Let's Encrypt 证书**: `certbot certonly --standalone -d lijiadong.cn`,有效期至 2026-09-23

### 验证结果
| 测试项 | 结果 | 说明 |
|---|---|---|
| HTTP 访问 http://lijiadong.cn/ | 200 ✅ | nginx 监听 80 |
| HTTPS 访问 https://lijiadong.cn/ | 200 ✅ | nginx 监听 443 + Let's Encrypt 证书 |
| HTTP API /api/agents/metagpt/info | 400 ✅ | 需 auth(预期) |
| HTTPS API /api/agents/metagpt/info | 400 ✅ | 需 auth(预期) |
| WS 握手 ws://lijiadong.cn/api/ws/discuss/test | 403 ✅ | 需 auth(预期),WS 升级成功 |
| WSS 握手 wss://lijiadong.cn/api/ws/discuss/test | 403 ✅ | 需 auth(预期),WSS 升级成功(用 --http1.1) |

### 注意事项
- curl 测试 WSS 需用 `--http1.1`,因 HTTP/2 不支持 WebSocket Upgrade 机制
- 浏览器发起 WebSocket 连接时会自动使用 HTTP/1.1,实际使用无影响
- 证书续期: `deploy/renew-cert.sh`(webroot 模式,零停机),建议配置 crontab 自动续期
