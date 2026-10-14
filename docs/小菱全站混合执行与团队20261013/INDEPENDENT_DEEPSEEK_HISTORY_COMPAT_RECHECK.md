# 历史推理兼容策略独立复核

## 当前结论

root已修复Gateway顺序；三条原红例复测转绿，原44项全部通过。另增40个实际HTTP/SSE组合，最终84项在三个独立进程均通过。以下保留发现与修复前红例，不代表当前候选仍失败。

本轮未修改root源文件，未登录或操作生产/GUI，也未调用真实模型。主任务已接收准确代码位置与最小修复建议。

## 已通过范围

独立矩阵覆盖新会话、并行工具（逆序返回输出）、多个有完整推理的轮次、后续缺推理轮次、带/不带推理的无工具助手回答、system纠错边界、encrypted不能替代原推理；每种覆盖省略/auto/required/none/指定function共40组合。

判断发生于官方精确host；两个外部host（包括api.deepseek.com.other.example）保留原payload。所有输入与工具列表完整不改，required/none/指定工具保持，未向历史添加假的思维。collector重建原推理后传给compat的一例也通过（合计41）。

## 三个独立红例

1. Gateway `previous_response_id` 从MemoryTranscriptStore恢复一份无reasoning的真实合成旧助手历史。兼容检查先看到新user，保持high；恢复历史后未再次检查，最终请求仍high。MockTransport按缺推理协议返回400。
2. 完整reasoning+工具调用对先经过compat保留high，真实`_project_input`/`compact_transcript`投影省略reasoning却保留调用对，最后仍high，MockTransport返回400。
3. 同样投影保留无工具助手回答却省略其reasoning，最终仍high，MockTransport返回400。

第2/3项只替代语义摘要模型的输出；仍执行实际压缩选择、来源选择收敛、最终投影和Gateway HTTP构建。因此它定位的是适配顺序，不证明真实模型摘要的事实保真度。

三项都期待最终安全兼容请求为none且HTTP200，但修复前实际HTTP400/effort=high；合计3失败、41通过。原payload及存储历史未在检测中改写。

## 代码位置与建议

修复前`backend/app/services/deepseek_responses_service.py:570`先prepare_deepseek_responses_payload，`577-579`才恢复旧历史，`581-583`才投影/压缩；返回前未对最终input再适配。

建议：先deepcopy请求并恢复历史，完成原压缩投影，再对最终upstream_payload调用prepare_deepseek_responses_payload。原完整input_items继续用于账本，不把发送投影回写旧历史。不改工具约束或全局思考，不伪造reasoning。

Native通道的顺序不同：runtime先投影，NativeResponsesTransport._stream才按实际发送input做compat；本次未在该顺序复现同一漏判。

## 规范依据与证据边界

[Responses指南](https://api-docs.deepseek.com/guides/responses_api/)规定无状态input回放与reasoning.content语义；[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)要求带tools的思考请求完整回传历史推理，包含无工具轮次。

这些本地MockTransport响应不是新增生产失败，也不能认定此次生产400全由Gateway造成。实际用户小菱主链是Native，生产故障与修复验收由主任务另行记录。百万token、真实模型理解和生产GUI没有在这里验证。

## 文件

- `evidence/independent/test_deepseek_history_compat_order.py`：本轮独立44例，可直接用backend环境pytest运行。
- `evidence/independent/prism-independent-history-compat-order-red.log`：3失败41通过。
- `evidence/independent/prism-independent-latest-provider-compat.log`：原root55项通过。
- `evidence/independent/deepseek-history-compat-red-receipt.json`：文件bytes/SHA256与范围。

## 修后复测

root最终源确认为deepcopy→恢复previous transcript→真实_project_input→末端prepare_deepseek_responses_payload；完整账本仍使用未压缩input_items。原44项全部通过。

增加40个协议组合：恢复缺链/恢复完整链/真实压缩工具对/真实压缩助手回答×五种工具选择×buffered/SSE。每项检查最终effort、严格tool_choice、旧账本不改、新账本保留完整未压缩历史、原payload不改、只有一次实际mock上游请求。84项在三个独立Python进程各通过一次。未重做已经通过的runtime15×3；本次没有发现最终顺序的已确认阻断缺陷。

新增证据：prism-independent-history-compat-order-green.log、extra.log、extra-round-2.log、extra-round-3.log及deepseek-history-compat-green-receipt.json。主独立脚本扩为84项；红日志保留，不覆盖。原44项源版本的SHA及归档状态见red-receipt。

这些证据属于本地合成协议和HTTP封装，不替代新版本生产原业务复测。
