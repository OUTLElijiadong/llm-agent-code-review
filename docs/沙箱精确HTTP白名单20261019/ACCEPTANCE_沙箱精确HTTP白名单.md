# 沙箱精确 HTTP 白名单验收记录

日期：2026-10-19

## 代码验收

- 后端 schema：本地黑盒请求必须携带 1–20 条精确 GET 规则且包含无查询参数的 `GET /`；拒绝通配路径、目录穿越、百分号编码、重复目标、超限查询串、非 loopback 数据库及其他测试模式携带白名单。
- Worker：独立验证同一契约，将规范计划纳入请求摘要，并只生成固定 runner 环境字段；无法由请求覆盖命令、镜像、挂载或额外环境变量。
- Runner：启动目标前接管并清除编码计划；只做 TCP 就绪检查，再按计划向固定 loopback origin 发 GET；不跟随重定向、不记录正文；跳过归档中的启动补丁、PoC 与 Agent 测试脚本。日志截断时后端拒绝回执。
- 报告：服务端逐项核对固定计划和 runner 回执；包含目标、状态、字节数、耗时、断言状态及覆盖缺口；明确声明白名单外路径和 AI 动态用例未覆盖。
- 前端：仅在本地黑盒显示精确请求编辑器；验证失败禁止提交；结果用表格展示，不输出原始回执或响应正文。
- 沙箱五种运行时镜像默认值均固定到 runner 摘要 `c98adc1104c19e91a1f138a8bb1885182ed51e02680a5abdf861e667bb709d45`。

## 已执行检查

| 检查 | 结果 |
|---|---|
| `frontend/src/views/sandbox/SandboxWorkstation.test.ts` | 最新代码 27 项通过 |
| 前端生产构建 | 最新代码 `vue-tsc && vite build` 成功 |
| 沙箱 HTTP scope schema 测试 | 15 项通过 |
| 报告和 Worker 回归 | `test_sandbox_report_publish.py` + `test_prism_sandbox_executor.py`：178 项通过；出现 1 条 Python `crypt` 弃用警告 |
| Worker scope 独立验证 smoke | 通过；接受有效精确计划，拒绝编码路径和查询越界 |
| runner 本地 HTTP 集成 | 通过；仅出现 `/` 与 `/healthz?name=allowed` 两个请求；目标进程未获得白名单环境变量；归档启动补丁和 AI 脚本未运行 |
| runner 非法多层编码查询集成 | 通过；返回 `scope_invalid`，应用启动标记不存在 |
| 五种运行时镜像构建 | Python、Node、Java、Go、PHP 全部构建成功；当前主机产物均为 `linux/arm64`；镜像标签和 `/opt/prism/runner.sh` SHA-256 均为 `c98adc1104c19e91a1f138a8bb1885182ed51e02680a5abdf861e667bb709d45` |
| Docker 内 runner 正向/负向验收 | PHP 镜像隔离容器中白名单请求成功；多层编码路径返回 `scope_invalid` 且未产生任何 HTTP 请求 |
| 固定 PHP LFI 提交 runner 烟测 | `purva-patel/Local-File-Inclusion-Demo@0e0e47093ac866b9a579a0c3fb61a3a6410355a0`：禁网、只读根文件系统下仅发送 `GET /`、`GET /vulnerable.php?page=about.php`、`GET /vulnerable.php?page=README_FILES.txt`、`GET /vulnerable.php?page=nonexistent.php`；状态均为 200。只记录状态、响应字节数和耗时，不保存正文；未做内容语义断言 |
| Python `compileall`、`bash -n`、`git diff --check` | 通过 |
| Worker 五镜像摘要核对 | 五个值均与 runner 当前 SHA-256 一致 |

## 尚未完成的产品与发布验收

- Docker 本地构建平台是 Apple Silicon `linux/arm64`；尚未确认生产架构或在生产架构构建并绑定镜像 digest，不能把本地镜像直接作为生产镜像。
- 当前容器测试走 Docker 本地默认 runtime，不代表已在产品配置的 `runsc`/gVisor runtime 上通过。
- 已运行固定提交靶场的 runner 烟测，但尚未在 Prism 前端真实点击导入项目、创建任务并检查持久化报告归属、刷新后状态及报告排版；因此该烟测不能当作产品端到端或生产成功记录。
- 本靶场根目录 `README.md` 不在 PHP 文档根下；请求的是项目内的安全示例清单 `README_FILES.txt`。缺失文件请求的 HTTP 状态为 200，runner 未读取正文，所以不能据此断言错误文案的语义正确。
- 未执行生产发布；迁移、线上健康检查、生产账号报告权限及回滚证据均待发布验收。

## 验收结论

产品代码和本机自动化/容器 runner 验收已完成；固定来源的受限 HTTP 烟测通过。尚无证据证明功能已部署、已在产品 UI 导入靶场、已持久化完整报告或在生产环境可用。生产架构构建、runsc 验收、真实产品 UI/报告核验及正式发布仍未闭环。
