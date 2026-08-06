# Agent 项目选择模糊匹配与自定义输入 - 验收记录

## 进度

- [x] T1 失败测试
- [x] T2 纯函数实现
- [x] T3 选择器接入
- [x] T4 回归验证
- [x] T5 生产部署
- [x] T6 真实聊天验收

## 实现结果

- 后端 Clarify 推荐候选与项目 API 查询结果合并，按项目 ID 去重。
- 项目选择器输入关键词后，250ms 防抖调用 `/api/projects?keyword=...`。
- 选择“其他（自定义输入）”后显示项目名称或 ID 输入框。
- 自定义数字转换为 `project_id`；自定义名称转换为 `project_query`。
- 空自定义输入会被必填校验拦截，展示用哨兵值不会发送到后端。
- Clarify 下拉统一使用 `z-index: 3100`，高于 Agent 抽屉的 3000，项目、任务和普通选择项均可点击。

## 本地验证

- 失败基线：工具模块不存在，新增测试按预期失败。
- 前端定向：6 passed。
- 前端全量：56 passed。
- 后端定向：8 passed。
- 后端全量：1041 passed。
- ESLint、Ruff、生产构建、`git diff --check`：通过。

## 生产部署

- 备份目录：`/opt/code-review/backups/agent_project_selector_20260715_175818`。
- 回滚镜像：`deploy-frontend:agent-project-selector-before-20260715-175818`。
- 最终镜像：`sha256:8c952cc86c5561736df6f76cce7105a0c856b4c9d6e6f5ac2b211ee329ccfe78`。
- 仅重建并切换 Frontend，Backend、MySQL 和其他容器未重建。
- Frontend：`running=true`、`restart=0`、`oom=false`、HTTP/HTTPS 200。
- 收尾巡检发现 ClamAV 的 `clamd` 曾在 Frontend 构建内存峰值期间被主机 OOM 终止；重启 `cr_clamav` 后容器恢复 `healthy`，容器内检查为 `Clamd is up`，Backend 通过私有网络向 `clamav:3310` 发送协议级 PING 并收到 PONG。

## 真实聊天验收

- 输入“请帮我查看一个项目的代码文件”，Agent 正确触发 `project_id` Clarify。
- 未输入关键词时显示当前 13 个项目和“其他（自定义输入）”，不再只有一个候选。
- 输入“皮卡丘”后 `/api/projects?keyword=皮卡丘` 返回 200，下拉仅显示“皮卡丘漏洞平台”“皮卡丘漏洞靶场”和“其他”。
- 下拉弹层计算层级为 3100，选项可真实点击。
- 选择“其他”后出现自定义输入框，可输入“皮卡丘漏洞”。
- 提交后 `/api/agents/clarify` 返回 200，并生成下一轮项目确认，没有把展示哨兵值当项目 ID。
- 浏览器应用控制台错误和警告为 0。

## 验收结论

项目较少时可直接选择，项目较多时可按名称远程模糊搜索；未命中或不在当前候选中的项目可通过“其他”输入名称或 ID。功能、接口、权限边界和生产运行状态全部通过验收。
