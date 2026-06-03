# ALIGNMENT_多Agent优化

## 1. 原始需求

用户希望检查项目还有哪些改进点,加入多 agent 概念,核对文档是否与实现对齐,并同步更新文档。

## 2. 项目上下文

- 后端为 FastAPI + SQLAlchemy + Pydantic v2,当前虚拟环境为 Python 3.9.6。
- 前端为 Vue 3 + Vite + Element Plus,审查启动页已提供 `standard/security/performance/full` 等审查类型入口。
- AI 审查链路为 `ReviewService -> CodeChunker -> PromptBuilder -> DeepSeekAgent -> ResultParser -> ReviewIssue`。
- 实际 `/api` 路由数为 47 条,数据库模型表为 9 张。

## 3. 已识别问题

| 类别 | 问题 | 决策 |
| --- | --- | --- |
| AI 架构 | 只有单一通用审查代理,难体现多 agent 概念 | 新增代理画像与审查类型映射,复用现有 DeepSeek 调用链路 |
| 行号处理 | Prompt 要求模型加偏移,服务层又二次加偏移 | 改为模型返回分片内相对行号,后端统一换算 |
| 前端路由 | 审查启动后跳转 `/review/task/:id`,实际路由是 `/reviews/:id` | 修正跳转路径 |
| 状态枚举 | 前端使用 `processing/completed`,后端使用 `running/success` | 前端状态展示和筛选对齐后端 |
| 文档口径 | Python 版本、路由数量、表数量、AI 模块职责不一致 | 统一按当前实现修正文档 |
| 测试 | `backend/tests` 缺失,虚拟环境未安装 pytest | 新增多 agent 单元测试,补装开发依赖并验证 |

## 4. 边界确认

本次不新增数据库表、不引入 Celery/Redis、不切换 LLM Provider、不做异步任务系统。多 agent 作为审查编排增强,通过多次 Prompt 调用和结果去重实现。

## 5. 疑问澄清

当前需求可以基于现有项目自动决策,无必须中断确认的问题。多 agent 默认落在 `full/security/performance` 审查类型中,保持 `standard` 低成本。

