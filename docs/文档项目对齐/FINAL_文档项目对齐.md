# FINAL_文档项目对齐

## 总结

本次将文档口径从旧版本状态同步到当前项目事实:14 个 Agent、14 张业务表、87 条 HTTP API、1 条 WebSocket、当前默认模型 `deepseek-v4-flash`、上传硬限制 20MB、部署 nginx 位于 `frontend/nginx.conf`。

## 主要交付

- 更新 README 与 `说明文档.md` 的目录和数量口径。
- 更新需求、系统设计、API、后端、前端、部署、测试、开发计划文档。
- 给 v2/v3 专项文档补当前状态说明,避免历史数字与当前实现混淆。
- 新增 `docs/文档项目对齐/` 6A 工作流文档。

## 验证

最终验证结果:
- 后端:`115 passed`,覆盖率 `39%`
- 静态检查:`ruff` 通过
- 编译检查:`compileall app tests` 通过
- 前端:`npm run build` 通过,本次未输出构建警告
- Compose:`docker compose --env-file .env -f deploy/docker-compose.yml config --quiet` 通过
