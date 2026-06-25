# TODO_agent-governance-platform

> 任务名称：agent-governance-platform  
> 阶段：6A / Assess 待办清单  
> 日期：2026-06-25  

## 1. 上线前必做

- [ ] 生产环境执行 `cd backend && alembic upgrade head`，确认输出为 `002 (head)`。
- [ ] 部署前执行 `pip install -r backend/requirements.txt` 或重新构建后端镜像，确保 `apscheduler` 已安装。
- [ ] 在 `.env` 中按生产需要确认 `AGENT_GOVERNANCE_SCHEDULER_ENABLED=true` 或显式关闭。
- [ ] 在 `.env` 中按生产需要确认 `AGENT_KNOWLEDGE_FETCH_TIMEOUT`、`AGENT_KNOWLEDGE_FETCH_MAX_BYTES`、`AGENT_KNOWLEDGE_ALLOW_PRIVATE_URLS`、`AGENT_KNOWLEDGE_ENFORCE_DNS_CHECK`。
- [ ] 如需抓取 GitHub issue/PR 且担心匿名限流，在 `.env` 配置 `AGENT_KNOWLEDGE_GITHUB_TOKEN`，不要提交到 git。
- [ ] 登录管理员账号访问 `/admin/overview`，确认管理端菜单、统计卡、表格和操作按钮可用。
- [ ] 对 `/api/admin/**` 做一次生产环境权限抽查，确认普通用户无法访问。

## 2. 需要你提供或确认的配置

- [ ] 外部知识来源白名单：官方文档 URL、允许抓取的 GitHub 仓库、issue/PR 范围。
- [ ] Webhook 地址：审批、告警、调度失败是否推送到企业微信、飞书、钉钉或其他平台。
- [ ] 生产调度时间：当前默认 `daily@02:00` 抓取、`daily@03:00` 反思、`daily@04:00` 自进化，可按实际低峰时间调整。
- [ ] 自动审批阈值策略：当前默认低/中风险 allow 自动审批，高风险人工审批；后续可按 Agent 单独调阈值。
- [ ] Agent 共享知识域：默认隔离，若某些 Agent 需要共享知识，请列出可只读共享的 Agent 和知识域。

## 3. 建议增强

- [ ] 增加管理端端到端浏览器测试，覆盖 `/admin/overview`、审批通过/拒绝、策略试算、任务手动运行。
- [ ] 为生产多副本部署增加分布式调度锁，避免多个后端实例同时执行每日任务。
- [ ] 增加工具回放详情页，展示输入/输出摘要、策略上下文和审批链路。
- [ ] 增加成本中心细分：按 Agent、模型、任务类型统计 token 与费用。
- [ ] 增加模型评测闭环：把 `eval_case` 与 Agent 奖惩、版本灰度打通。

## 4. 已完成但上线需验收的能力

- [x] 外部抓取已从占位扩展为真实抓取器：项目代码、官方 URL、指定 URL、GitHub issue/PR。
- [x] 高风险知识审批通过后自动转 active，管理端也提供手动激活入口。
- [x] 策略规则编辑 API 和前端表单已接入，保存策略时自动生成 policy artifact 版本。
- [x] 工具权限配置 API 和前端表单已接入，工具网关真实应用 allow/deny/escalate 权限。
- [x] 任务调度配置 API 和前端表单已接入，支持管理端修改 schedule/status 并手动运行。

## 5. 已知非阻塞项

- 前端构建存在 3 类非阻塞警告，当前不影响业务运行或构建产物生成：
  - Sass legacy JS API deprecation：来自 Sass/Vite 工具链兼容层，当前构建成功；风险是 Dart Sass 2.0 后旧 API 可能移除，后续依赖升级时需要继续保持 `modern-compiler` 或升级相关插件。
  - Element Plus / @vueuse pure 注释警告：Rollup 无法解释依赖包里的 `/*#__PURE__*/` 注释位置并自动剥离；这是第三方依赖打包提示，不改变运行逻辑。
  - chunk size 警告：Monaco、ECharts、Element Plus 等包导致部分产物较大；不影响正确性，影响主要是首屏加载性能，后续可通过路由懒加载、手动分包或按需加载继续优化。
- 本轮不接真实 OpenClaw/Hermes 本体，仅使用其概念映射。
- 本轮未做模型权重微调，也未让奖惩自动封禁 Agent。

## 6. 快速验证命令

```bash
cd backend
.venv/bin/alembic current
.venv/bin/python -m pytest -o addopts='' tests -q
.venv/bin/python -m ruff check app tests
.venv/bin/python -m compileall app tests

cd ../frontend
npm run build
```

其中 `backend/tests/unit/services/test_agent_governance_api_integration.py` 已纳入全量测试，覆盖管理端真实 FastAPI 路由闭环，并自动校验 `frontend/src/api/adminGovernance.ts` 中 23 条管理端 API 调用全部匹配后端 `/api/admin` 注册端点。`backend/tests/unit/services/test_frontend_api_contract.py` 会继续校验前端 150 条 HTTP API 调用、SSE `/agents/events` 和 WebSocket `/api/ws/discuss/{session_id}` 均匹配后端真实端点。
