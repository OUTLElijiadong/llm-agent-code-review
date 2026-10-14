# 推理历史采集与回传专项复核

## 证据分类

本次修复是独立可复现的SSE回退协议缺口，不冒充生产论坛查询400的唯一根因。主任务另行确认生产存在真实非空推理，同时历史中也有不带推理的工具/助手轮次；历史兼容和受约束轮次之后的思考模式切换由主任务在provider适配器处理。

此报告中的事件流均是按官方事件结构构造的本地合成输入，不是生产推理正文，也没有真实模型调用。

## 原场景先复现

原collector仅处理output_item与工具参数/普通文本，不处理reasoning_text.delta/done和content_part。终止帧已有完整reasoning.content时原样保留，这一正向基线通过；但终止帧缺output或对应reasoning.content为空时，前面已收到的完整推理不能重建。

初始测试：10失败、3通过、1不适用（只靠终帧取文本却没有终帧output）。修复后移除不适用参数组合，并增补两例，最终15用例全部通过。

## 实际修复

- 仅修改runtime的`_response_from_events`，按item_id/output_index和content_index缓存原reasoning事件，不改原输入或原事件对象。
- delta分片用列表追加，避免逐次拼接重复复制长文本；done完整文本覆盖分片，后到重复delta不再次拼接。
- 完整终止帧原文仍优先；只补实际采集到但终帧缺失的推理，内容保留在内部reasoning.content，不生成summary/encrypted替代，也不将其变为output_text。
- output_item与content_part都能提供原文；多part、只有item_id的事件以及终帧漏reasoning项均可恢复。
- 不改工具选择、权限、压缩算法或失败/未完成执行门禁。无终止帧仍失败，半截工具调用不会执行。

## 验证

- 新增15项：五种文本来源/有无output的适用组合、完整终帧优先、多个content_index、item_id-only/终帧漏项、7000字符推理不裁剪/done去重、无工具完成轮次随后真实工具轮回放、半截流不执行。
- 新增15项在三个独立Python进程各通过一次（15×3）。
- 新增文件+原runtime测试+Responses integration，共182项通过；既有API不公开reasoning/原函数参数的过滤回归也通过。
- Ruff对修改runtime与新增测试通过。

## 压缩的另一路径：已复现并移交

只读辅助代理发现并由本代理重复两种合成投影：900字符reasoning分别跟随工具调用对、无工具助手回答。参数window=2000/output=300/threshold=500/keep_recent=350，摘要“历史已压缩”。两种都省略原reasoning节点index1，却保留相应工具对或助手回答；原账本不改，问题发生在发送投影。`estimate_tokens`是序列化UTF-8保守预算单位，不是实际provider token计量。这一证据已移交主任务，不能将本collector修复写成压缩整轮问题也已修复。

## 官方规范

Responses是无状态接口；需要每轮回放input，reasoning输入使用content，summary/encrypted不提供此兼容语义。官方同时列出reasoning_text.delta/done与content_part等流事件：[Responses指南](https://api-docs.deepseek.com/guides/responses_api/)。

带tools的思考请求要求完整回传历史推理，包括之前未发出工具调用的轮次：[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。这解释为什么仅保留可见助手消息或工具对不足；不得编造推理填补旧历史。

## 范围和待验收

本次15×3与182均为本地自动化，未生产写入/登录/GUI/真实模型，不证明本次论坛业务400已经生产闭环、百万token或模型语义压缩后完整理解。生产仍v4.0.101，新修复候选由主任务发布后重跑原场景及增样。

证据：`evidence/local/prism-reasoning-replay-*.log`、`prism-reasoning-compaction-shape.json`、`reasoning-replay-receipt.json`。
