# v4.0.78 候选发布说明

## 沙箱黑白盒可靠性

- 源码压缩在重复来源校验失败时对原文无损拆分、递归复核；不放宽原文覆盖、引用和来源 ID 校验，达到限制即失败关闭。
- 将 AI 动态断言与 HTTP 路由烟测分开记账。只有唯一预期黑盒文件和可信逐文件通过回执同时存在时才认定 AI 断言通过；未执行时显示 route_smoke。
- Node 测试 harness 使用 ESM .mjs，避免 package.json 的模块类型改变 harness 解释方式；Java harness 加入项目常见编译输出目录。
- 黑盒启动、白盒和 combined 测试执行前离线准备项目依赖；依赖不可用时不启动应用、不执行测试、不报通过；combined 共享一次准备状态。
- 后端报告与 Worker 执行回执保持分离，测试失败、启动失败、缺失回执、断言未执行和路由异常分别记录。

## 验证

- 完整后端：6,373 通过、6 跳过、6 warning，覆盖率 83%；6 个跳过项要求隔离 Redis 容器或 Unix socket。
- 仓库根目录全量：6,510 通过、8 跳过、6 warning。其中 6 个跳过项需要隔离 Redis；另 2 个来自 opt-in Linux 内核验收测试，启用还需要 PRISM_KERNEL_TEST=1、Linux root 和 ip/ipset/iptables/python3。根测试日志未记录这 2 项具体满足了哪个跳过条件。
- 黑盒/combined executor 定向 12 项通过；部署发布绑定 33 项和故障注入通过。
- 前端源码未改；既有前端 1,850 项测试、ESLint 和 Vite 构建通过。
- Ruff、runner shell 语法及 git diff 空白检查通过。

## 发布状态与边界

生产只读核验仍为 v4.0.77。SSH 公钥认证被拒，本候选没有上传、部署或做生产业务复测。真实 Docker Worker、隔离 Redis、Node/PHP/Go/Java 结构化源码 grounding，以及真实供应商对超过 1,000,000 token 输入的端到端语义验收仍未完成。候选测试通过不等于所有外部项目和运行条件下永不失败。
