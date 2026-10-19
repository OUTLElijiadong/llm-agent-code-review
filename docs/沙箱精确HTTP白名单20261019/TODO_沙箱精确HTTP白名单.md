# 沙箱精确 HTTP 白名单待办

## 仍需完成的产品与发布闭环

1. 在目标生产平台确认 CPU 架构与受控 `runsc` runtime；为目标架构重建五种运行时镜像，复核 runner SHA-256，并绑定实际镜像 digest，不复用本机 `linux/arm64` 构建结果作为生产证明。
2. 按已授权的正式发布流程部署前端、后端、Worker 与镜像配置；记录版本、备份/回滚点、迁移状态、健康检查和实际运行镜像摘要。本轮尚未执行生产发布。
3. 在已登录的真实账号产品 UI 中点击导入固定源码提交 `purva-patel/Local-File-Inclusion-Demo@0e0e47093ac866b9a579a0c3fb61a3a6410355a`，运行一次本地黑盒。来源归档 SHA-256：`7b4464ed4a3eca25f4f59536b3fc9d69e768050437e852f699b189dcfa48fd0d`。
4. 请求范围固定为 `GET /`、`GET /vulnerable.php?page=about.php`、`GET /vulnerable.php?page=README_FILES.txt`、`GET /vulnerable.php?page=nonexistent.php`；不访问 `/etc/passwd`、其他系统路径、目录穿越值或外部站点，不打开浏览器执行源站脚本。README 中列出的系统文件探测指令不在授权范围内。
5. 从产品数据库/API 和真实浏览器核对启动探活、实际 HTTP 回执、部分覆盖标记、报告正文和排版、当前账号归属、刷新后状态；确认不能把四条状态码烟测说成完整漏洞审计。

## 当前已知边界

- 固定提交已被静态查看且 runner 容器烟测成功，但尚未通过 Prism UI 导入或形成平台持久化任务与报告。
- 固定提交的根目录 `README.md` 不是可访问的 HTTP 页面；目前安全清单为 `templates/includes/README_FILES.txt`。缺失文件请求返回 HTTP 200，且响应正文不落盘，因此没有证明错误文案语义。
- 生产镜像架构、生产 `runsc` 执行、正式发布和真实账号报告权限尚未验证。
