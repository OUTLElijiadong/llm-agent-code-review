# 稳定部署与生产验收设计

## 发布架构

```mermaid
flowchart TD
  A[4.1.6 候选源码] --> B[定向回归与静态检查]
  B --> C[单一 Git 提交与 SHA]
  C --> D[本地 Git bundle]
  D --> E[生产仓库精确导入并检出 SHA]
  E --> F[deploy.sh all]
  F --> G[备份与隔离恢复验证]
  G --> H[迁移与后端镜像]
  H --> I[前端镜像与 assets 同步]
  I --> J[健康与 HTTPS 冒烟]
  J -->|通过| K[生产账本 current.env]
  J -->|失败| L[正式脚本应用回滚]
  K --> M[真实账号页面与报告验收]
```

## 控制流修复

```mermaid
sequenceDiagram
  participant U as 当前用户
  participant F as 圆桌前端
  participant A as 已鉴权 REST API
  participant B as DiscussionBus
  participant W as Agent 编排任务
  participant R as 审查任务账本
  U->>F: 确认停止
  F->>A: POST /discuss/sessions/{id}/stop
  A->>A: 校验会话所有者
  A->>B: 持久标记 cancelled / 移除待启动上下文
  alt 有活动编排任务
    A->>W: cancel()
    W->>R: 记录取消并禁止后续阶段
  else 无活动编排任务
    A->>R: 只取消当前用户的 running 报告任务
  end
  A-->>F: 返回终态与上游在途请求提示
  F->>F: 锁定取消终态，忽略迟到的活动快照
```

## 报告可见性

- 代码审查/报告型任务继续使用既有对象所有者、项目成员和 `report:view` 策略。
- 远程网站评估列表以账号与角色先过滤再 count、排序和分页：管理员 `scope=all`；审批人 `scope=all` 只包含本人任务与待审批申请；其他请求按本人任务过滤。
- 待审批申请是审批工作项，不代表审批人拥有结果报告访问权；报告接口继续执行单独的对象所有者/管理员校验。

## 生产验收数据流

```mermaid
flowchart LR
  A[生产 release 与健康回执] --> C[验收记录]
  B[账号可见菜单与列表] --> C
  D[报告详情与导出] --> C
  E[Token 日用量原始统计与图表] --> C
  F[CNVD/CNNVD 授权与精确命中凭据] --> C
  G[未完成/权限不足/未授权项目] --> H[TODO 与限制说明]
```

## 异常处理

- bundle 导入、精确 SHA 检查或正式部署预检失败时不切换服务，记录实际阻断项。
- 正式脚本在切换后遇到健康/冒烟失败时按脚本的应用镜像回滚；数据库迁移不自动降级，依据备份和迁移状态单独核对。
- REST 停止失败且 WebSocket 也无法送达时，前端恢复可重试状态并显示停止未送达，不伪装成已停止。
- 报告列表过滤、详情或导出任一权限检查不一致时，停止该类型的“已通过”结论并记录任务/账号/路由证据。
