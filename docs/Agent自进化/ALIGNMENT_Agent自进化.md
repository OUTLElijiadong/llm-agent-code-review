# ALIGNMENT_Agent自进化

> 版本：v3.0（提案）· 状态：设计中 · 起始日期：2026-05-31

## 1. 原始需求

让平台的审查 Agent 具备「自进化」能力：随着使用，审查越来越准、噪声越来越少，能自动沉淀本团队/本仓库的审查经验，而不是每次都从零开始、依赖人工不断改 Prompt。

## 2. 核心观点（设计地基）

> **自进化的地基不是模型，而是「闭环反馈信号」。没有 ground-truth 信号的自进化，只会放大模型自身偏见，然后漂移。**

代码审查恰好是少数**天生自带 ground truth** 的场景——一条审查意见最终被开发者**修复**还是**忽略**，就是最直接的标签。棱镜 Prism 已经在 `review_issue.status` 里采集了这个信号，只是还没把它聚合成「学习信号」反哺审查。本次要做的就是把这个反馈闭环**工程化**。

## 3. 项目上下文（现状盘点）

- 后端：FastAPI + SQLAlchemy 2.x + Pydantic v2，Python 3.9，SQLite（本地）/ MySQL 8（部署）。
- AI 审查链路：`ReviewService → CodeChunker → PromptBuilder → DeepSeekAgent → ResultParser → ReviewIssue`。
- 多 Agent：`backend/app/agents/` 下 13 个 `BaseAgent` + `registry` + `event_bus` + `orchestrator`，已有圆桌讨论与主动提问机制。

**自进化要挂载的基础设施大多已存在：**

| 自进化所需能力 | 现有载体 | 现状 |
|---|---|---|
| L0 遥测 | `ai_call_log`（prompt/response/token/耗时/状态） | ✅ 已有，缺「调用→issue 结果」的关联 |
| 反馈信号 | `review_issue.status`（`unfixed/fixed/ignored/pending_review`）+ `handled_by/handled_at` | ✅ 已采集，**未聚合成学习信号** |
| 可进化规则库 | `review_rule`（`rule_content` 即 Prompt 片段、`enabled`、`language`、`severity`、`user_id`、`is_builtin`） | ✅ 已有，目前只能人工增删 |
| 规则生效通道 | `prompt_builder._format_rules()` 把启用规则注入 Prompt | ✅ **启用即生效**，闭环出口现成 |
| 交叉验证器 | `security_static_rules` / `security_patterns`（静态命中）vs LLM 命中 | ✅ 可作第二意见 |
| Agent 范式 | `BaseAgent` + `AgentRegistry` | ✅ 可新增 `EvolutionAgent` |
| 人工闸门 | admin / 普通用户角色 + `audit_log` | ✅ 可做审批与可回滚审计 |

## 4. 已识别问题

| 类别 | 问题 | 决策方向 |
|---|---|---|
| 反馈未利用 | `fixed/ignored` 标签躺在表里，没人消费 | 新增聚合，算每条规则/每类问题的采纳率、假阳性率 |
| 规则静态 | `review_rule` 全靠人工维护，学不到本仓库习惯 | 新增 `EvolutionAgent` 从反馈蒸馏「候选规则」 |
| 噪声无抑制 | 高 `ignored` 率的规则会持续刷屏 | 进化时优先提案「降权/禁用/收窄语言」 |
| 无评估基准 | 改了规则/Prompt 无法判断是变好还是变差 | 建「黄金回归集」`eval_case`，进化产物必须先过闸门 |
| 调用与结果割裂 | `ai_call_log` 与最终 issue 结果不关联，无法追因 | 遥测补充 issue 维度关联 |

## 5. 边界确认（本期不做）

- **不做权重级微调 / RL（DPO/RFT）**：信任敏感场景优先「可读、可回滚」的进化（规则 + few-shot），权重级列为远期非目标。
- **不引入 Celery/Redis/向量数据库**：进化为离线/定时批处理，沿用现有同步技术栈；相似度先用轻量指纹/关键词，向量检索列为可选增强。
- **不切换 LLM Provider**，沿用 DeepSeek 调用链路。
- **不做全自动上线**：所有进化提案默认 `enabled=0`，**必须经 admin 人工闸门审批**才生效。

## 6. 疑问澄清

- 反馈信号的语义约定：`fixed` 视为真阳性，`ignored` 视为疑似假阳性（但需防「用户偷懒批量忽略」，故设最小样本量 + 跨任务/跨用户双重门槛，详见 DESIGN 第 6 节防翻车）。
- 进化触发：默认手动触发 + 可选定时（沿用现有同步任务，不引入调度中间件）。
- 可基于现有项目自动决策，无必须中断确认的问题。
