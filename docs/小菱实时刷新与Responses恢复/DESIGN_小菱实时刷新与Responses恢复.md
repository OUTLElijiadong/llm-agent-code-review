# 小菱实时刷新与 Responses 恢复 - 设计文档

## 数据流

```mermaid
flowchart LR
    M[Agent Mesh 消息] --> S[会话快照 API]
    S --> P{会话状态}
    P -->|活跃| P1[1 秒轮询]
    P -->|其他| P2[3 秒轮询]
    P1 --> F[小菱悬浮窗]
    P2 --> F
    F --> T[默认折叠调用链]
```

## 中断恢复

```mermaid
sequenceDiagram
    participant R as Responses Runtime
    participant C as Checkpoint
    participant E as Tool Executor
    participant D as DeepSeek
    R->>C: 读取 last_response 与 transcript
    R->>R: 仅提取 completed 响应调用
    R->>R: 排除已有 function_call_output 的 call_id
    R->>E: 真实执行尚未完成调用
    E->>C: 持久化 function_call_output
    R->>D: 发送成对历史并继续推理
```

## 异常策略

- 恢复工具再次要求审批或输入时，重新形成标准 pending 状态，不绕过人工决策。
- 工具执行异常以标准错误输出回灌模型，维持现有协议。
- 轮询瞬时失败不清空界面，按下一周期重试。
- 组件卸载、会话切换和主动停止时仍取消旧定时器并使用 generation 防止串话。
