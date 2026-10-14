# 团队轮询请求复核

观察日期：2026-10-14。范围：候选工作树前端与本地确定性测试。

## 需求与边界

在已观察到生产发布窗口内存不足、团队详情周期请求的背景下，先复现实际请求行为，再最小修复。稳定的 completed、failed、cancelled、expired 团队在后台不继续周期读取完整详情；打开目标团队或显式刷新仍能得到最新结果。活动团队增量事件、同账号历史恢复、账号切换后的迟到响应隔离继续有效。未读取成功的团队不能被误当终态。

本次只拥有 AgentChatDrawer、AdminCopilot 的团队轮询段、共享详情读取选择函数和相关测试；未修改 GlobalDiscussionHost、AgentDiscussionPanel、Agent Mesh 后端、部署脚本或生产数据。未安装 UI 工具、未操作生产 GUI、未登录账号、未触发真实模型调用。

## 源码复核与红灯

两个聊天宿主原先在每次 session snapshot 轮询后执行 `refreshAgentTeam`：先以当前 surface/session、limit=20 读取团队列表，再对列表中的每个 team_id 无条件调用 `getAgentTeam`。详情包含成员、任务结果、事件和消息。因此，团队已终态并不影响宿主周期读取详情。

本地挂载真实 Vue 组件，给每端 16 个团队，四种终态各 4 个。每个用例首次恢复读取 16 次详情，然后清零详情计数并推进 9 秒定时器。两端各独立运行三轮，六个用例均红灯：每轮在首次恢复后又产生 **48 次完整详情请求**。这些数字是本地 fake timer + API fixture 证据，不是生产流量计数。原始失败日志为 `evidence/team-polling-red.log`。

`ThinkingCity` 的来源是宿主 `activeThinkingTeamIds`，已排除四种终态；它的 `trackedTeams` 根据当前 props 集合移除团队。`useAgentTeamEvents` 对每队保留独立 after_id 游标，排空 has_more 后按终态停止。历史 messages.teamIds 是展示锚点，本次未复现“自动无限恢复全部历史团队并永久增量跟踪”的行为；不能把这条猜测当成已确认缺陷。

## 最小实现

新增 `frontend/src/utils/agentTeamPolling.ts`，两个宿主复用同一选择规则：只有已成功读取、当前与列表均终态、状态一致且列表 updated_at 未变化的详情才能复用。

以下情况继续读取详情：

- 尚无成功详情，包括上次详情请求失败。
- 草稿、排队、运行、验证等非终态，或没有确认状态。
- 从活动变为终态，确保最终任务结果、错误与通知能同步。
- 终态状态改变、updated_at 改变，或团队被重试后重新变为活动。
- 用户端显式打开的目标团队；打开时立即同步，窗口 refreshed 事件继续刷新目标。关闭后该终态团队再次适用后台停止规则。

管理员侧面板已有独立按目标读取、终态停止的逻辑，打开或重新打开会读取最新详情；宿主复用其他稳定终态详情，避免同时扫完所有旧团队。

成功的空列表、局部失败保留既有详情、refresh 请求代际、账号/会话 scope 检查、消息团队锚点、服务端结果通知及本地快照格式沿用现有实现。没有删记录、截断任务结果或更改增量事件运行逻辑。

## 绿灯与增样

