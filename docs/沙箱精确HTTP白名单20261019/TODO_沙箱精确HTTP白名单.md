# 沙箱精确 HTTP 白名单待办

## 当前待办

1. 完成当前配置好的唯一一次 PHP 靶场黑盒运行。当前动作时确认待用户答复；已限定为三条本机路径，根路径与远程目标均为空。运行后核对实际请求、任务终态、报告与资源回收；不得重试或扩大扫描范围。
2. 服务器磁盘使用率为 85%，达到运维告警阈值。用户表示将手动清理；Codex 未运行清理或删除操作。清理后再次执行只读 `ops-check.sh` 确认状态恢复。

## 已核对的生产基线

- 活动生产目录 `/opt/prism-releases/974ba0d68bd1ea3baa29bc50b6a7fdb383418070` 当前为 `4.1.11`；从该目录运行 `ops-check.sh` 返回 `status=ok`、`can_continue=true`。
- `/opt/code-review` 是旧 checkout，HEAD、`.env` 与旧账本仍指向 `f114033bb76af393e5ef595ca8b824ebde0de62a`。不得用它判断活动服务故障或直接重建默认生产环境。
- 活动五种 profile 镜像均与 `profiles.json` 固定摘要一致，runner SHA 为 `b441df2dc23e274a68b41a7a1befed8a0979429598c07c2d04cd2e7994d9a9a1`。本修订 runner SHA 为 `fa6afdd11e17c45feef91cc73b2903b25cd1583e9a391e8ac3a7ae50cc7e2ad1`，正式发布时五种镜像都必须重建。

## 已完成证据

- 生产版本 `4.1.11` / commit `974ba0d68bd1ea3baa29bc50b6a7fdb383418070` 已部署。
- UI 创建任务 `sbx_b12572a3c508401f9d305c16`，实际发出四条精确 GET 请求；其中三条返回 HTTP 200，根路径无响应。该证据确认强制根路径规则过严。
- Worker 确认容器与任务目录已清理。产品报告、结构化 JSON、执行日志、JUnit、SARIF 和 HTML 已生成；AI 动态测试与模型审查均按白名单策略跳过。
- 未发生重试、未请求系统敏感文件、未访问远程目标，响应正文未保存。
- 4.1.12 已部署，提交 `80bac2cc56640d65477926f1820c2abbe90046ae`；生产 HTTPS `/healthz`、`/readyz` 返回版本与 SHA 匹配。只读巡检的发布、容器、备份、Alembic、HTTPS、内存均通过，磁盘 85% 告警是唯一降级项。
- 新任务已在真实产品页面配置为 PHP 黑盒测试，精确白名单仅含 `/vulnerable.php?page=about.php`、`/vulnerable.php?page=README_FILES.txt`、`/vulnerable.php?page=nonexistent.php`，三条均期望 200；项目固定提交 `0e0e47093ac866b9a579a0c3fb61a3a6410355a0`，无远程目标。任务仍未提交或运行。
