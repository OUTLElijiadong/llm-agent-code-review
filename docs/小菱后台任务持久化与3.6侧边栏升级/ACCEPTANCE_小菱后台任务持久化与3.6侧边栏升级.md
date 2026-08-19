# 验收记录：小菱后台任务持久化与 3.6 侧边栏升级

## 当前状态

- [x] 生产开发者论坛 v3.5 发布记录已核实。
- [x] 前端卸载误中止和服务重启不续跑根因已复现到代码路径。
- [x] Responses 自动恢复实现；原生 waiting_approval/waiting_input 保留 pending。
- [x] 常规审查自动恢复实现；数据库 execution_token 防止多 Worker 竞态。
- [x] 小菱订阅生命周期修复；卸载不再 abort，显式停止仍 cancel。
- [x] 3.6 版本固化与发布脚本测试。
- [x] 悬浮岛/可折叠侧边栏实现与前端构建验收。
- [x] 公网部署、健康检查和真实浏览器验收。

## 证据边界

本文件只记录已实际执行的验证。代码检查、自动测试和公网验收完成后再补充准确命令、数量、release SHA 与页面证据。

## 已执行证据

- `ruff check`：通过。
- `python3 -m py_compile`：通过。
- 后端相关回归：147 passed（健康契约、Responses/SSE、审查状态、沙箱执行、稳定 Worker 信封与租约故障窗口回归）。
- 前端侧边栏组件回归：2 passed（版本显示、普通用户入口、折叠持久化）。
- 前端 `eslint`、`vue-tsc`、`vite build`：通过。
- 部署 Shell/运维契约：`deploy shell and operations tests: PASS`。
- Alembic head：`040`，迁移链 `036 -> 037 -> 038 -> 039 -> 040` 连续。
- 健康接口回归已同步为 `status/version/release` 正式契约并通过。
- 生产发布强制 `all` 双镜像同版本，禁止部分发布造成 3.6 展示与健康契约漂移。
- Responses 与沙箱启动恢复取消固定 200 条上限，启动时处理全部遗留活动任务。
- Alembic 全历史离线 SQL 生成会被既有迁移 `010` 的在线查询写法阻断；新迁移在生产发布中由真实 MySQL 在线执行并验收。
- 最终生产发布完成：`release=3cd8882034415b0b8a64d3d1f4702524500dab48`，双镜像均为同一 release，数据库 Alembic revision 为 `040`。
- 生产备份完成：`./backups/code_review_20260819T114521Z_3cd888203441.sql.gz`（371852584 bytes）；备份隔离恢复检查通过，基线为 84 张表、Alembic `040`。
- 公网 `https://www.lijiadong.cn/healthz` 返回 `status=ok`、`version=3.6.0` 和上述 release；`/readyz` 返回 `status=ready`，首页 HTTP/2 200。
- 真实浏览器登录后确认侧边栏显示 `v3.6 · PRISM`，包含工作区、智能审查、Agent 与安全、社区与支持、个人空间五组；整体收起和展开均正常，原有权限入口仍可见。
- 真实浏览器打开小菱历史对话，确认“子 Agent 协作团队”卡片和“13 步完成”状态均由服务器恢复；关闭悬浮窗后重新打开，团队卡片和完成状态仍存在。
- 生产开发者论坛已核实 v3.5 置顶发布公告，因此本次版本号按既定规则升级为 v3.6。
- 发布中复现并修复 MySQL `general_log` 无界增长：25 GB 原件备份到 `backups/mysql-general-log-20260819T192021/`，系统表修复并截断，生产配置改为 `--general-log=0`；最终发布后文件大小连续观测无增长。
