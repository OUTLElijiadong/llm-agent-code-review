# 小菱上下文与移动端复测：验收记录

## 初始状态

- 生产初始版本：v4.0.22，SHA `2fa4dbae0f7c7de3620c4f40330c0f67616bf2aa`；候选版本为 v4.0.23，发布前复核生产状态。
- 真实手机视口：Safari 响应式模式 `390×844`、2 倍像素比。截图显示悬浮小菱按钮盖住“安全态势”卡片右侧 0 值及“24h 恶意文件”文字。
- 语义缺口：基线 `test_c13_semantic_coverage.py` 中两个 strict xfail 分别覆盖沙箱来源 ID 齐全但漏事实、Responses 来源锚点齐全但遗漏中段权限。尚未改候选代码前需先运行基线测试确认。
- 百万级证据边界：已有约 1.1M 应用估算 tokens 合成史与模拟模型测试；不表示真实 tokenizer、生产模型或完整会话 UI 验收。

## 候选修复与本地验证

| 项目 | 状态 | 证据/限制 |
|---|---|---|
| 手机端 AdminCopilot 遮挡及 Teleport 路由竞态 | 候选已修复；生产复测待做 | 入口移入管理页顶栏。独立复核在 `/admin` 槽位尚未挂载时复现 Vue Teleport 告警和入口不迁移；修复为目标出现后再迁移、等待期间保留 body 浮动入口。组件级路由/MutationObserver 回归通过。Vitest 1381 passed；Playwright 49 passed、9 skipped（58 项，含 320–1440px 多视口）；lint/build passed。视口自动化不是发布后 Safari 真机验收。 |
| 沙箱压缩保护用户约束 | 已复现并修复 | 修复前 strict xfail 有明确漏事实样本；按来源保护用户硬约束，预算不足明确失败。后端全量测试已覆盖。 |
| Responses 压缩保护中段权限 | 已复现并修复 | 修复前 strict xfail 有明确漏事实样本；压缩后核验保护事实和来源。后端全量测试已覆盖。 |
| 小菱主聊天 >1M 估算 token 压缩 | 合成模型测试通过；真实模型待验 | 1,100 条混合历史经平台保守估算超过 1,000,000 tokens；模拟压缩验证首/中/尾权限和更正保留、次序以及原文不变。该证据不代表真实模型调用。 |
| 多入口上下文压缩修复 | 候选本地回归通过 | 涉及主聊天、Responses、圆桌追问、临时 Agent 团队、白盒 source_context、全链审计、正式审查附加上下文、黑盒 exploit 背景；均以原来源/用户约束做账本，超预算失败关闭。黑盒修复仅覆盖 exploit 的 probe/recon 与长 finding 背景，不代表 pentest 其他阶段已验。 |
| 全量后端测试 | 通过 | `backend/.venv311/bin/python -m pytest tests -q -o addopts=''`：5363 passed、5 skipped、5 warnings，174.68 秒；此后仅补充测试用例，C12/圆桌定向复跑 68 passed。 |
| 后端静态检查 | 通过 | 本次修改文件 Ruff `All checks passed!`；`compileall -q app alembic/versions/036_performance_indexes.py` 通过；`git diff --check` 通过。 |
| 生产发布与复测 | 尚未开始 | 需真实模型压缩、备份/恢复门禁、精确 SHA 发布和发布后真实浏览器矩阵。 |

## 尚需的发布后证据

- 真实供应商调用执行超过百万平台估算 token 的内存内压缩；记录模型返回的 usage、分层压缩结果、首/中/尾事实问答。绝不把 API Key、测试长文本或凭据写入日志/数据库。
- 发布后健康、ready、release SHA、Alembic、运维巡检、HTTPS、5xx 日志和备份验证。
- Safari 真实点击管理员、审查员、普通账号；核验账号隔离、会话保存及顺序、刷新/异常退出恢复、权限拒绝、页面布局和无 404。生产聊天记录按用户此前授权保留。
- 逐屏核对圆桌进度、后台继续运行、晚到消息是否写入讨论上下文/最终结果，以及结束后五分钟追问窗口。

## 独立终审新增发现（2026-09-29）

- 独立复核在 Vue 3.5.30 + jsdom 复现 `/admin` out-in 路由过渡时 Teleport 目标缺失；目标后来出现也不会自动修复入口位置。候选采用短时 `MutationObserver` 等待槽位出现，等待期间 Teleport 保持在 `body`；目标到位后迁移并停止观察，离开管理页也会停止观察。
- 新增真实组件回归覆盖非管理路由 → 移动端管理路由 → 页头槽位延迟挂载；检查按钮从浮动位置迁入槽位，且没有 Teleport 目标告警。更新候选全量 Vitest 为 1381 passed，前端 lint 与生产构建通过。
- 这是候选组件级证据；生产 Safari 移动视口重放尚未执行，不据此宣称生产 UI 已验收。
