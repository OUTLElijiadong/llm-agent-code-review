# 设计：Responses 审批闭环

```mermaid
flowchart LR
    U[用户与小菱对话] --> I{是否明确要求改全局模型配置}
    I -->|否或不清楚| C[按当前任务选择或先澄清]
    I -->|是| R[创建高风险审批并绑定 run]
    R --> L[审批列表批量标记会话关联项]
    L --> G{从通用审批入口决定}
    G -->|绑定持久化 run| B[拒绝通用批准/驳回，保持状态]
    B --> O[返回原小菱对话续跑]
    O --> E[原会话审批与恢复执行]
    G -->|无持久化 run| H[保留旧审批处理流程]
```

## 模块与契约

- `agent_responses_service` 与运维知识：明确当前任务模型选择和全局配置修改的语义边界。
- `approval_service.session_resume_required_item_ids(db, items)`：批量收集 `request_json.run_id` 和 `resource=response_run:<id>` 候选值，一次查询持久化 run；任一命中即返回审批 ID。
- 治理审批列表 API/schema：对批量返回结果附加 `requires_session_resume`，不做逐项 SQL 查询。
- `GovernanceWorkstation.vue`：会话审批展示“返回小菱对话处理”，不显示通用决策动作；handler 对绑定事项做防御性拒绝。
- 通用决定 API：在修改审批状态前检查关联，返回 400；响应 run 与审批行保持原状。

## 异常处理

关联字段格式损坏时，对候选字段分别检查；任一字段命中都按受保护会话处理。只有两个关联字段均未命中持久化 run 时，才沿用原通用审批路径。SQL 使用单次 `IN` 查询并对批量空集合提前返回。
