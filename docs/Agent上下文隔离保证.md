# Agent 上下文隔离保证

> 适用范围:全平台所有并发 Agent/推演线,**无论什么任务什么情况下**。
> 更新:2026-08-29(渗透测试流水线落地时系统梳理)

## 隔离不变量

**一条并发执行线(推演线/子Agent/圆桌会话)能看到的输入只有三类:**

1. 自己的系统提示与任务假设;
2. 上游阶段的**结构化产物**(显式 JSON,非对话历史);
3. 自己执行工具产生的结果。

任何执行线**不得**看到:其他并发线的提示词、假设、中间结果或对话历史。

## 各子系统的隔离机制与证据

| 子系统 | 机制 | 关键代码 | 证明 |
| --- | --- | --- | --- |
| 渗透测试推演线 | `_build_line_context()` 唯一输入构造入口;线内无共享状态;sha256 输入指纹落库审计 | `backend/app/services/pentest_service.py` | `test_concurrent_line_prompts_do_not_cross_contaminate`(并发双线金丝雀断言) |
| 审查三阶段协同 | 每请求独立 orchestrator(`build_orchestrator` 每请求隔离) | `backend/app/agents/orchestrator.py` | 既有 review 测试 |
| 多Agent 团队(agent_team) | 任务依赖走 `_dependency_context()` 结构化注入;租约 `claim_next_task` CAS 领取;执行策略快照 | `backend/app/services/agent_team_service.py` | 既有 agent_team 测试 |
| 圆桌讨论 | `DiscussionSession` 按 session_id 隔离,owner 校验,续会必须携带真实上下文重建 | `backend/app/agents/discussion_bus.py` | 既有 discussion 测试 |
| 小菱会话 | `AgentResponseRun.surface`(user/admin)会话域隔离;SSE 流带 run_id/session_id | `backend/app/services/agent_responses_service.py` | 既有 responses 测试 |
| 沙箱执行 | 每环境独立 runsc 容器(network=none)、不可变源码快照 sha256、request_digest HMAC 防换包、stop 墓碑协议逐 request 回收 | `deploy/prism_sandbox_executor.py` | 沙箱链路既有约束(见部署运维文档) |
| 个人知识库 RAG | 检索按 `user_id` 过滤(连管理员也不放行);Agent KB 按 `agent_code` 隔离 | `knowledge_service.py` / `agent_knowledge_service.py` | 既有 knowledge 测试 |

## 新增并发执行线的检查单

1. 输入构造收敛到**一个函数**,显式列出全部输入来源;
2. 线工作单元不接收共享 DB 会话/全局可变对象;落库由单一编排线程完成;
3. LLM 调用无隐式跨线历史(每次独立请求);
4. 为输入生成指纹(sha256)落库,便于事后审计"这条线当时看到了什么";
5. 写一条并发金丝雀测试:两条线各带唯一标记,断言双方提示词/产物互不包含对方标记。
