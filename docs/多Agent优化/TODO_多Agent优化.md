# TODO_多Agent优化

## 1. 待办事项

| 优先级 | 待办 | 建议处理方式 |
| --- | --- | --- |
| P0 | 配置真实 `DEEPSEEK_API_KEY` | 在根目录 `.env` 填写,不要提交到版本库 |
| P0 | 演示前跑一次真实 `full` 审查 | 记录耗时和 token 消耗,确认余额充足 |
| P1 | 修复全量 ruff 历史问题 | 先运行 `ruff check app tests`,按模块逐个清理 |
| P1 | 补齐 API 集成测试 | 用 FastAPI TestClient 覆盖 auth/project/review/report |
| P1 | 审查任务异步化 | 后续引入 Celery/Redis 或 FastAPI BackgroundTasks |
| P2 | Monaco 包体积优化 | 按需加载语言包或配置 Vite manualChunks |
| P2 | Sass `@import` 迁移 | 将 `@import` 改为 `@use` |

## 2. 需要人工确认

- 多 agent 是否作为论文核心创新点之一展开描述。
- 答辩演示是否使用 `full` 多 agent,还是用 `standard` 保证现场速度。

