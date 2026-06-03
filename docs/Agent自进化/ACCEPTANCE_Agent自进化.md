# ACCEPTANCE_Agent自进化

> 交付日期：2026-05-31 · 范围：P1(L0/L1) + P2(L2 核心闭环) · 状态：已实现并通过测试

## 1. 交付总览

把「审查 → 用户处理 issue → 反馈聚合 → 经验沉淀/规则蒸馏 → 评估闸门 → 人工审批 → 启用后自动流回审查」
这条自进化闭环落地。全程**可解释(规则/经验)、可评估(黄金集闸门)、可回滚(快照)、可审批(admin + audit_log)**。

## 2. 新增 / 改动文件

### 后端 · 新增
| 文件 | 作用 |
|---|---|
| `app/models/review_experience.py` | 经验记忆表(L1) |
| `app/models/evolution_proposal.py` | 进化提案表(L2) |
| `app/models/eval_case.py` | 黄金回归集表(闸门基准) |
| `app/services/feedback_service.py` | 反馈聚合(采纳率/假阳性率/双门槛计数) |
| `app/services/experience_service.py` | 经验沉淀 + 时间衰减 + 相似检索 |
| `app/services/eval_gate.py` | 评估闸门(before/after 复跑打分) |
| `app/services/evolution_service.py` | 提案全生命周期(运行/评估/审批/驳回/回滚) |
| `app/agents/evolution_agent.py` | 自进化代理(假阳性提案 + 规则蒸馏) |
| `app/schemas/evolution.py` | 提案/经验/黄金集 Schema |
| `app/api/v1/evolution.py` | 自进化 API(全部 admin 鉴权) |
| `seed_eval_cases.py` | 黄金集种子(6 条锚点用例) |

### 后端 · 改动(最小侵入)
| 文件 | 改动 |
|---|---|
| `app/ai/prompt_builder.py` | 新增 `_format_experience` + `build_prompt` 增 `experience_section` 注入点 |
| `app/ai/prompts/review.zh.md` | 模板加入 `{experience_section}` 占位 |
| `app/services/review_service.py` | 审查前检索经验并线程化注入;失败降级为不注入 |
| `app/agents/orchestrator.py` | 注册 `EvolutionAgent` 并注入 DB |
| `app/api/__init__.py` | 挂载 `/api/evolution` 路由 |
| `init_sqlite.py` | 导入 3 张新表(本地 `create_all`) |
| `deploy/mysql/init.sql` | 3 张新表 DDL(MySQL 部署) |

### 前端(P3 · 已交付)
| 文件 | 作用 |
|---|---|
| `src/views/admin/EvolutionCenter.vue` | 进化中心:反馈指标看板 + 提案审批台(运行/评估/审批/驳回/回滚)+ 经验库 + 黄金集 + 反馈明细 + 提案详情抽屉 |
| `src/api/evolution.ts` | 前端 API client,对齐 `/api/evolution/*` |
| `src/types/evolution.ts` | 类型定义 |
| `src/router/index.ts`(改) | 新增 `admin/evolution` 路由(`role: admin`) |
| `src/components/layout/AppSidebar.vue`(改) | 管理组新增「Agent 自进化」菜单(仅 admin 可见) |

## 3. 闭环出口验证

进化产出的规则就是普通 `review_rule` 行(`enabled=1, is_builtin=0, user_id=NULL` 全局),
由既有 `prompt_builder._format_rules()` 自动注入下一次审查——**审查链路无需改动,启用即生效**。

## 4. 防翻车落点(对应 CONSENSUS 红线)

| 红线 | 落点 |
|---|---|
| Goodhart:ignored 不自动删规则 | `generate_fp_proposals` 双门槛(样本≥20 且跨≥2 任务);内置规则只降级不禁用 |
| 回声室:进化产物必须过人工锚点 | `eval_gate.run_gate` 在 `eval_case` 上复跑;无黄金集直接判不通过 |
| 闸门按提案类型分级 | 召回从严(安全关键);噪声仅对「会改变检出」的提案(new_rule/disable_rule/narrow_language)把关并留容差,`adjust_severity` 等纯标签改动只看召回,避免被 LLM 逐次审查的非确定性波动误伤 |
| 漂移:旧经验淘汰 | `experience_service.decay_weight` 时间衰减 + 聚合滑动窗口 |
| 信任:可解释/可回滚/可审批 | 产物均为可读规则;`applied_snapshot` 一键回滚;admin 审批 + `audit_log` |
| 闸门即产品 | `approve_proposal(require_eval=True)`:未 `eval_passed` 不得生效 |

