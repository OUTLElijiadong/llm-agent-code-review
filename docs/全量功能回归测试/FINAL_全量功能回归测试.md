# 全量功能回归测试 · 总结报告

## 结论

本轮已完成全站全量真实点击测试、DeepSeek QA 调用、日志监控、缺陷修复、回归验证和合成数据清理。测试期间发现的问题已修复并复测通过；最终 QA synthetic 数据残留为 `0`。

## 验证结果

| 项目 | 结果 |
| --- | --- |
| MySQL | `cr_mysql` healthy |
| 后端测试 | `112 passed`，覆盖率 `38%` |
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
