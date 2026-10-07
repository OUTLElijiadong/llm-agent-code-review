# Prism v4.0.79 候选发布说明

v4.0.79 针对反复失败的黑盒/白盒源码 grounding。多文件源码以原文进入核验，路径元数据分开保存，避免 JSON 转义破坏逐字引文；递归压缩只提交来源 ID 与当前正文；Python 黑盒动态端口检查扩展到间接调用、函数参数/返回值、反射、operator.methodcaller、`os.environb` 和别名写入，并保留只读环境 helper 的安全正例。普通 runner 与部署注入 runner 的黑盒执行在启动前准备离线依赖，失败不会伪报成功；就绪超时或退出时回收应用进程组和子进程。

候选后端最新完整回归 6495 passed / 6 skipped；6 个 Redis 场景按配置跳过，另以隔离 Redis 实测。最终黑白盒/源码/runner 定向回归 244 passed，覆盖动态反射和复杂合法 URL 的正负边界，以及 runner 的 SIGINT/SIGTERM 中断清理。前端 1850 passed，静态检查与 v4.0.79 构建通过。此文件描述候选，不能代替正式发布和生产 Safari/Worker 验收；生产当前仍为 v4.0.78，边界见 ACCEPTANCE_黑白盒可靠性.md。
