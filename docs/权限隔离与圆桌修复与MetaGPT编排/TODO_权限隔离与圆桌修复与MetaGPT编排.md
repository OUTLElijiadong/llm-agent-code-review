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
