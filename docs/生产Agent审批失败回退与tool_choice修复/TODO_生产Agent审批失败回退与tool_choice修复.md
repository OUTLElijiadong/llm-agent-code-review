# TODO：待办与操作指引

## 用户侧（推荐立即做）

1. 打开管理员的管理 Agent（小菱），当前会话会自动恢复 run_ede09… 的失败运行，点击标题栏 **“重试运行”**，验证 run 从失败点继续并完成。
2. 若该会话被其它新会话覆盖，可在会话下拉选择原会话；重试只续跑模型循环，**不会重放已批准的写操作**（审批已全部 approved，告警已处置）。

## 工程侧待办

0. **后端镜像 tag 被并行构建取代（已核实无风险）**：部署约 4 分钟后后端被切换为 `bb-signal2-08051801`（已核实该镜像内含本次修复，md5 与 /opt/prism-current 补丁一致）。请确认该 tag 的构建来源（可能是你的构建自动化/并行操作）；若它基于 /opt/prism-current 构建，后续重建不会丢修复。
1. **生产源码与本地仓库对齐**：本次仅同步了 6 个受影响文件 + 服务器版 agent_responses_service.py 的单点补丁。服务器 frontend 的 `codeFile.ts`、`ProjectDetail.vue` 比本地新（quarantine 隔离上传），本地 main 缺少这些改动，建议补一次 `chore(prod-sync)` 提交把生产在运行但未提交的改动回收到仓库（如先前 ae712da 的做法）。
2. **后端全量镜像**：本次后端用叠加镜像（FROM docroot-fix + 3 文件覆盖）规避 pypi 直连过慢。建议后续在服务器配置 `PIP_INDEX_URL` 镜像（如清华/阿里 pypi）后执行一次干净的全量构建，使镜像与源码树完全一致。
3. **本地仓库基线**：本地 main 与 origin/main 已分叉（本地 1 个提交 vs 远端 7 个）；工作区还有 sandbox_service.py 等未提交改动，建议按 RELEASE_CHECKLIST 梳理干净提交后再走正式发布流程。
4. **503 重试与知识库 RAG 未部署**：本地 agent_responses_service.py 含上游 503 有限重试与操作知识库 RAG，生产版本未包含（本次为最小补丁）。生产 08-04 曾出现 503 failed 记录，建议下一轮发布一并带上。
5. **监控**：观察 24h 内 agent_response_run 是否再出现 `tool_choice` 400 类 failed；可在运维告警中增加该关键字。
