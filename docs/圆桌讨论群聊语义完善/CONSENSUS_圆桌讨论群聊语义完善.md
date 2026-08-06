# 圆桌讨论群聊语义完善 - 共识文档

## 需求描述

圆桌讨论以一个共享会话承载五个代码审查子 Agent、用户和主持 Agent。每个审查 Agent 每轮读取完整历史，产生一次结构化自主决策：发言或静音；发言时同时声明立场和可选回应对象。主持 Agent 最后总结共识、分歧、用户关注点和修复优先级。

## 技术方案

- `full` 审查画像组合补入通用质量 Agent，圆桌固定以该组合启动。
- `DiscussionTurn` 增加向后兼容字段：`action`、`stance`、`reply_to`、`round_index`。
- `_speaker_turn` 请求模型输出 JSON 决策对象；解析器验证枚举并提供纯文本回退。
- `action=silent` 产生可回放的静音状态记录，前端以紧凑状态行显示，不作为有效问题发言。
- 用户发言同时写入共享 `all_turns`，保证后续 Agent、主持总结和报告抽取都能读取。
- 主持总结 Prompt 明确输出共识、分歧、用户关注点和建议，不把静音记录当成观点。
- 前端参会者条显示每个 Agent 最近状态，消息气泡显示立场标签和回应对象。

## 接口契约

`DiscussionTurn` 新增可选字段：

```json
{
  "action": "speak | silent",
  "stance": "propose | agree | oppose | question | supplement | neutral",
  "reply_to": "security | reliability | performance | maintainability | general | null",
  "round_index": 1
}
```

旧帧缺少以上字段时，前端按 `action=speak`、`stance=neutral` 处理。

## 验收标准

1. 圆桌预检返回五个代码审查子 Agent，且主持 Agent 单独显示。
2. 任一后发言 Agent 的 Prompt 含此前全部 Agent 决策和用户插话。
3. 模型能返回 `speak` 或 `silent`；静音不生成问题正文，但前端可见且重连可回放。
4. 发言的结构化立场和回应对象通过 WebSocket 到达前端并正确展示。
5. 主持总结明确处理共识与分歧，用户插话包含在总结上下文中。
6. 旧纯文本模型响应自动降级为普通发言，不中断讨论。
7. 后端定向测试、前端测试/类型检查/构建通过。
8. 服务器 Backend/Frontend 容器健康，HTTP/HTTPS 与 WebSocket 握手正常。

## 技术约束

- 不增加数据库迁移。
- 不改变现有鉴权、权限隔离和报告访问边界。
- 不在代码、文档或服务器文件中记录服务器密码或 API Key。
- 保留现有工作区和服务器上的无关未提交修改。

