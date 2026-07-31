# TASK：全服管理 Agent 与内测前检查

- [x] 获取生产服务器、Root 登录、管理员和普通用户验收授权
- [x] 盘点本地工作树、生产服务拓扑和现有运维 Agent
- [x] 复现 `prism-ops-check.service` 失败原因
- [x] 升级 `operations` 为全服管理 Agent 并同步治理职责契约
- [x] 扩充 Root 执行器整机只读、systemd、Docker、文件、软件包、防火墙和账户能力
- [x] 增加审计脱敏、文件回滚和执行器宿主机审计
- [x] 为 critical Responses 审批增加“确认执行”输入门禁
- [x] 建立 23 个 `/admin` 页面的完整动作矩阵
- [x] 实现固定、类型化的 `admin_execute_capability` 工具并复用真实 API/业务 Service
- [x] 增加页面路由、侧栏、OpenAPI 与 Agent 能力对齐契约测试
- [x] 修复角色数据范围页面的无读取盲覆盖风险，并补齐 RBAC 管理菜单
- [x] 补充后端、前端和部署执行器回归测试
- [x] 完成本地定向测试、类型检查、构建和脚本验收
- [ ] 创建生产备份并完成 SHA-256 与隔离恢复验证
- [ ] 精确部署、验证回滚点及全部既有服务状态
- [ ] 使用管理员和普通用户执行内测前浏览器端到端检查
- [ ] 由独立子 Agent 复核数据、权限、部署和验收证据
- [ ] 生成 ACCEPTANCE、FINAL 和剩余 TODO
