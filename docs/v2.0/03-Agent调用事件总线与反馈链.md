# 03 · Agent 调用事件总线与反馈链

## 一、动机

v1.0 用户调用 ChatAgent 时只看到最终 markdown，中间的"意图分类 → 路由 → 子 Agent → 完成"全部不可见。v2.0 引入事件总线与流式反馈，让"Agent 在干什么"全程透明。

## 二、事件模型

```python
# backend/app/agents/events.py
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

class AgentEventType(str, Enum):
    DISPATCH   = "dispatch"     # 主控派发任务到子 Agent
    THINKING   = "thinking"     # Agent 开始思考（意图分析/LLM 调用前）
    PROGRESS   = "progress"     # Agent 进行中（多 chunk / 重试 / 子调用）
    COMPLETE   = "complete"     # 任务成功
    FAILED     = "failed"       # 任务失败
    CLARIFY    = "clarify"      # Agent 主动追问（缺字段）
    STATUS     = "status"       # 仅状态变化广播（idle→working 等）

@dataclass
class AgentEvent:
    type: AgentEventType
    agent: str                 # 当前主体
    trace_id: str              # 一次会话/调用链的根 id
    parent: str = ""           # 上游 agent，便于绘制路径
    message: str = ""          # 人类可读描述
    payload: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
```

## 三、EventBus 设计

```python
# backend/app/agents/event_bus.py
import asyncio
from collections import defaultdict, deque
from typing import AsyncIterator

class AgentEventBus:
    _instance = None
    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []
        self._history: deque[AgentEvent] = deque(maxlen=200)

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def publish(self, event: AgentEvent) -> None:
        self._history.append(event)
        for q in list(self._subscribers):
            try: q.put_nowait(event)
            except asyncio.QueueFull: pass

    async def subscribe(self) -> AsyncIterator[AgentEvent]:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.append(q)
        try:
            for ev in list(self._history)[-50:]:
                yield ev
            while True:
                yield await q.get()
        finally:
            self._subscribers.remove(q)
```

设计要点：
- 单例（线程安全：FastAPI 单 worker 协程模型即可；多 worker 上线后改用 Redis pub/sub）
- 内存缓存最近 200 条（用于新订阅者的"近况"回放）
- 订阅者断开时自动清理队列

## 四、BaseAgent 增强

```python
# backend/app/agents/base.py 新增
def emit(self, type_: AgentEventType, **kw):
    bus = AgentEventBus.instance()
    bus.publish(AgentEvent(type=type_, agent=self.name, **kw))

def call(...):
    self.emit(AgentEventType.THINKING, message=f"{self.name} 正在调用 LLM")
    # ...原 LLM 调用
    if result.success:
        self.emit(AgentEventType.COMPLETE, message=f"{self.name} 调用完成",
                  payload={"tokens": result.tokens, "duration_ms": result.duration_ms})
    else:
        self.emit(AgentEventType.FAILED, message=result.error or "未知错误")
```

Orchestrator/ChatAssistant 在派发时额外发 DISPATCH：

```python
def chat(self, messages, ctx):
    trace_id = ctx.extra.get("trace_id") or str(uuid.uuid4())
    self.emit(AgentEventType.DISPATCH, trace_id=trace_id,
              message="主控接受聊天请求", payload={"messages": len(messages)})
    return self.chat_agent.execute(messages, ctx)
```

## 五、HTTP 接口

### 5.1 SSE 事件流

`GET /api/agents/events`（需登录）

```
data: {"type":"dispatch","agent":"orchestrator","trace_id":"...","message":"..."}\n\n
data: {"type":"thinking","agent":"chat_assistant","trace_id":"...","message":"..."}\n\n
...
```

### 5.2 同步快照

`GET /api/agents/situation`

```json
{
  "online": 12,
  "working": 2,
  "idle": 10,
  "today_calls": 187,
  "spectrum": [{"bucket":"12:00","count":4}, ...],   // 60 个分钟桶
  "hotspots": [{"code":"code_reviewer","count":78}, ...]
}
```

### 5.3 Agent 运行时清单

`GET /api/agents/runtime` — 从 AgentRegistry 实时枚举，含元数据。

```json
[
  {"code":"orchestrator","name":"主控调度","category":"meta","icon":"orchestrator","color":"#5B58E8","status":"idle","skills":[...],"description":"..."},
  ...
]
```

## 六、前端 StepStream 协议

`AgentChatDrawer` 接收用户消息时：

1. POST `/api/ai/chat` 拿最终回复（保持兼容）
2. 同时订阅 SSE，把 `trace_id` 匹配的事件流转换为"步骤气泡"：

```
┌─ 用户问："看一下我的项目"
├─ ◌ orchestrator   分发任务  (12:34:21)
├─ ◌ chat_assistant 分类意图  list_projects
├─ ◉ project_manager 执行     12 个项目返回
└─ ✓ chat_assistant 整合答复
```

组件：`AiStepStream.vue`，维护本次 trace_id 的事件队列。

## 七、错误与重试

- 任意 Agent emit `FAILED` 后，前端步骤气泡显示红色，并展开错误堆栈
- 用户可点击"重试"，重新 POST `/api/ai/chat` 并复用历史消息
- 超过 3 分钟无任何事件 → 视为断流，自动重连 SSE

## 八、向后兼容

- 不修改 `/api/ai/chat` 响应结构
- v1.0 前端如果不订阅 SSE，照常拿最终回复
- SSE 是增强通道，断开不影响主流程