## 5. 验证结果

- **单元测试**:`pytest` 共 **106 项全绿**(73 原有 + 33 自进化新增),无回归。
  - 反馈聚合 4、经验记忆 7、评估闸门 9、生命周期 5、EvolutionAgent 8。
  - 覆盖:采纳率/假阳性率、双门槛拦截、时间衰减、harvest 幂等、闸门召回/噪声判定与分级把关、
    审批生效、回滚还原、未过闸门拒绝审批等。
- **建表**:全新库跑 `init_sqlite.py` 建 13 张表;部署 MySQL 经 `deploy/mysql/init.sql`,
  存量库用 `Base.metadata.create_all` 补建 3 张新表已验证(MySQL 8 实测 14 张表)。
- **集成**:`app.main` 正常加载,`/api/evolution/*` 10 条路由注册;`AgentRegistry` 含 `evolution`(共 14 个 Agent)。
- **前端**:5 个改动模块经 Vite 按需编译全部 200;`vue-tsc` 全量类型检查零报错。
- **MySQL 实测闭环**(Docker `cr_mysql`):登录 admin → `/run` 从 11 条真实已修复问题沉淀 11 条经验;
  注入演示噪声触发 `adjust_severity` 提案 → `evaluate` 通过(召回 1.0→1.0)→ `approve` 规则严重度 中→低
  → `rollback` 还原中 → `audit_log` 全留痕;演示数据已清理,库恢复 11 经验 / 0 提案。

## 6. 如何运行

```bash
# 0. 起 Docker MySQL(本项目强制 MySQL,严禁 SQLite;.env 含 MYSQL_*)
docker compose --env-file .env -f deploy/docker-compose.yml up -d mysql
# 1. 存量库补建 3 张新表(全新库由 init.sql 自动建)
cd backend && .venv/bin/python -c "from app.core.database import Base, engine; import app.models.review_experience, app.models.evolution_proposal, app.models.eval_case; Base.metadata.create_all(bind=engine)"
# 2. 灌入黄金集锚点用例(走 MySQL)
.venv/bin/python seed_eval_cases.py
# 3. 跑测试(用内存 SQLite,不依赖 MySQL)
.venv/bin/python -m pytest -q --no-cov
# 4. 起后端 + 前端
.venv/bin/uvicorn app.main:app --port 8000        # 读 .env → MySQL:3307
cd ../frontend && npm run dev                       # http://localhost:5173 (代理 /api → :8000)
# 入口:管理员登录 → 侧边栏「管理 / Agent 自进化」
```

API(均需 admin token):
```
GET  /api/evolution/feedback            # 反馈信号总览
POST /api/evolution/run                 # 触发一轮进化(沉淀经验 + 生成提案)
GET  /api/evolution/proposals           # 提案列表
POST /api/evolution/proposals/{id}/evaluate   # 跑评估闸门
POST /api/evolution/proposals/{id}/approve    # 审批生效(需先过闸门)
POST /api/evolution/proposals/{id}/rollback   # 回滚
GET  /api/evolution/experiences         # 经验库
GET  /api/evolution/eval-cases          # 黄金集
```

## 7. 遗留 / 下一步(P3+)

- ✅ 前端「进化中心」`EvolutionCenter.vue` 已交付(提案审批台 + 指标看板)。
- L3 few-shot:把被采纳案例的 `fixed_code` 作为示例注入(经验库已存 `code_pattern`,可扩展)。
- 定时触发:接入现有定时方式周期性 `run`(当前为 admin 手动触发)。
- `narrow_language` 提案类型已建模,生成侧暂未产出(降级/禁用/新规则已覆盖主路径)。
- L4 工具合成 / L5 权重级:按 CONSENSUS 仍列为非目标。
