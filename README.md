# 棱镜 Prism · 智能代码审查平台

> DeepSeek 大模型驱动的多 Agent 代码审查与治理平台

## 项目简介

棱镜 Prism 面向教学、内部研发和中小团队代码质量治理场景。用户可创建项目、上传或在线编辑代码，系统通过 DeepSeek 兼容大模型和多 Agent 编排，从代码质量、安全、可靠性、性能、可维护性等维度生成结构化问题、修复建议、审查报告和可追溯的 Agent 运行记录。

项目已从最初的审查主流程扩展到圆桌讨论、Agent Skill、自进化、Agent 治理、RBAC、论坛、个人知识库、工单、反馈、报告模板和上传安全扫描等模块。历史专项结果保存在 `docs/` 各 6A 目录；**当前事实以代码、运行时和最新对齐文档为准**。

## 核心特性

- 项目、成员、文件、版本三级代码管理，Monaco Editor 在线编辑
- DeepSeek 智能审查 + 14 个 Agent 编排，结构化输出问题、严重程度和修复建议
- 多 Agent 圆桌讨论审：WebSocket 实时发言、插话、暂停/恢复/终止和共识报告
- 问题闭环：未修复、已修复、已忽略、待复查
- 报告在线预览、模板管理、Word/PDF 导出
- 仪表盘：风险分布、问题类型、评分趋势、真实任务与 Agent 活动
- Agent Skill、自进化、黄金集评估、审批、回滚和治理审计
- RBAC、项目成员、数据范围、操作审计
- 论坛、个人知识库、用户画像、反馈和维修工单
- ClamAV + YARA 上传安全扫描与生产 fail-closed（本地真实容器验收已通过；生产发布后验收待完成）

## 当前工程基线

> 快照日期：2026-07-10。覆盖率专项的 87 文件可审计冻结快照已完成本地与服务器隔离复核：后端 1025 项测试通过、行覆盖率 77.87%；白名单冻结后并行新增的 4 项脚本测试也已复核，当前工作区自动发现共 1029 项测试、行覆盖率 77.98%。前端 8 个 Vitest 文件共 49 项测试通过，CI 质量门禁已落地。

| 项目 | 当前口径 |
| --- | --- |
| HTTP API | 194 条业务操作（`/api/*`） |
| WebSocket | 1 条（`/api/ws/discuss/{session_id}`） |
| Agent | 14 个（13 个专业 Agent + Orchestrator） |
| ORM | 51 张表声明；生产库另有迁移版本表和 2 张历史遗留表 |
| 后端模块 | 28 API / 30 Model / 28 Schema / 45 Service / 21 Agent 顶层 / 18 Skill |
| 前端模块 | 27 API / 25 组件 / 53 页面 / 18 类型模块 |
| 生产部署 | MySQL + Backend + Frontend/Nginx + ClamAV，共 4 个容器 |
| 当前网关 | Nginx + Let's Encrypt 证书挂载 |
| 后端质量 | 专项冻结快照 1025 passed / 77.871019%（11,785/15,134）；当前工作区含并行脚本测试为 1029 passed / 77.976741%（11,801/15,134）；`ruff check app tests ../scripts` 与 compileall 通过 |
| 前端质量 | 8 个 Vitest 文件 / 49 项测试；纳入 V8 的核心模块行覆盖率 99.79%、分支 93.39%、函数 100%；生产构建通过 |
| Backend 生产镜像 | 双阶段构建；Docker CLI 体积 494 MB，较优化前 964 MB 减少约 48.8%；最终镜像无编译工具链 |

完整取证、差异和优化顺序见 [`docs/项目工作方式与部署对齐/`](docs/项目工作方式与部署对齐/)。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue 3 + TypeScript + Vite + Element Plus + Pinia + Vue Router + ECharts + Monaco Editor + Axios |
| 后端 | Python 3.11 + FastAPI + SQLAlchemy 2.x + Pydantic v2 + Alembic + Uvicorn |
| 数据库 | MySQL 8.0 |
| AI | DeepSeek 兼容 API + 多 Agent / Skill / 治理编排 |
| 报告 | python-docx + ReportLab |
| 鉴权 | JWT + bcrypt + RBAC |
| 网关 | Nginx（SPA、REST、SSE、WebSocket、TLS） |
| 容器 | Docker Compose |
| 测试 | pytest + pytest-cov + Ruff；Vitest + V8 coverage + jsdom |

## 工程目录

