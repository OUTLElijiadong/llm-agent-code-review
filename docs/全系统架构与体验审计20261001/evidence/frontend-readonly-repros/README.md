# 前端只读组件复现证据

- 源码版本：`fab823f71cda410cfdc03006f08fbca55f4644a9`，2026-10-01。
- 证据范围：从该工作树直接加载真实 Vue 组件，在 Vitest / jsdom 中控制 API Promise 的完成顺序。API、模型、审批动作均为 mock，不对生产发请求，不调用付费模型。组件源码未修改。
- 三次独立运行：每轮 3 个测试文件、10 条断言全部通过。**这些是“现存错误行为被复现”的正向断言；通过不能理解成产品功能通过。** 测试名称及完整输出见 `repeat-1.log`、`repeat-2.log`、`repeat-3.log`。
- 复现命令（仓库根）：`./docs/全系统架构与体验审计20261001/evidence/frontend-readonly-repros/run.sh`。脚本在 `/tmp` 创建临时 Vitest 根并使用现有依赖；所有 API 已替换。脚本保留临时目录便于回查。

## 六类实际发现

| 编号 | 优先级 | 当前代码定位 | 触发与实际结果 | 实际范围与修复建议 |
|---|---|---|---|---|
| F01 | 中 | `frontend/src/components/ai/AgentChatDrawer.vue:978-1004`（提交点1002、1003）；同步来源960-968、1058-1063 | 同一会话团队查询A较慢，查询B先得到completed，A随后将界面覆盖为running；两个已有团队中一个详情请求短暂失败，刷新将该团队从实时及本地缓存列表删除。 | 已实测用户端小菱组件，两种状态失败；管理员相同函数598-621仅源码同形观察，未组件运行。按会话及刷新代际丢弃旧结果；成功团队逐id合并，失败团队保留上次快照并显式标陈旧。不能用简单“状态只前进”规则替代代际，因为合法重试会回到运行态。 |
| F02 | 中 | `frontend/src/components/ai/AgentTeamWindow.vue:230-243`、253-266 | 确认重试时集合为originally-failed，确认期间同一团队同步变成newly-failed，实际调用重试newly-failed。附加组件契约样本：确认团队1期间props变团队2，重试/取消实际传团队2。 | 已实测组件，API是mock。用户生产触发最实际的是“确认弹窗打开期间后台状态更新”；跨团队prop样本未在真实页面手动复现。弹窗之前冻结team_id及task_keys，确认之后核对账号/会话/组件生命周期与目标，变化时重新确认而非替换对象。 |
| F03 | 中 | `frontend/src/views/issue/IssueHub.vue:269-290`（结果提交281、282） | 选择项目A再选B，B先返回、A晚到；筛选器B但展示A结果。另读取返回403后，列表和总数仍保留原数据。 | 已实测问题追踪组件；没有证明后端越权，也没有跨账号模型调用。增加请求序号及参数快照，以新请求为准；对401/403/404等失去可见性响应清除快照/勾选，普通网络失败可保留陈旧数据并注明原筛选范围。 |
| F04 | 中 | `frontend/src/views/sandbox/SandboxWorkstation.vue:360-369`（提交368） | 先选A再选B，B副本先返回，A响应晚到将sourceRevisions改为A，但form.project_id仍为B。 | 已实测配置组件，后端`project_source_revision_service.py:85-87`验证副本属于当前项目，因此当前证据是错误候选与提交404，不是执行其他项目或越权。绑定项目与请求代际，并在切换/卸载时作废。 |
| F05 | 中 | `frontend/src/views/sandbox/SandboxWorkstation.vue:380-391` | initial加载Promise未完成即卸载；之后完成会继续执行onMounted余下语句，新建2500ms轮询；首次tick再次请求listSandboxes。 | 已实测组件生命周期（listSandboxes调用从1增到2），未观察生产流量。onBeforeUnmount发生时定时器尚不存在，故现有clearInterval无法清理未来创建的timer。设置disposed，每个await之后检查，获取数据提交和创建监听/定时器前均验证生命周期。 |
| F06 | 低 | `frontend/src/components/ai/AgentTeamWindow.vue:199-201`、367-372 | 含170字符前缀与尾部证据的消息，只显示前160字符加...，窗口没有完整payload展开入口，尾部证据不可见。 | 只影响这个团队详情窗口的展示，不证明模型输出或服务端保存被截断。聊天内AgentTeamTrace提供完整结构化详情，不能称全站记录丢失。复用已存在详情/分页组件，摘要旁提供展开/复制原文并保留键盘入口。 |

## 没有确认为漏洞的线索

- `AdminOverview.vue:303` 拼接HTML tooltip，追踪后发现来源IP经过`rate_limit.py:23-59`解析规范化、`geoip_service.py:38-59`拒绝非法/非公网IP；国家和城市来自本机GeoLite数据库。没有已证明的普通用户可控HTML输入路径，不能据此报告已确认XSS。可以将统一转义当防御纵深建议。
- 项目/报告详情中常量化route id不能单独定为切换对象不更新：`AppLayout.vue:46`、管理布局`AdminLayout.vue:155`以route.path作key，路径变化会重建页面。
- 主聊天已使用`useAgentChatScope`绑定账号和会话、HTTP层已屏蔽旧凭据迟到401。未据源码臆称聊天跨账号泄漏。
