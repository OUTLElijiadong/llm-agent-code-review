# 自动封禁待完成事项

1. 最新源码的 Linux 隔离网络复测已通过：宿主 INPUT、Docker DNAT 443、60 秒内核自动到期及人工解封均通过，2 场景共 96.338 秒；测试使用独立网络命名空间和合成日志，不改生产防火墙。见 `证据/内核隔离回归.txt` 与 `证据/内核源码指纹.txt`。
2. v4.0.44 生产发布、备份隔离恢复和运行 SHA / APP_VERSION / 迁移 / healthz / readyz / HTTPS 尚待执行。
3. 生产保护配置、Safari 真实点击启用规则与小菱异常研判、root verified 回执尚待执行。
4. 需记录生产 IPv4/IPv6 能力、权限与复测结果，并删除隔离网络测试目录。
5. 完成知识 Harness validate/postflight，再由独立复核重新检查最终验收记录与生产数据。
