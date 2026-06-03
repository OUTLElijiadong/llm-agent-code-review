# 04 · Agent 主动提问机制（Clarify Protocol）

## 一、痛点

v1.0 的 `ChatAssistantAgent._classify_intent` 在用户说"看一下报告"时，会默认 `list_reports`；用户说"删除项目"但没给 ID 时，直接返回错误。这是基于猜测，违背用户实际意图。

v2.0 引入 **Clarify Protocol**：Agent 识别到关键字段缺失时，不再猜测，主动向用户追问；用户回填后继续执行。

## 二、协议形态

### 2.1 Agent 输出 Clarify

子 Agent 或 ChatAgent 检测到必填字段缺失，返回：

```python
AgentResult(
    success=False,
    error="NEED_CLARIFICATION",
    data={
        "clarify_id": "clr_xxx",       # uuid
        "intent": "delete_project",
        "questions": [
            {
                "key": "project_id",
                "label": "你要删除哪个项目？",
                "type": "select_project",      # 前端控件类型
                "required": True,
                "hint": "从你的项目列表中选一个",
                "options": [                    # 可选：预填选项
                    {"value": 12, "label": "我的Python项目"},
                    {"value": 13, "label": "Demo"}
                ]
            }
        ],
        "context": {"original_text": "删除项目"}
    },
    model="chat_assistant"
)
```

并 emit `CLARIFY` 事件到 EventBus。

### 2.2 前端展示

`AgentChatDrawer` 收到 `error="NEED_CLARIFICATION"` 后，渲染一张追问卡片：

```
┌─────────────────────────────────────────┐
│ 🤔 我想确认一下                          │
│                                         │
│ 你要删除哪个项目？                       │
│ [▾ 选择项目              ]                 │
│                                         │
│ [取消]  [确认提交]                      │
└─────────────────────────────────────────┘
```

控件类型映射：

| type | 控件 |
|---|---|
| `text` | 单行输入 |
| `textarea` | 多行 |
| `select` | 下拉（用 `options`） |
| `select_project` | 项目下拉（自动从 /api/projects 拉） |
| `select_file` | 文件下拉（依赖已选项目） |
| `select_task` | 审查任务下拉 |
| `number` | 数字 |
| `code` | Monaco 代码片段 |

### 2.3 用户回填后

前端 POST `/api/agents/clarify`：

```json
{
  "clarify_id": "clr_xxx",
  "answers": {"project_id": 12}
}
```

后端把 answers 合并回 intent payload，重新走一次 handler：

```python
@router.post("/clarify")
def submit_clarification(payload: ClarifyPayload, ...):
    clarify = clarify_store.pop(payload.clarify_id)  # 取出待回填的 intent
    intent = clarify["intent"]
    merged_payload = {**clarify["payload"], **payload.answers}
    orch = get_orchestrator()
    result = orch.chat_agent._dispatch(intent, merged_payload, ctx)
    ...
```

## 三、ChatAgent 增强：意图分类时识别缺字段

```python
INTENT_REQUIRED_FIELDS = {
    "delete_project":      ["project_id"],
    "start_review":        ["project_id"],
    "list_review_issues":  ["task_id"],
    "list_code_files":     ["project_id"],
    "create_project":      ["project_name"],
    "review_code":         ["code"],
}

INTENT_OPTIONAL_FIELDS = {
    "start_review": ["review_type"],   # 默认 quick，但建议追问
}

def _validate_or_clarify(intent_name, payload) -> Optional[AgentResult]:
    required = INTENT_REQUIRED_FIELDS.get(intent_name, [])
    missing = [k for k in required if not payload.get(k)]
    if not missing:
        return None
    questions = [build_question(intent_name, k) for k in missing]
    return AgentResult(success=False, error="NEED_CLARIFICATION", data={...})
```

`build_question` 是一张固定的"字段 → 提问模板"映射，例如：

```python
QUESTION_TEMPLATES = {
    "project_id": {"label": "你想操作哪个项目？", "type": "select_project"},
    "task_id":    {"label": "针对哪个审查任务？", "type": "select_task"},
    "code":       {"label": "请把要审查的代码贴在这里",
                   "type": "code", "hint": "支持任何语言，用三个反引号包裹"},
    "review_type":{"label": "想做哪种审查？", "type": "select",
                   "options": [
                     {"value":"quick","label":"快速审查"},
                     {"value":"standard","label":"标准审查"},
                     {"value":"security","label":"安全审查"},
                     {"value":"performance","label":"性能审查"},
                     {"value":"full","label":"全面审查"}
                   ]},
}
```

## 四、Clarify 状态机

```
USER → ChatAgent  (一段自然语言)
         ↓
     classify → (intent, payload)
         ↓
     _validate_or_clarify
         ├─ 无缺 → 执行 handler → 返回结果
         └─ 有缺 → 保存到 clarify_store(key=clarify_id, 超时 5min)
                  → 返回 CLARIFY → emit CLARIFY 事件
                                  ↓
USER → [Clarify 回填卡]
         ↓
POST /api/agents/clarify {clarify_id, answers}
         ↓
合并 payload → 重走 handler → 正常 AgentResult
```

## 五、超时与清理

- `clarify_store` 用内存 dict，key 自带 TTL（5 分钟）
- 5 分钟未回填的 clarify 自动失效，前端表单提交时收到 410，提示"会话已过期，请重新提问"

## 六、可观测

- 每次 CLARIFY 都发布事件到 EventBus，前端办公室的调度流可见"🤔 chat_assistant 正在等待用户回答"
- AgentDeskCard 的状态切换为 `blocked`，进入黄色感叹号动画
- /api/agents/usage 的 last_called_at 不更新，避免污染调用统计

## 七、回归约束

- 不破坏 v1.0 的 `/api/ai/chat` 响应结构：CLARIFY 走的依然是 200 + Resp.data 的 ChatResponse；前端通过 `content` 中的标记字符或新增的 `clarify` 字段识别（v2.0 ChatResponse 新增 `clarify?: ClarifyOut`）
- 老前端如果不识别 clarify 字段，会把追问文本直接展示给用户，仍可接受
