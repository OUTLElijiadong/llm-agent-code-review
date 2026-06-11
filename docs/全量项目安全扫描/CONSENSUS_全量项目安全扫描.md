# CONSENSUS_全量项目安全扫描

## 需求描述

在现有 SecuritySentinel 安全审计能力上新增“全量项目安全扫描”。用户可在安全中心一键扫描当前账号可见的所有活跃项目；同时单个项目的安全审计按钮、API 和结果展示保持不变。全量扫描需要覆盖接口扫描、项目代码联动性分析，并输出多 Agent 讨论式审查结论。

## 技术方案

- 后端新增 `SecurityScanAllProjectsIn` 输入 Schema。
- 后端新增 `POST /api/security/scan-all-projects`。
- `SecuritySentinelAgent` 新增 `scan_all_projects`，按权限查询活跃项目并复用现有 `scan_project`。
- `scan_project` 增强威胁模型：扫描常见 Python/Node/Java/Django 路由和前端 HTTP client 调用，输出 `api_endpoints`。
- 基于接口、危险接收点和数据流生成 `code_links`，体现接口到同文件 sink、跨文件路径等代码联动关系。
- `scan_all_projects` 聚合每个项目的接口、联动关系和数据流，并生成同步 `discussion` 多 Agent 摘要。
- 聚合返回：
  - `findings`: 所有项目 findings 合并，`file_path` 加项目名前缀。
  - `threat_model`: 合并接口清单、跨文件数据流、代码联动关系，并标注项目上下文。
  - `discussion`: 安全、可靠性、性能、可维护性、主持 Agent 的讨论摘要、共识和行动项。
  - `risk_score`: 基于全部 findings 严重度重新计算。
  - `file_count`: 所有成功扫描项目的文件数总和。
  - `summary`: 汇总项目数、跳过数、严重度分布和风险评分。
- 前端类型和 API 封装新增 `scanAllProjects`。
- `SecurityScanModal` 新增 `source="all-projects"` 模式。
- `SecurityCenter` 增加全量扫描弹窗入口；保留跳转项目列表用于单项目扫描。

## 约束

- 不改变已有单项目扫描接口行为。
- 不引入新依赖。
- 不新增数据库迁移。
- 全量扫描仍可能触发多次 LLM 调用，前端需给出耗时提示。
- 多 Agent 讨论本次采用同步摘要，不依赖实时 WebSocket 圆桌会话。

## 验收标准

- `scan-all-projects` 在无项目时稳定返回空结果。
- 有多个项目时结果能聚合 findings、文件数、风险评分、接口、代码联动关系。
- 单项目和全量项目扫描结果都能返回 `api_endpoints`、`code_links`、`discussion` 相关结构。
- 前端 `vue-tsc` 通过。
- 后端相关单元测试通过。
- `说明文档.md` 与 API 文档同步记录新能力。
