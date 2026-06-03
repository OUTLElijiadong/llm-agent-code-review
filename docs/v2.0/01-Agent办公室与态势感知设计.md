# 01 · Agent 办公室与态势感知设计

## 一、设计灵感

参考 Marvis、Devin 的"AI 助理工位"视觉语言：
- 每个 Agent 一个"工位卡"，有头像 + 名牌 + 状态光环
- 顶部一条"态势感知带"，类似机场监控屏，展示活跃 Agent、调用波形、任务队列
- 底部为"全平台调度流"，最近 N 次调用列表

## 二、页面布局（替换原 `AgentCenter.vue`）

```
┌────────────────────────────────────────────────────────────┐
│  [v2.0] Agent 办公室                          [刷新] [×]   │
├────────────────────────────────────────────────────────────┤
│  ┌─ 态势感知 ─────────────────────────────────────────┐    │
│  │  在岗 11 · 工作中 2 · 空闲 9 · 今日调用 187        │    │
│  │  ▁▂▃▆█▇▆▅▃▂▁  (近 1 小时调用波形)                  │    │
│  │  热点：code_reviewer 78 次  ·  chat_assistant 35   │    │
│  └────────────────────────────────────────────────────┘    │
├────────────────────────────────────────────────────────────┤
│  工位区（grid: 11 个工位卡）                               │
│  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐                    │
│  │  🧠  │  │  🔍  │  │  🛡  │  │  ⚡  │  ...               │
│  │ 主控 │  │ 审查 │  │ 安全 │  │ 性能 │                    │
│  │ idle │  │ work │  │ idle │  │ idle │                    │
│  │  ◌    │  │  ◉    │  │  ◌    │  │  ◌    │                    │
│  │  47   │  │  78   │  │  12   │  │  9    │                    │
│  └──────┘  └──────┘  └──────┘  └──────┘                    │
├────────────────────────────────────────────────────────────┤
│  实时调度流（最近 20 条事件）                              │
│  • 12:34:21  chat_assistant → 分类意图 list_projects        │
│  • 12:34:22  orchestrator   → 调度 project_manager          │
│  • 12:34:23  project_manager → 返回 12 个项目               │
│  • ...                                                     │
└────────────────────────────────────────────────────────────┘
```

## 三、状态机（AgentStatus）

| 值 | 含义 | 颜色 | 动画 |
|---|---|---|---|
| `idle` | 空闲在岗 | `--gray-300` | 慢呼吸 (4s 周期) |
| `thinking` | 接受调用，意图分析中 | `--brand-500` | 三点 typing |
| `working` | 正在执行真实任务 | `--accent-500` | 旋转光环 + 内核脉冲 |
| `blocked` | 等待 Clarify 用户回填 | `--sev-medium` | 黄色感叹号闪烁 |
| `error` | 最近一次调用失败 | `--sev-severe` | 红色破裂边框 |
| `offline` | DB 未注入 / 健康检查失败 | `--gray-400` | 灰度 + 静态 |

状态从 Orchestrator EventBus 推送，前端通过 SSE 接收，并维护 `Map<agentName, AgentStatus>`。

## 四、组件清单

| 组件 | 路径 | 职责 |
|---|---|---|
| `AgentOffice.vue` | `views/agent/AgentOffice.vue` | 顶层布局，替换旧 AgentCenter |
| `SituationPanel.vue` | `components/agent/SituationPanel.vue` | 顶部态势带：在岗数、波形、热点 |
| `AgentDeskCard.vue` | `components/agent/AgentDeskCard.vue` | 单个工位卡 |
| `AgentAvatar.vue` | `components/agent/AgentAvatar.vue` | SVG 头像 + 状态光环 |
| `AgentStatusRing.vue` | `components/agent/AgentStatusRing.vue` | 状态光环动画 |
| `AgentEventStream.vue` | `components/agent/AgentEventStream.vue` | 实时调度流 |
| `AgentSpectrumBar.vue` | `components/agent/AgentSpectrumBar.vue` | 1 小时调用波形（SVG sparkline） |

## 五、交互行为

1. **点击工位卡** → 抽屉打开，展示该 Agent 的详细画像、近 30 条调用、Clarify 历史。
2. **点击"调用"按钮**（在抽屉里） → 跳到 ChatDrawer，预填一条"@<agent_name>"。
3. **态势带"在岗 11"** 点击 → 滚动到工位区。
4. **调度流右上角"暂停"** → 暂停 SSE 推送，便于排查。

## 六、数据来源

- 在岗数 / 列表：`GET /api/agents/runtime` → 从 AgentRegistry 实时枚举。
- 调用统计：沿用 `/api/agents/usage`，已按 user 聚合。
- 波形：`GET /api/agents/situation?minutes=60` → 按分钟桶聚合 AiCallLog。
- 实时事件：`GET /api/agents/events`（SSE） → Orchestrator EventBus 广播。

## 七、可访问性

- 工位卡 `role="article"`，包含 `aria-label="Agent <name>, 状态 <label>"`
- 状态光环用 `aria-hidden="true"`，状态文字仍单独输出
- 调度流支持键盘上下浏览（`tabindex=0` + 上下键）
- 颜色不是唯一区分项，每个状态都有文字徽标
