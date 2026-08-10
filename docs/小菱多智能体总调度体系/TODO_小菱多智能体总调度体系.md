# TODO - 小菱多智能体总调度体系

## 已完成

- [x] 设计、开发、迁移、测试、生产部署和真实验收。
- [x] 同账户 user/admin 跨会话发现、消息投递、自动续跑和 UI 折叠链。
- [x] 生产 `.env` 凭证存在性确认，未复制或提交密钥。
- [x] 生产备份、健康、就绪、迁移 head、容器健康和数据库独立核验。
- [x] 临时验收账号软删除、令牌失效、角色撤销和会话归档。

## 非阻塞维护项

1. 全仓 Ruff 既有 10 项问题：位于 `app/agents/test_case_generator_agent.py`、`app/services/project_service.py`、`app/services/project_source_revision_service.py`、`app/services/sandbox_service.py`，与本任务新增 Agent Mesh 文件无关。
2. 可增加后台定时巡检，对离线会话的长期 `queued` 消息按 `expires_at` 聚合告警；当前消息状态机和手动 ACK/过期逻辑已可用。
3. 浏览器控制接口本轮无法切换视口，移动端未做浏览器截图；前端组件测试、窄屏 CSS 约束和生产构建已通过，后续可在真实移动设备补做截图回归。
4. 独立子代理复核因上游 429/连接中断未取得报告；主流程已完成确定性双重核验。待代理服务稳定后可再执行一次纯只读复核，不阻塞当前生产版本。

## 配置指引

本任务没有缺失的生产配置。DeepSeek 凭证仍只存服务器 `/opt/code-review/.env`，如需轮换请在维护窗口按现有发布脚本执行，禁止将密钥写入 Git、前端构建产物、截图或日志。
