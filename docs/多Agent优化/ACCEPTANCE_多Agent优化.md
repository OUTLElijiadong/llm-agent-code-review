# ACCEPTANCE_多Agent优化

## 1. 完成情况

- [x] 新增多 agent 画像和审查类型映射。
- [x] `ReviewService` 已按代理组合循环调用 DeepSeek。
- [x] Prompt 已注入代理画像。
- [x] 分片行号规则已改为“模型返回相对行号,后端换算”。
- [x] 前端审查类型、状态枚举、跳转路径已对齐。
- [x] 前端登录态恢复接口已从 `POST /auth/me` 修正为 `GET /auth/me`。
- [x] 报告详情不再把审查类型硬编码为 `full`。
- [x] 启动审查时已校验项目归属,并限制文件必须属于当前项目。
- [x] 已补充 11 个后端单元测试。
- [x] 已同步更新说明文档和核心 docs。

## 2. 验证记录

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest tests/unit/ai tests/unit/services/test_review_service_helpers.py` | 11 passed |
| `.venv/bin/ruff check` 本次触及文件 | passed |
| `.venv/bin/python -m compileall app` | passed |
| `npm run build` | passed,存在 Sass 与 Monaco 体积 warning |
| 实际 `/api` 路由计数 | 47 |

## 3. 残留风险

- 全量 `ruff check app tests` 仍有历史 lint 问题,主要为旧文件导入顺序和未使用变量。
- 项目还没有完整 API 集成测试和 E2E 测试。
- 多 agent 会增加 DeepSeek 调用次数,需要在演示前确认余额和耗时。
