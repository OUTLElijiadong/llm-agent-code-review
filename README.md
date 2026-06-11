# 棱镜 Prism · 智能代码审查平台

> DeepSeek 大模型驱动的多 Agent 代码审查平台

## 项目简介

棱镜 Prism 是一个面向开发团队的智能代码审查平台。用户创建项目、上传或在线编辑代码文件，代码内容直接存入数据库；系统将代码送入 DeepSeek V4 大模型，通过通用质量、安全、可靠性、性能、可维护性等多 Agent 角色，从 **代码规范、潜在 Bug、安全漏洞、性能问题、异常处理、命名规范、可维护性、注释完整性** 八个维度进行审查，产出结构化的问题清单、修复建议和审查报告。

与传统静态分析工具相比，棱镜能够 **理解代码语义**，并以自然语言给出问题描述和修复方案；与依赖第三方代码托管平台(GitHub/GitLab)的方案相比，棱镜采用 **数据库直存代码** 的方式，部署简单、演示稳定、适合中小团队内部使用。

## 核心特性

- 项目 / 文件 / 版本三级代码管理，Monaco Editor 在线编辑
- DeepSeek V4 智能审查 + 多 Agent 编排，问题、严重程度、修复代码结构化输出
- **多 Agent 圆桌讨论审**：聊天室式实时讨论，各 Agent 逐条发言且彼此可见，用户可随时插话，主持人汇总共识（WebSocket 实时推送）
- 审查问题闭环管理：未修复 / 已修复 / 已忽略 / 待复查
- 审查报告在线查看 + Word / PDF 导出
- 仪表盘可视化：风险分布、问题类型、评分趋势
- **Agent 自进化（v3.0）**：从审查反馈（采纳/忽略）聚合信号、沉淀经验并蒸馏规则，经黄金集评估闸门 + 管理员审批后自动流回审查，全程可解释、可审计、可一键回滚
- 角色权限：管理员 / 普通用户

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue 3 + Vite + Element Plus + Pinia + Vue Router + ECharts + Monaco Editor + Axios |
| 后端 | Python 3.9+ 本地开发 / Python 3.11 容器运行 + FastAPI + SQLAlchemy 2.x + Pydantic v2 + Alembic + Uvicorn |
| 数据库 | MySQL 8.0 |
| AI 模型 | DeepSeek V4 (deepseek-v4-flash) + 多 Agent Prompt 编排 |
| 报告导出 | python-docx (Word) + ReportLab (PDF) |
| 鉴权 | JWT (PyJWT) + passlib[bcrypt] |
| 容器化 | Docker + docker-compose |
| 接口文档 | Swagger / ReDoc (FastAPI 自带) |

## 工程目录

```
棱镜-Prism/
├── README.md
├── 说明文档.md                     # 项目全生命周期管理文档
├── .env                            # 环境变量配置
├── .env.example
├── .gitignore
├── docs/                           # 工程文档
│   ├── 01-对齐文档.md
│   ├── 02-需求规格说明书.md
│   ├── 03-系统设计文档.md
│   ├── 04-数据库设计文档.md
│   ├── 05-API接口文档.md
│   ├── 06-后端开发文档.md
│   ├── 07-前端开发文档.md
│   ├── 08-AI审查模块文档.md
│   ├── 09-部署运维文档.md
│   ├── 10-测试文档.md
│   ├── 11-开发计划.md
│   ├── v2.0/                      # v2.0 功能设计文档 (7份)
│   ├── 多Agent优化/                # 多Agent优化6A工作流文档
│   ├── 圆桌讨论/                   # v2.3 圆桌讨论审功能文档
│   └── Agent自进化/                # v3.0 Agent自进化设计 (6A工作流)
├── backend/                        # FastAPI 后端
│   ├── app/
│   │   ├── agents/                # Agent 注册中心与编排 (14个Agent)
│   │   ├── ai/                    # AI 智能体 (Prompt/解析/分片/评分)
│   │   ├── api/v1/                # RESTful API 路由 (87条 HTTP + 1条 WebSocket)
│   │   ├── core/                  # 配置、安全、数据库、异常、依赖
│   │   ├── exporters/             # Word/PDF 导出
│   │   ├── models/                # SQLAlchemy ORM (14张业务表 + base)
│   │   ├── schemas/               # Pydantic Schema (16个模块)
│   │   ├── services/              # 业务服务层 (17个服务)
│   │   ├── utils/                 # 工具函数
│   │   └── main.py
│   ├── alembic/                   # 数据库迁移
│   ├── tests/
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/                       # Vue3 前端
│   ├── src/
│   │   ├── api/                   # 接口封装 (17个模块)
│   │   ├── components/            # 通用组件 (23个)
│   │   ├── constants/             # 常量定义
│   │   ├── router/                # 路由 + 守卫
│   │   ├── stores/                # Pinia 状态管理
│   │   ├── types/                 # TypeScript 类型定义 (15个)
│   │   ├── utils/                 # 工具函数
│   │   ├── views/                 # 页面 (27个)
│   │   ├── assets/styles/         # 全局样式
│   │   ├── App.vue
│   │   └── main.ts
│   ├── .env.development
│   ├── package.json
│   ├── vite.config.ts
│   └── Dockerfile
├── deploy/                         # 部署相关
│   ├── docker-compose.yml
│   ├── deploy.sh
│   ├── README.md
│   ├── .env.example
│   └── mysql/
│       ├── init.sql               # 14张业务表 DDL
│       └── seed.sql               # 管理员 + 内置审查规则
└── frontend/Caddyfile             # 前端容器 Caddy/HTTPS 配置
```

## 快速开始

```bash
# 1. 进入项目目录
cd 棱镜-Prism

# 2. 配置本地开发环境变量
cp .env.example .env
# 编辑 .env, 填入 DEEPSEEK_API_KEY、DB_HOST=127.0.0.1、DB_PORT=3307 等

# 3. 本地开发一键启动（MySQL 容器 + 后端热重载 + 前端 HMR）
./dev.sh

# 4. 访问
# 前端: http://localhost:5173
# 后端 Swagger: http://localhost:8000/docs
```

生产 Docker Compose 部署:

```bash
cd deploy
cp .env.example .env
# 编辑 deploy/.env, 填入 MySQL 密码、DEEPSEEK_API_KEY、JWT_SECRET
./deploy.sh
```

本地开发模式:

```bash
# 后端
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd .. && docker compose --env-file .env -f deploy/docker-compose.yml up -d mysql
cd backend
uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm install
npm run dev
```

## 开发路线

1. **基础管理系统**：用户、项目、代码文件管理
2. **AI 审查核心**：DeepSeek 接入、问题解析、列表展示
3. **报告与统计**：报告生成、导出、仪表盘
4. **增强与打磨**：版本管理、规则配置、多 Agent 编排、测试

## 许可

商业用途请遵循相关许可协议。