```text
├── README.md
├── 说明文档.md                    # 项目全生命周期与进度主记录
├── backend/
│   ├── app/
│   │   ├── agents/               # 14 Agent、编排、Skill、治理
│   │   ├── ai/                   # LLM、Prompt、解析、分片、评分
│   │   ├── api/v1/               # 194 条业务 HTTP 对应的路由模块
│   │   ├── core/                 # 配置、安全、数据库、异常、限流
│   │   ├── exporters/            # Word/PDF 导出
│   │   ├── models/               # 51 张 ORM 表声明
│   │   ├── schemas/              # Pydantic 请求/响应模型
│   │   ├── services/             # 业务服务层
│   │   └── utils/
│   ├── alembic/                  # 001～009 数据库迁移
│   ├── tests/
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── requirements.lock         # Python 3.11 生产哈希锁
│   └── requirements-dev.lock     # Python 3.11 开发/CI 哈希锁
├── frontend/
│   ├── src/
│   │   ├── api/                  # 27 个 API 模块
│   │   ├── components/           # 25 个 Vue 组件
│   │   ├── views/                # 53 个 Vue 页面
│   │   ├── types/                # 18 个类型模块
│   │   ├── router/
│   │   └── stores/
│   ├── nginx.conf                # 当前生产 Nginx 配置
│   ├── package.json
│   └── Dockerfile
├── deploy/
│   ├── docker-compose.yml        # MySQL/Backend/Frontend/ClamAV
│   ├── deploy.sh / rollback.sh   # 精确 SHA 发布与应用回滚
│   ├── backup.sh / restore.sh    # 一致性备份、验证与恢复
│   ├── ops-check.sh / cleanup.sh # 机器巡检与受控清理
│   ├── systemd/                  # 备份、恢复验证和巡检 timers
│   ├── issue-cert.sh
│   ├── renew-cert.sh
│   ├── mysql/
│   │   ├── init.sql              # 22 张基础表；其余结构由 Alembic 补齐
│   │   └── seed.sql
│   └── README.md
└── docs/
    ├── 01-11 核心工程文档
    ├── 项目工作方式与部署对齐/    # 最新代码/文档/生产三方对齐
    └── 各功能专项 6A 文档
```

`frontend/Caddyfile` 是历史遗留配置，不是当前生产容器的配置来源。

## 本地快速开始

```bash
# 1. 配置本地环境
cp .env.example .env
# 编辑 .env；敏感值只放本地环境文件，不提交 Git

# 2. 启动 MySQL、后端热重载和前端 HMR
./dev.sh

# 3. 访问
# 前端：http://localhost:5173
# 后端 Swagger：http://localhost:8000/docs（本地 OPENAPI_ENABLED=true 时）
```

手动启动：

```bash
# 后端
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock
alembic upgrade head
uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm ci
npm run dev
```

## 生产部署

```bash
cd deploy
cp .env.example .env
# 填写数据库、JWT、模型端点等配置；禁止把 .env 提交 Git
release_sha="$(git -C .. rev-parse HEAD)"
./deploy.sh all --revision "$release_sha"
```

> **当前重要限制**：T12 本地生产级全量门禁已完成；T13 因工作区存在大量并行未提交改动、尚无承载本轮实现的精确审查 SHA 而阻塞。禁止直接部署当前工作区；必须先形成干净可审查的 commit、完成远端 CI，并按 [`deploy/RELEASE_CHECKLIST.md`](deploy/RELEASE_CHECKLIST.md) 执行生产备份、隔离恢复和小步发布。**本地门禁通过不等于生产发布授权。**

生产环境默认关闭 Swagger UI 与 OpenAPI JSON；Nginx 同时拒绝公网文档和 `/metrics`。

## 质量验证

```bash
# 后端
cd backend
.venv/bin/python -m pytest -o addopts='' -q
.venv/bin/python -m pytest -o addopts='' -q --cov=app --cov-report=term-missing --cov-fail-under=72
.venv/bin/ruff check app tests ../scripts
.venv/bin/python -m compileall app tests ../scripts
.venv/bin/python -m pip_audit -r requirements.lock

# 前端
cd frontend
npm run lint
npm run test:coverage
npm run build
npm audit --registry=https://registry.npmjs.org --audit-level=moderate

# 项目事实、OpenAPI 与部署静态门禁（仓库根目录）
cd ..
backend/.venv/bin/python scripts/generate_project_facts.py --check
backend/.venv/bin/python scripts/check_openapi_contract.py
./deploy/tests/test_scripts.sh
```

截至 2026-07-10，`.github/workflows/quality.yml` 已覆盖 Python 3.11 哈希锁、Ruff、全量 pytest、72% 覆盖率、compileall、pip-audit、MySQL 8 Alembic，Node 20.x 的 ESLint/覆盖率/构建/npm audit，以及项目事实、OpenAPI、Compose、Shell 和 systemd 门禁。仍需在 GitHub 首次实际运行并配置必需检查/分支保护。

## 文档阅读顺序

1. `说明文档.md`：项目规划和最新进度。
2. `docs/生产P0加固与工程治理/`：当前生产加固设计、任务、验收和最终门禁。
3. `docs/项目工作方式与部署对齐/FINAL_项目工作方式与部署对齐.md`：初始三方取证与差异来源。
4. `docs/01-11`：需求、设计、接口、开发、部署和测试细节。
5. 各专项 6A 目录：功能的历史决策与阶段验收。

## 许可

商业用途请遵循相关许可协议。
