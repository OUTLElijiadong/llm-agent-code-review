# FINAL_全量项目安全扫描

## 项目总结

本次新增“全量项目安全扫描”能力，并保留原有单项目扫描能力。全量扫描已扩展为接口扫描、项目代码联动性分析和多 Agent 讨论式审查。

## 交付内容

- 后端新增 `SecurityScanAllProjectsIn` 和 `POST /api/security/scan-all-projects`。
- `SecuritySentinelAgent.scan_all_projects` 按当前用户权限查询活跃项目，复用 `scan_project` 逐项目扫描并聚合结果。
- 项目级威胁模型新增 `api_endpoints`，可抽取常见后端路由和前端 HTTP client 调用。
- 项目级威胁模型新增 `code_links`，可表达接口到危险接收点、跨文件数据流等代码联动关系。
- 全量扫描新增 `discussion`，输出安全、可靠性、性能、可维护性和主持 Agent 的讨论摘要、共识和行动项。
- 聚合结果复用 `SecurityScanOut`，包含 findings、风险评分、扫描文件数、接口清单、代码联动、跨文件数据流、项目跳过信息。
- 前端新增 `scanAllProjects` API 封装和增强后的安全审计类型。
- `SecurityScanModal` 新增 `all-projects` 模式，支持每项目扫描文件数、跨文件数据流追踪配置，并展示接口、联动关系和讨论结论。
- `SecurityCenter` 新增“全量扫描”入口，同时保留“单项目扫描”跳转。
- 同步更新 API 文档和 `说明文档.md` 进度记录。

## 验证结果

- 后端单测：21 passed。
- 后端编译：通过。
- 后端 Ruff：通过。
- OpenAPI：`/api/security/scan-all-projects` 已注册，响应 schema 包含 `discussion`、`api_endpoints`、`code_links`。
- 前端构建：`npm run build` 通过。
- 前端轻量服务检查：Vite `/security` 返回 HTTP 200。

## 说明

未执行真实 DeepSeek 扫描，避免触发外部模型调用；未启动后端和 MySQL 做登录后实点。当前环境未安装 Playwright，未做登录后浏览器交互。功能已通过单元测试、接口注册、前端构建和轻量服务检查覆盖主要逻辑。
