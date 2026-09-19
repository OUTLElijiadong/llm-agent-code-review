---
标题: Prism 继续优化机会审计设计
类型: 项目设计
状态: 可执行
更新日期: 2026-09-19
观察时间: 2026-09-19
来源: CONSENSUS_优化机会审计、3.9.9 代码与生产边界
置信度: 高
tags:
- 主题/项目管理
- 类型/系统设计
- 状态/可执行
---
# Prism 继续优化机会审计设计

## 1. 目标架构

```mermaid
flowchart LR
  UI[浏览器页面/小菱浮窗] --> ROUTE[唯一页面与兼容路由]
  ROUTE --> CAP[能力注册表]
  CAP --> AUTH[集中身份/RBAC判定]
  AUTH --> DOMAIN[统一业务服务]
  DOMAIN --> CHAT[Responses/Mesh会话与运行账本]
  DOMAIN --> SUPPORT[统一支持工单事实源]
  DOMAIN --> INPUT[统一输入边界]
  CHAT --> RET[活跃/归档保留策略]
  INPUT --> DYN[动态攻击字典]
  DOMAIN --> OBS[指标、审计、失败账本]
  MODEL[DeepSeek/CodeQL/安全扫描外部依赖] -.能力探测/配额/结果回执.-> OBS
```

## 2. 事实源设计

| 领域 | 主事实源 | 兼容源处理 |
| --- | --- | --- |
| 小菱会话 | `AgentMeshConversation`、`AgentResponseRun` 及消息账本 | 旧 `AdminChatSession/AdminChatMessage` 只读迁移，迁移后禁止新写入 |
| 支持中心 | `/support` 页面与统一工单模型 | 旧 `/maintenance`、`/feedback` 仅跳转/适配，不进入导航、搜索和权限目录 |
| 角色权限 | 集中角色解析与 capability 判定 | 旧 `user.role` 只作为迁移兼容，不能与 RBAC 产生相反结论 |
| 自定义 Agent | 管理员审批的平台级发布目录 | 列表、搜索和派发共用 enabled + published 谓词，普通目录不暴露 owner_id |
| 模型扫描 | 任务/分片/续跑账本 | 配置值、单次输出或截断响应不能替代结果账本 |

## 3. 会话流程

```mermaid
sequenceDiagram
  participant B as 浏览器
  participant API as 会话 API
  participant DB as Responses/Mesh 账本
  participant J as 清理任务
  B->>API: 当前账号查询 active/archived
  API->>DB: user_id + surface + session_key
  DB-->>API: 归属后的分页记录
  B->>API: 新消息/显式归档
  API->>DB: 写消息并生成稳定标题
  API->>J: 触发保留策略
  J->>DB: 归档最旧可归档会话并记录原因
```

## 4. 输入安全契约

- 结构化请求模型统一 `extra="forbid"`，标识字段只允许规定字符集。
- 文本字段按业务用途设置长度、控制字符策略和是否允许换行；错误信息不回显 SQL、模板或凭据。
- `dict/list/data URL` 必须限制嵌套深度、键/元素数量、单项字节数和总字节数。
- 动态字典验证响应码、无 500、无 SQL/模板执行、无 HTML 执行、无越权写入和无日志泄漏。

## 5. 兼容与回滚

先添加只读审计、指标和迁移前检查，再切换写路径；新旧计数不一致时停止切换。数据库迁移必须可逆，保留当前发布备份，回滚点要包含应用版本、迁移版本和配置快照。
