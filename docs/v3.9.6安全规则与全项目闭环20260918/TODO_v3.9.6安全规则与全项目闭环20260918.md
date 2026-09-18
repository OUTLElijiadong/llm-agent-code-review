# TODO_v3.9.6安全规则与全项目闭环20260918

## 发布前必须完成

- [x] 最后一轮后端全量测试零失败（4178 通过、5 跳过）。
- [x] 独立只读终审发现的 multipart 异常资源清理阻断已修复，并由新增回归与全量测试验证。
- [x] 提交中不包含 `.claude/launch.json` 与 `backend/uv.lock`。
- [x] 生产数据库备份与独立恢复校验通过（97 张表）。
- [x] 发布后核对版本、SHA、唯一迁移 head、容器健康和公网同源接口。
- [x] 对两条已证实的 QA/验收临时账号执行软停用与 token 失效，不删除关联历史。
- [ ] Safari 登录页已确认 `v3.9.6`；后台页面二次登录仍需用户在 macOS 密码库提示中解锁后，补核统一页面、测试数据隐藏和兼容跳转截图。

## 发布后仍需外部条件

- [ ] GitHub 上确认新 CodeQL 工作流至少一次真实成功运行；本地配置通过不等于 GitHub 告警已产生。
- [x] 已执行真实 DeepSeek `full` 全项目任务 #178：5/5 文件、33 条发现、覆盖账本完整，8 次首轮截断经提升预算重试恢复，总 Token 278,036。
- [ ] 为平台上传项目设计受控 CodeQL CLI 沙箱、SARIF 导入和 Finding 合并；当前仅有 Prism 仓库 CI。
- [ ] 为 Codex Security Deep Scan 提供 managed read-only filesystem permission profile 后重跑；当前宿主环境阻断。
- [ ] 把普通文本字段的动态 SQLi/XSS/Unicode 攻击字典纳入持续生产前测试；当前已覆盖高风险接收点和 Schema 合同，不宣称每个输入框都做过浏览器动态攻击。
