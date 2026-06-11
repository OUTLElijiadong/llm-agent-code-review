# 全量功能回归测试 · 总结报告

## 结论

本轮已完成全站全量真实点击测试、DeepSeek QA 调用、日志监控、缺陷修复、回归验证和合成数据清理。测试期间发现的问题已修复并复测通过；最终 QA synthetic 数据残留为 `0`。

## 验证结果

| 项目 | 结果 |
| --- | --- |
| MySQL | `cr_mysql` healthy |
| 后端测试 | 本轮原始结果 `112 passed`，覆盖率 `38%`；2026-06-05 文档对齐复核为 `115 passed`，覆盖率 `39%` |
| 后端静态检查 | Ruff、compileall 通过 |
| 前端构建 | `npm run build` 通过 |
| API 数据核对 | `_qa_crosscheck.py` 全部通过 |
| 清理后 DB 核对 | QA project/file/version/task/task_file/issue/report/ai_log/rule/audit_log 均为 `0` |
| 导出回归 | Word DOCX、PDF 文件签名和响应类型正确 |
| 服务日志 | 已执行请求未出现未处理异常 |

## 已关闭缺陷

1. 修复 Word/PDF Blob 下载被 Axios JSON 拦截器误判失败。
2. AI 修复提示词改为显式生成，外部润色增加二次确认。
3. AI 提示词跳过二进制 Base64 上下文，并限制脱敏文本长度。
4. 固定 bcrypt 兼容版本，修复本地交叉核对代理影响。
5. 修复 Monaco 编辑器旧版本同步、路由重挂载、报告导出、多文件任务追踪和 tag 类型警告。
6. 新增规则页搜索，支持大量规则下定位自定义规则，并兼容下划线/短横线丢失的输入场景。

## 清理摘要

通过 UI 真实点击删除 QA 任务、QA 项目和 QA 规则；随后使用限定 ID/名称的 SQL 清理软删除与关联残留。清理范围仅限合成测试对象，正常项目、文件、任务、问题和 AI 日志未被删除。

## 剩余风险

- 应用内 Browser 后半程连接不可用，本轮使用 Safari Computer Use 完成真实点击验证。
- 前端构建仍有既有 Sass 弃用提示、Rollup pure 注释提示和 Monaco 大 chunk 警告，不影响功能通过。
- Agent 自进化审批/回滚、真实用户禁用/密码重置等生产破坏性动作只验证到确认路径，未对正常数据执行最终提交。
- 2026-06-05 复核后自动化测试数量增加到 `115 passed`，覆盖率仍低于核心文档目标，后续优先补 API/导出/讨论链路测试。

## 2026-06-10 复测补充

本轮围绕“所有 API 接口和真实按钮可用”做专项复测。后端在 Docker MySQL 上完成迁移、`141 passed` 单元测试、ruff 和 compileall；前端 `npm run build` 通过。业务接口按排除 Swagger/OpenAPI/Redoc 的口径枚举为 `93` 条 HTTP + `1` 条 WebSocket，其中 `93` 条 HTTP 已完成真实请求冒烟，结果 `PASSED=93 FAILED=0`。

真实按钮验证使用临时 Chrome 独立用户数据目录执行，覆盖登录、顶部搜索、Agent 抽屉、仪表盘、项目管理、项目详情、代码中心、审查任务、问题追踪、报告、Agent 中心、审查规则、安全中心、个人中心和管理页。首轮脚本有选择器误判，针对失败点复测后 `20` 项全部通过，无 API 4xx/5xx、无前端 console error。QA 项目 `id=6/7` 已清理。

本轮修复了 MySQL 迁移中 `user_api_config.user_id` 与 `user.id` 外键类型不一致的问题，并清理 API 配置相关代码的 ruff 问题。真实启动外部大模型审查、真实禁用用户、真实重置密码、Agent 自进化审批/回滚仍只验证到安全边界，未对正常数据执行最终破坏性提交。
