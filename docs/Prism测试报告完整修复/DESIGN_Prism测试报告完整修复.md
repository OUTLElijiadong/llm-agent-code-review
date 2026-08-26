# Prism 测试报告完整修复：设计文档

## 总体架构

```mermaid
flowchart LR
    S[静态规则与 AST] --> C[统一候选]
    A[单/多 Agent] --> C
    R[圆桌共识] --> C
    C --> P[源码位置归一化]
    P --> M[N 路根因聚类]
    M --> V[CVSS 向量校验与计分]
    V --> D[(问题持久化)]
    D --> Q[归并后评分与风险]
    Q --> O[页面/HTML/PDF/Word]
```

## 结果质量设计

- 候选携带文件、CWE/类别、标题、证据、原始位置、精确位置、来源、置信度和 CVSS 值对象。
- 聚类主键由文件、规范类别、调用点/证据锚点组成；行号邻近只是辅助条件。
- 规范问题保存指纹、确认次数、来源列表和位置来源；先建普通索引，不建唯一约束。
- 精确位置优先级为 AST/规则调用点、完整源码唯一证据匹配、模型位置。
- CVSS 服务只接受完整 v3.1 向量并确定性计算分数；无向量即未评分。
- 评分模块在归并后运行，返回版本、各严重度数量、单位扣分、总扣分、下限截断和风险等级。

## 安全设计

- Nginx 对所有含自身 `add_header` 的 location 显式复用安全头；CSP 先兼容验证再强制。
- 反向代理覆盖外部转发头；后端只有在可信代理来源时采信真实客户端地址。
- SlowAPI 使用 Redis 共享存储，认证失败为 5 次/分钟，不设置永久账号锁。
- 上传管线区分“可审查源码”和“可执行内容”；二进制、恶意软件、WebShell 继续隔离或拒绝。

## 远程导入设计

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> downloading
    downloading --> validating
    validating --> scanning
    scanning --> importing
    importing --> succeeded
    queued --> cancelled
    downloading --> failed
    validating --> failed
    scanning --> failed
    importing --> failed
```

- 数据库任务是状态权威；字段包含归属、URL、项目名、幂等键、阶段、字节/文件进度、租约、心跳、重试次数、项目 ID 和脱敏错误。
- Worker 使用数据库租约领取任务，流式下载到受控临时文件并计算哈希；归档校验完成后才原子创建项目。
- 同一用户、URL 和项目名的并发提交返回已有活动任务；进程启动时回收过期租约并续跑。
- API 提交返回任务 ID，状态查询支持页面刷新恢复；旧同步调用保留兼容层。

## 前端设计

- HTML 预览在用户点击同步阶段预开窗口；失败则使用带 `sandbox` 的页内预览，Blob URL 在关闭时释放。
- 修复方案选择后等待 DOM 更新，再滚动详情 ref，设置 `scroll-margin-top` 和焦点；减少动画时即时滚动。
- 空邮箱 trim 后不发送；视图切换增加 `aria-pressed` 和足够触控尺寸；Radio 从 `label` 值迁移为 `value`。

## 异常与回滚

- 结果聚类保留原始来源和指纹，可通过管线版本切回旧逻辑。
- 异步任务失败必须保存可操作原因，不把异常任务标记成功。
- 新迁移只新增可空字段/表，旧镜像可读取；生产先备份数据库，再按提交哈希部署。
