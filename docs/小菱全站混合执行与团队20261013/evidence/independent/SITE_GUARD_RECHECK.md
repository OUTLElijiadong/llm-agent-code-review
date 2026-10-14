# 站点完成判定独立复核

观察日期：2026-10-15。仅本地 SQLite 团队流程与服务判定，未操作生产。

## 方法与范围

独立读取 `_verified_site_submission_calls`、`_site_submission_is_verified`、父提交执行账本匹配、管理员逐调用账本、站点执行与核验 task receipt/readback。新增测试仅在本目录；没有编辑 `agent_responses_service.py` 或原实现者测试。

复用原 scenario 构造真实隔离 SQLite 用户/角色、小菱权限、当前会话和固定 execute/verify 团队；按真实任务租约领取、执行站点服务、持久化 AgentToolExecution/result_receipt、独立读取并 complete_task。业务 API 边界由本地确定性回执替代，未连接真实业务服务器或模型。每例首先验证完成态团队及合法基线，再变动一个边界。

没有重复原实现者全部场景或新增的19个畸形/冲突重复call样本。这里新增7个补充用例：一个新用户轮次、两个参数JSON类型、两个读回JSON类型、一个执行成员角色、一个不一致公开checkpoint。

## 首轮结果

`site-guard-recheck-red.txt`：**6 failed / 1 passed**，失败已立即交主代理。通过的是 execute 成员由 worker 改为 verifier 后正确拒绝；合法基线的真实站点流程每例调用两次本地 API，完成判定本身没有增加业务调用。

三类确认的本地判定不一致：

1. 管理员原 pin 调用的团队已完成并核验后，用户在同一 checkpoint 添加新只读轮次，读团队并回复“本轮只读查询已完成”。当前轮次 verified 集合正确为空，但管理员逐调用逻辑仍扫描整份运行 transcript，旧提交账本历史 pending 导致新只读轮次错误阻断。
2. 固定 execute 输入 page 从 JSON 整数1改为 true 或1.0，仅改任务输入而保留原账本；读回快照 page 或条目 id 从整数1改为 true，仅改核验 task result。上述四例仍允许完成，来源是 Python `==` 将 bool/int/float结构值等同，未保持精确JSON类型/快照身份。这是持久证据一致性边界，不等于已证明外部用户能任意改数据库。
3. 将当前公开提交 output 改为 completed=true/verification_status=completed，并把真实团队状态改为 queued，用户端返回 None。初始 pending guard 只看公开 output 的 pending 标记，因而跳过持久验证。仅证明“不一致公开 checkpoint”的 fail-closed 缺口；没有证明生产模型或用户可以注入平台工具输出，不能以此宣称生产漏洞。

## 复跑入口

```bash
cd backend
.venv/bin/python -m pytest -o addopts='' ../docs/小菱全站混合执行与团队20261013/evidence/independent/test_site_guard_independent.py -q
```

原日志保留，不用新的绿结果覆盖首轮红证据；最终修复后的独立结果见下文。

## 有限语言增样

主代理随后明确授权为否定失败线索补真实 scenario 红例。新增单独的 `test_site_guard_language_independent.py`，只使用三类对照句：成功“任务没有失败，操作已完成。”、失败“任务执行失败，未能完成业务操作。”、等待“已提交任务，尚未完成，等待业务核验。”。没有改原7例或覆盖原红日志。

该组覆盖 queued、client_observed、管理员真实完成、原父账本与公开失败输出一致、诚实等待，以及分类函数对照，共9个有限样本。运行时实现代理已处理此前否定失败声明与持久状态问题：**2 failed / 7 passed**。新的失败是明确失败句仍被识别为成功，以及因此错误阻断原父 ledger=failed 的管理员失败答复。日志为 `site-guard-language-red.txt`，已立即交主代理和实现代理继续最小修复。两次红灯属于不同修复阶段，不能合并计数或混称最终候选通过。

最终交回后已同时重跑原7例和语言9例，结果与原实现者的既有测试范围分别记录如下。

## 最终独立复测

实现代理明确停止源码编辑并交回后，本代理使用三个独立 pytest 进程复跑原7例与语言9例。每轮均为 **16 passed**，分别耗时 **2.80s、2.79s、2.80s**；共48次测试执行。日志分别是 `site-guard-green-run1.txt`、`site-guard-green-run2.txt`、`site-guard-green-run3.txt`。两份红日志未覆盖。

另外独立重新运行实现代理已有的两个 surface 合法 page=1.0 → API page=1 样本，以及同步回执的 valid/no_run/foreign_owner/other_surface/other_session 五个样本，合计 **7 passed / 78 deselected**；日志为 `site-guard-normalization-direct-green.txt`。这些7例为重跑既有用例，不能称为本代理新增用例。

最终只读核对确认：管理员逐调用检查使用当前用户轮次；完成声明使用持久原调用账本，公开 completed 标记不跳过该检查；原调用参数、固定 task 输入及账本/回执使用规范 JSON 比较，output/readback 对应快照也按规范 JSON 比较；API返回合法参数规范化仍保留原核验语义。非团队同步分支除当前调用账本，还查询匹配当前 run/user/surface/session 的 `AgentResponseRun`。原 worker/verifier 角色、持久执行回执和独立读回约束保留。

独立复测前后 `backend/app/services/agent_responses_service.py` 的 SHA256 均为：

`cc039e8e4d9989700817f81102040d8feed464e0fa03cf88c0235eab9ca4eb8c`

本次本代理新增16例三轮、既有样本7例单轮，共55次测试执行通过；实现代理汇报的304例与根代理的889例后端集成属于他们各自的验证，未计入本代理独立通过数。本地确认问题已在以上限定样本中修复，未发现该范围内新的失败；没有据此宣称生产、全站能力或OOM问题已解决。

## 未覆盖边界

未执行真实模型、生产数据库、生产账号或真实业务 HTTP；未测试并发写入/读回竞态、数据库事务隔离级别、授权撤销瞬间、全站全部能力、多写任务团队或跨请求流恢复。源代码已确认 owner/surface/session、原call ledger身份、execute/verify角色与固定输入、持久执行回执及对应读回门禁存在；本次仍不能把局部边界修复外推为全链路或全站已验收。
