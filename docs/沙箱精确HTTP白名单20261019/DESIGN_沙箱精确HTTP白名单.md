# 沙箱精确 HTTP 白名单架构设计

## 系统分层

```mermaid
flowchart LR
  UI[沙箱工作台：编辑精确 GET 计划] --> API[后端 schema 与 service 校验]
  API --> DB[持久化白名单与执行快照]
  API -->|规范化计划 + 源码摘要| W[Sandbox Worker 协议校验]
  W -->|编码计划| R[可信 runner]
  R --> APP[容器 loopback 应用]
  R -->|仅白名单 GET 回执| API
  API -->|与持久计划逐项比对| E[结构化证据与报告]
  UI --> E
```

## 模块与契约

### 白名单条目

`{method: "GET", path: string, query: Record<string,string>, expected_statuses: number[]}`。仅允许固定 origin `http://127.0.0.1:<preview_port>`；路径必须是以单个 `/` 开始的精确安全路径，不允许 fragment、路径穿越段、通配符或控制字符。查询键值只允许未编码 ASCII 安全字符并受 2048 字节序列化上限约束。空 `expected_statuses` 表示只记录回包、不执行状态断言。

### 创建请求

本地 test 且模式为 `blackbox` 时 `http_scope` 必填，包含 1–20 项，并且必须包含唯一的 `{method: GET, path: /, query: {}}`。组合测试会运行项目测试脚本，不开放白名单模式。远程目标、白盒、部署请求不接受该字段，避免两种权限模型混用。

### Worker 执行协议

`http_scope` 是固定 execute schema 的字段之一，进入请求摘要。Worker 再验证字段、条数、方法、路径和根探活条目，之后生成 base64 编码的只读行协议传给容器。Runner 在任何目标代码启动前把编码计划移入权限为 0600 的临时文件并通过 exec 替换原进程，移除原始编码环境变量；计划被 runner 读入内存并删除后才启动应用，目标进程不继承计划内容或 runner 内部模式标记。调用方不能设置任意环境变量、命令或镜像参数。

### Runner 回执

唯一 `PRISM_BLACKBOX_DONE` 回执使用 `basis=exact_http_allowlist`，含每项的精确目标、HTTP 状态码、读取字节数、耗时和状态断言结果。Runner 不记录响应正文，不跟随重定向，不做路由发现，不运行 `_prism_poc.sh`、`_agent_tests/*` 或 `_prism_verify.sh`。回执计划在应用启动前读入 runner 内存并删除临时计划文件；白名单模式跳过归档内启动补丁及 Agent 脚本；Worker 日志发生截断时后端拒绝采信回执。

后端用保存的计划校验回执请求次序和目标，不匹配、重复、缺失、状态/字段越界时失败关闭。

## 数据流

```mermaid
sequenceDiagram
  participant U as 用户界面
  participant B as 后端
  participant W as Worker
  participant R as Runner
  participant A as 本地应用
  U->>B: 黑盒创建 + 精确白名单
  B->>B: schema/service 双校验并持久化
  B->>W: 不可变源码快照 + 规范计划
  W->>W: 协议二次校验并固定计划摘要
  W->>R: 固定环境变量中的编码计划
  R->>R: 安全临时文件接管并 exec，清除编码环境变量
  R->>A: 仅建立 TCP 连接确认服务已就绪
  loop 每个白名单条目
    R->>A: 只发送精确 GET path/query（包含计划中的 GET /）
  end
  R-->>W: 唯一结构化回执（无正文）
  W-->>B: 执行结果和日志
  B->>B: 逐项比对持久白名单与回执
  B-->>U: 证据表、断言、跳过项与覆盖缺口
```

## 异常策略

- 白名单无效或缺失：创建/执行前拒绝。
- runner 计划缺失、回执不唯一或与持久化计划不一致：失败关闭。
- 探活或任一请求无响应：测试失败；状态断言不匹配：失败；未配置断言：记录结果但明确未证明业务正确性。
- 响应读取最多保留计数字节（上限 64 KiB），不保存 body；重定向只记录原始响应，不跳转。
- AI 动态用例状态标记为跳过；整体报告至少注明动态覆盖不完整。
