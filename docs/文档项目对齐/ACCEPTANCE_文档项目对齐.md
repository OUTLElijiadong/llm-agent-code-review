# ACCEPTANCE_文档项目对齐

## 验收记录

| 检查项 | 状态 | 证据 |
| --- | --- | --- |
| API/Agent 运行时取证 | 已完成 | `87` HTTP、`1` WebSocket、`14` Agent |
| 顶层文档对齐 | 已完成 | README、`说明文档.md` 已更新 |
| 核心文档对齐 | 已完成 | `docs/01-11` 已更新关键差异 |
| 专项文档当前注记 | 已完成 | v2.0、安全哨兵、自进化、回归与日志文档已补当前口径 |
| 自动化验证 | 已完成 | 后端 `115 passed`/覆盖率 `39%`;`ruff`、`compileall` 通过;前端 `npm run build` 通过且本次未输出构建警告 |
| Compose 配置解析 | 已完成 | `docker compose --env-file .env -f deploy/docker-compose.yml config --quiet` 通过 |

## 当前风险

- 自动化覆盖率当前为 39%,未达到核心测试文档目标 70%;这是测试建设待办,不是本次文档对齐阻塞项。
