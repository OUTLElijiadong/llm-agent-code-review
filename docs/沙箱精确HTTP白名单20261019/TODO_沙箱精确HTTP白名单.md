# 沙箱精确 HTTP 白名单待办

## 仍需完成

1. 按正式发布流程发布一次修订版，采用三段式版本号 `4.1.12`。从活动发布树 `974ba0d68bd1ea3baa29bc50b6a7fdb383418070` 继续，保留现有回滚版本和数据库备份；构建前后端，重建并固定 Python/Node/Java/Go/PHP 五种运行时镜像，再同步 Worker 与活动 systemd 发布树。发布验收需核对活动 `ops-check`、健康接口、Runner SHA、五个 profile digest 和报告路由。4.1.12 修订已提交为固定候选并通过前端 27 项、后端 106 项针对性回归与前端生产构建；生产镜像构建和部署仍待完成。
2. 重新运行指定 PHP LFI 靶场需要新的单次运行授权。用户此前授权的一次生产执行已使用并以失败结束；不得自动重跑。取得授权后只运行一次，核对路径白名单、真实 HTTP 请求、任务终态、报告和资源回收。

## 已核对的生产基线

- 活动生产目录 `/opt/prism-releases/974ba0d68bd1ea3baa29bc50b6a7fdb383418070` 当前为 `4.1.11`；从该目录运行 `ops-check.sh` 返回 `status=ok`、`can_continue=true`。
- `/opt/code-review` 是旧 checkout，HEAD、`.env` 与旧账本仍指向 `f114033bb76af393e5ef595ca8b824ebde0de62a`。不得用它判断活动服务故障或直接重建默认生产环境。
- 活动五种 profile 镜像均与 `profiles.json` 固定摘要一致，runner SHA 为 `b441df2dc23e274a68b41a7a1befed8a0979429598c07c2d04cd2e7994d9a9a1`。本修订 runner SHA 为 `fa6afdd11e17c45feef91cc73b2903b25cd1583e9a391e8ac3a7ae50cc7e2ad1`，正式发布时五种镜像都必须重建。

## 已完成证据

- 生产版本 `4.1.11` / commit `974ba0d68bd1ea3baa29bc50b6a7fdb383418070` 已部署。
- UI 创建任务 `sbx_b12572a3c508401f9d305c16`，实际发出四条精确 GET 请求；其中三条返回 HTTP 200，根路径无响应。该证据确认强制根路径规则过严。
- Worker 确认容器与任务目录已清理。产品报告、结构化 JSON、执行日志、JUnit、SARIF 和 HTML 已生成；AI 动态测试与模型审查均按白名单策略跳过。
- 未发生重试、未请求系统敏感文件、未访问远程目标，响应正文未保存。
