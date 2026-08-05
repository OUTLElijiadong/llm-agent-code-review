# TODO：最高管理员管理 Agent 安全监控与主动告警（生产已部署）

> 更新日期：2026-08-05；生产已部署（deploy-security-monitor @ c77656b4，Alembic 027）

## 一、需要你确认/决策

1. **两把 ED25519 密钥归属待认领**（最重要）：
   - `SHA256:wbLkqbw/WvhqS4M84/JZO2Lm+LdU59ovc+N70q/SVf4`（6/12 从 217.28.137.70、8/4 从 45.135.228.155 以 root 登录）
   - `SHA256:QMGEeLXiu6IGGJj6thdv29zLHkWVUs0tUiyh2+ZAwDw`
   - 若不属于你本人/CI，请立即走 SSH 授权清理流程（写操作需唯一超级管理员审批）。**这是 Agent 上线后优先级最高的事件。**
2. **备份完整性**：5 份 2026-07-31 手工备份缺少 SHA256 校验文件（已产生 critical 告警）。请确认是否可补校验（`sha256sum`）或删除；保留期内备份应全部可校验。
3. **SSH 白名单**：生产已配置 `["117.141.0.0/16","39.144.0.0/16"]`（你常用家宽段）。如有其他办公/云服务器出口，请补充，否则会收到对应 high 登录告警。
4. **调度间隔**：`security_monitor` interval@5m 固定字面量；如想用 `SECURITY_MONITOR_INTERVAL_MINUTES` 控制需后续改造。
5. **生产部署分支与本地 main 分叉**：`deploy-security-monitor` 基于生产基线（含其他会话 8 月未提交工作）；本地 main 是另一条线。建议后续由你或其他会话把两边合并统一。

## 二、观察项（上线后）

- 前端右上角弹窗/未读队列：请以超级管理员登录 `https://lijiadong.cn` 验证（应看到离线期间产生的告警弹窗）。
- 安全态势查询：`GET /api/admin/observability/security/status?since_hours=24`（超管）。
- 手动巡检：`POST /api/admin/observability/security/run-monitor`（超管）。
- 告警已读：弹窗自动标记；也可 `POST /api/admin/observability/alerts/{id}/read`。
- `APP_RELEASE` 环境显示旧值（.env 内 3ffbfe）属展示问题，不影响功能，可后续更新 .env。

## 三、运维指引（简要）

- 备份清理/磁盘优化等写操作：必须通过管理 Agent 审批（critical 输入"确认执行"）。
- ip_attribution 使用 http://ip-api.com/json 被动溯源；如需 HTTPS/付费可改 `THREAT_INTEL_BASE_URL`。
- 发布树执行器已指向 `/opt/code-review/deploy/prism_ops_executor.py`（systemd 已更新）。