| 验证 | 结果 |
|---|---|
| 两端 16 个终态团队，3 轮独立恢复，首次之后 9 秒周期 | 每轮额外详情请求由 48 降为 0；16 个结果仍保留 |
| 终态与 running/verifying/queued/draft 混合 | 稳定终态复用，活动团队继续同步 |
| 新终态团队首次详情读取失败 | 下次继续重试，不误停未知详情 |
| 活动转 completed/failed | 拉取最终状态一次，稳定后停止 |
| 终态版本更新及重试 queued | 读取新结果，重新进入活动集合 |
| 100000 个 ASCII 字符加末尾哨兵的真实任务 result 字段 | 100006 字符 / 100018 UTF-8 字节，内容逐字相等、末尾完整，没有前端截断 |
| 用户目标窗口打开、refreshed、关闭 | 只要求选中团队获取最新；其他终态不取；关闭后后台不取 |
| 管理员终态侧面板打开/重开 | 显式目标详情读取有效 |
| 同账号历史团队恢复及跨账号迟到详情 | 历史团队首次重新确认、稳定后复用；旧账号响应不能回填新账号 |
| 3 个活动团队独立游标，3 轮事件测试 | 各自 0→自己的 event_id，completed/failed/cancelled 后停止 |
| 事件 API 短暂 500 与未知状态 | 指数退避后沿用既有游标重试，最终终态停止 |
| 既有权限错误、has_more 排空、完整消息及操作目标冻结 | 相关回归通过 |

执行命令：

```bash
cd frontend
npm test -- src/components/ai/AgentChatDrawer.test.ts src/components/admin/AdminCopilot.test.ts src/composables/useAgentTeamEvents.test.ts src/components/ai/AgentTeamSidePanel.test.ts src/components/ai/AgentTeamWindow.full-payload.test.ts src/components/ai/AgentTeamWindow.target-freeze.test.ts src/utils/agentChatSessions.test.ts src/utils/agentPageActions.test.ts
npx vue-tsc --noEmit
npx eslint src/components/ai/AgentChatDrawer.vue src/components/ai/AgentChatDrawer.test.ts src/components/admin/AdminCopilot.vue src/components/admin/AdminCopilot.test.ts src/utils/agentTeamPolling.ts src/composables/useAgentTeamEvents.test.ts
git diff --check
```

结果：**8 个测试文件、193 个测试通过**；类型检查、定向 ESLint 和 diff whitespace 检查均 exit 0。新增团队复核为宿主 13 个用例和增量 composable 4 个用例。完整日志：`evidence/team-polling-green-full.log`；终态专项日志：`evidence/team-polling-green-terminal.log`；检查日志：`evidence/team-polling-typecheck.log`、`evidence/team-polling-eslint.log`。

## 后端列表仍需单独处理的证据

实际后端 `agent_team_service.list_teams` 调用 `_team_out(db, row)`。该函数即使 include_events=False 仍查询成员和任务，解析 task.result_json 并生成任务结果及 summary 投影；只省略 events/messages。因此，这次前端优化确实消除了稳定终态的重复详情 GET，却不能宣称 `/agent-teams` 列表已经变成窄列或轻量查询。此发现已送交主代理处理，前端子任务没有扩大所有权修改后端。

本地测试不能证明生产 OOM 根因已全部消除；发布后的真实进程内存、生产日志和账号行为需要由主任务独立验收。当前结果仅确认并修复了两端宿主周期扫完所有终态团队详情这一条真实行为。

## 独立复核与知识闭环

只读子代理独立重读最终源码、红绿日志、测试和原始目标，重跑团队专项、增量和既有账号/同会话迟到恢复样本：**3 个文件、25 个测试通过，100 个未命中筛选的测试跳过**。独立核对红 48→绿 0、8 文件 193 测试及真实 tasks[0].result.evidence 的 100000 ASCII+尾哨兵，数字与原始日志一致；未发现本次修复的已确认缺陷。独立日志：`evidence/team-polling-independent.txt`，SHA-256 `e60bd55e50849a4c2b42e9c2d366aef014d777c3e82cd6dd0f69aef087796385`。该独立复核仍是本地自动化，不是生产验收。

知识 Harness preflight 收据：`20261014-051105-1b04974bb2`，hard_gate=ready。查阅唯一主库首页、用户习惯、治理、路由和任务主题入口；没有本次用户新增长期偏好，不创建空习惯摘要。validate 已完成，1530 个文件、0 错误、0 警告；postflight result_status=通过，闭环回执为 `myself/03_索引/审计/任务闭环/任务闭环-20261014-052531.md`。
