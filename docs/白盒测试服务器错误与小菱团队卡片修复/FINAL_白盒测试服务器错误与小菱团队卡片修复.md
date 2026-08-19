# 最终报告

## 结论

本次服务器 500 已修复并完成公网验收。根因是归档规划器和 worker 链路把 GitHub 常见的 `tar.gz` 当作 ZIP 读取，触发 `BadZipFile` 并被包装成 500；现在统一使用安全归档读取器，并在 worker 启动前将非 ZIP 源码归一化为 ZIP。恶意文件隔离、路径校验和大小限制未绕过。

后续回归又确认生产 PHP worker 把 Deprecated/Warning 当作 Fatal/Parse，导致真实测试和动态用例都通过时仍判失败。现在 worker 只以 `Fatal error`、`Parse error` 或 `Errors parsing` 判定 PHP 语法失败，提示级输出仍完整进入报告。

小菱团队卡片改为绑定调用消息的 `teamIds`，SSE 期间独立轮询团队详情并缓存最小快照；关闭悬浮窗、切换新对话和从历史恢复时都保存并恢复活动会话，因此卡片不再依赖结论补充或当前页面瞬时状态。

## 代码与验证

- 生产运行 SHA：`fbf651d276317fe31cf487c2c511bf4c64ad3108`（包含归档、团队卡片、会话恢复、PHP worker 判定及 worker digest 固化）。
- 后端专项测试：82 passed；前端全量测试：35 个测试文件、238 passed；前端 `vue-tsc && vite build` 通过。
- 前端全仓 ESLint 0 error、0 warning；后端 Ruff、shell 语法和 `git diff --check` 均通过。

## 公网发布

2026-08-19T02:30:29Z 部署完成。服务器 HEAD、后端、前端同 SHA且 Git 工作树干净，数据库备份并完成隔离恢复校验（84 张表、Alembic 036）；五个生产容器均 healthy，域名 HTTPS 根入口 200。PHP 沙箱镜像已重建、固定 digest 并重启 executor。

任务 `sbx_caf6d82214ee4076960c040a` 的白盒创建接口返回 200，服务端无 5xx。任务成功完成源码部署、入口识别、测试生成、事实提取和报告发布，3/3 动态测试通过，最终 `executed=true, passed=true`、`exit_code=0`、`outcome=succeeded`。导出 6 个工件，包括 `sandbox-result.json`、JUnit、SARIF、HTML 和 16.2 KiB Markdown 审查报告。

小菱卡片公网验收已覆盖：历史对话展示、关闭后重开、新建对话后再打开历史对话，团队卡片和结论均恢复。
