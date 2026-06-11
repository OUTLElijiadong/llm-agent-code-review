# 圆桌讨论 WebSocket 线上修复 - TODO

1. 建议后续把公网裸露的 `8000`、`3307` 端口收敛为内网或本机绑定，仅保留 `80/443` 对外；本次未调整端口暴露策略，避免影响现有运维访问方式。
2. 建议补一条自动化集成测试，覆盖 `/api/discuss/start` 到 `/api/ws/discuss/{session_id}` 的真实 WebSocket 握手。
