# TODO：待办与操作指引

## 用户侧（推荐立即做）

1. 打开管理员的管理 Agent（小菱），当前会话会自动恢复 run_ede09… 的失败运行，点击标题栏 **“重试运行”**，验证 run 从失败点继续并完成。
2. 若该会话被其它新会话覆盖，可在会话下拉选择原会话；重试只续跑模型循环，**不会重放已批准的写操作**（审批已全部 approved，告警已处置）。

## 追加修复（2026-08-06 结论守卫）

- **修复"工具链中断/错误不输出结论"**：`_admin_completion_guard` 未调用写工具时，
  仅"声称写操作已完成"需要证据拦截；模型诚实输出"无需处理/失败/说明性结论"时放行，
  不再清空结论并重试后 failed（此前"请运行不存在的操作"会被守卫吞掉结论）。
- `_MUTATION_SUCCESS_PATTERNS` 补充"操作完成/已处理/处理完成"等通用完成声明，
  无写工具却声称完成仍拦截（防编造）。
- 验证：本地 115 项测试全过；生产容器守卫行为 8/8、复现 7/7；
  真实冒烟"请运行 prism_ops.does_not_exist" → response.completed 并输出完整结论。
- 生产镜像：`prism-backend:agent-conclusion-fix-08061000`（基于 agent-retry-guard-08060925 overlay）。
- 服务器提交：`a46d755`；远端 main：`ab0727a`。

## 追加修复（2026-08-06）

- **发现并修复完成守卫误判**：只读查询（如"请只读查询当前未解决告警数量"）被 `_ADMIN_MUTATION_REQUEST`
  中的"解决"等动作词误判为写请求，导致 run failed（`管理写请求在没有精确工具执行证据时就结束了`）。
  已加只读意图短路：强只读词（查询/查看/统计/数量等）且无强写动词 → 不算写请求；
  强写动词排除"已/未/待/不"完成态修饰；复合写指令（查询后删除）仍判写。
- 生产验证：守卫行为测试 10/10、复现测试 7/7、真实登录冒烟（之前失败场景）completed。
- 生产镜像：`prism-backend:agent-retry-guard-08060920`（基于最新部署镜像 1470af9f overlay）。
- 服务器生产源提交：`9770256` + `310aabd`（deploy-security-monitor）；远端 main：`2d44ad2` + 合并 `c791d6c`。

## 已解决（2026-08-05 最终）

- 生产真正源确认为 `/opt/code-review`（活跃 git 仓库），修复已合入其源码并固化提交：
  `97e3426`（后端 runtime/service/api）、`97b14d2`（前端两组件重试按钮），分支 `deploy-security-monitor`。
- 生产后端 `agent-retry-prodsrc-08052145`、前端 `agent-retry-prodsrc-08052158`，5 容器 healthy，
  复现测试 7/7 通过，真实登录冒烟通过（run_ede09 已 completed）。
- **deploy-security-monitor 不上 GitHub**（用户决策）：分支历史含 175MB 数据库备份
  `deploy/.releases/responses-20260801-005011/backup/database.sql`（b5e7073 引入），GitHub 100MB 上限拒绝；
  修复已随远端 main（0d7a8c8）存在，生产基线保留在服务器本地 git。

## 工程侧待办

0. **后端镜像 tag 被并行构建取代（已核实无风险）**：部署约 4 分钟后后端被切换为 `bb-signal2-08051801`（已核实该镜像内含本次修复，md5 与 /opt/prism-current 补丁一致）。请确认该 tag 的构建来源（可能是你的构建自动化/并行操作）；若它基于 /opt/prism-current 构建，后续重建不会丢修复。
1. **生产源码与本地仓库对齐**：本次仅同步了 6 个受影响文件 + 服务器版 agent_responses_service.py 的单点补丁。服务器 frontend 的 `codeFile.ts`、`ProjectDetail.vue` 比本地新（quarantine 隔离上传），本地 main 缺少这些改动，建议补一次 `chore(prod-sync)` 提交把生产在运行但未提交的改动回收到仓库（如先前 ae712da 的做法）。
2. **后端全量镜像**：本次后端用叠加镜像（FROM docroot-fix + 3 文件覆盖）规避 pypi 直连过慢。建议后续在服务器配置 `PIP_INDEX_URL` 镜像（如清华/阿里 pypi）后执行一次干净的全量构建，使镜像与源码树完全一致。
3. **本地仓库基线**：本地 main 与 origin/main 已分叉（本地 1 个提交 vs 远端 7 个）；工作区还有 sandbox_service.py 等未提交改动，建议按 RELEASE_CHECKLIST 梳理干净提交后再走正式发布流程。
4. **503 重试与知识库 RAG 未部署**：本地 agent_responses_service.py 含上游 503 有限重试与操作知识库 RAG，生产版本未包含（本次为最小补丁）。生产 08-04 曾出现 503 failed 记录，建议下一轮发布一并带上。
5. **监控**：观察 24h 内 agent_response_run 是否再出现 `tool_choice` 400 类 failed；可在运维告警中增加该关键字。
