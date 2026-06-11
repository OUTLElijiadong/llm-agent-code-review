# 08 · SecuritySentinel 安全哨兵 Agent 设计

> 版本：v2.1 · 状态：已实现 · 起始日期：2026-05-28
> 编号衔接 v2.0 第 07 篇，作为 v2.0 之后的首个能力扩展。

## 一、立项动机

### 1.1 现状

棱镜 v2.0 当时已注册 12 个 Agent。当前主项目已经完成 v2.1 安全哨兵和 v3.0 自进化扩展,运行时注册中心为 14 个 Agent。其中"安全"相关能力分布在两处：

| 位置 | 形态 | 局限 |
|---|---|---|
| `app/ai/multi_agent.py` 中的 `SECURITY_AGENT` | **画像**(prompt 注入) | 只在 `review_type=security/full` 时通过 Prompt 段落临时启用，**不在 AgentRegistry**，不可被 Agent 办公室、聊天助手、API 直接调度 |
| `app/agents/review_agent.py` 中的 `code_reviewer` | 通用代码审查 Agent | 安全只是 8 个维度之一，覆盖广但深度浅，不输出 OWASP/CWE 编号、不做跨文件攻击面分析 |

实际答辩演示与对外宣传中，"基于大模型智能体的代码审查"如果**没有一个独立的、可见的、专门面向网络安全的 Agent**，就很难支撑"多 Agent 协同安全审查"这一核心叙事。

### 1.2 用户痛点

- 选 `security` 审查类型时，仍会被通用质量代理"稀释"，输出里夹杂大量风格类问题。
- 项目级别只能逐文件审查，**看不到跨文件的攻击面**（例如：A 文件接收外部输入 → B 文件未消毒 → C 文件落 SQL）。
- 敏感信息（密钥/密码/令牌）检测目前依赖大模型语义识别，**无确定性正则兜底**，容易漏报。
- 审查问题里没有 OWASP / CWE 标签，**修复优先级和合规对齐无依据**。

## 二、目标与非目标

### 2.1 目标 G

| 编号 | 目标 | 验收口径 |
|---|---|---|
| G1 | 新增 `security_sentinel` 独立 Agent，注册到 AgentRegistry | `/api/agents/runtime` 返回当前全部 14 个 Agent；Agent 办公室出现安全哨兵工位卡 |
| G2 | 支持三种调用形态：file / task / project | 三种入参均返回结构化 `SecurityFinding[]`，含 OWASP/CWE/严重度 |
| G3 | 确定性敏感信息扫描 | 不依赖 LLM 也能识别 ≥ 10 类常见硬编码秘钥（API key/JWT/密码/私钥等） |
| G4 | 项目级跨文件威胁建模 | 给出"输入入口 → 中间处理 → 危险接收点"的攻击路径概览 |
| G5 | 通过 ChatAgent 自然语言可达 | "帮我做项目安全审计" → CLARIFY 追问 scope/project_id → 调度 security_sentinel |
| G6 | 不新建数据库表 | 发现项复用 `review_issue` 表，扩展字段存 JSON 元数据 |
| G7 | 与 v2.0 SSE 事件总线打通 | EventBus 实时推送 DISPATCH→THINKING→PROGRESS→COMPLETE |

### 2.2 非目标（v2.1 不做）

- 不接入第三方安全数据库（如 Snyk、NVD、CVE 实时查询）—— 仅基于 LLM 知识库 + 本地正则规则
- 不做依赖供应链漏洞扫描（package.json / requirements.txt 比对 CVE）—— 列入 v2.2 路线图
- 不做运行时安全监控 / RASP —— 平台是静态审查工具，不在工作流内
- 不重写已有 `SECURITY_AGENT` 画像，二者并行共存

## 三、Agent 视觉与元数据

### 3.1 元数据档案

```python
class SecuritySentinelAgent(BaseAgent):
    name = "security_sentinel"
    description = "网络安全深度审查 Agent: OWASP Top10 / CWE / 敏感信息 / 项目级威胁建模"
    icon = "security_sentinel"
    color = "#D93B3B"           # 警戒红，与 ai_prompt 的 #E25C73 区分
    category = "security"       # 新增分类
    skills = (
        "OWASP Top10",
        "CWE 漏洞分类",
        "敏感信息扫描",
        "跨文件威胁建模",
        "合规检查",
        "POC 演示",
    )
```

### 3.2 视觉档案（补充至 `02-Agent图标与动画规范.md`）

| code | 中文名 | 几何图形 | 主色 | 隐喻 |
|---|---|---|---|---|
| `security_sentinel` | 安全哨兵 | 盾牌 + 居中锁孔 + 上方雷达扇形 | `#D93B3B` 警戒红 | 主动巡视的护盾 |

动画规范沿用 v2.0：`working` 状态时盾牌内核脉冲 + 雷达扇形旋转（1.8s）。

### 3.3 分类扩展

`category` 引入新值 `security`。注册中心 `summary()` 的 `by_category` 桶数从 7 变为 8。前端 `AgentOffice` 的 category 分组排序追加：`meta → frontline → reviewer → security → orchestrator → analytics → output → general`。

## 四、能力规格

### 4.1 三种调用形态

| 形态 | 入参 | 用途 | 默认 |
|---|---|---|---|
| `file` | `file_id`, `scan_depth?` | 单文件深度安全审查 | scan_depth=standard |
| `task` | `task_id` | 在已有审查任务上做安全复审；只对已检出但未标记 OWASP 的问题打标签 + 补充 POC | — |
| `project` | `project_id`, `top_n?`, `trace_dataflow?` | 项目级威胁建模 + 跨文件数据流追踪 | top_n=50, trace_dataflow=true |

### 4.2 输出 Schema

```json
{
  "findings": [
    {
      "title": "硬编码 OpenAI API Key",
      "category": "敏感信息泄露",
      "owasp": "A02:2021-Cryptographic Failures",
      "cwe": "CWE-798 (Use of Hard-coded Credentials)",
      "severity": "严重",
      "file_path": "backend/app/services/llm.py",
      "lines": "L23",
      "evidence": "OPENAI_KEY = \"sk-proj-...\"",
      "exploit_scenario": "代码若上传公开仓库,密钥可被任意人提取并消耗配额。",
      "fix_suggestion": "改从环境变量或 Vault 读取,加入 .gitignore,轮换该密钥。",
      "references": [
        "https://owasp.org/Top10/A02_2021-Cryptographic_Failures/",
        "https://cwe.mitre.org/data/definitions/798.html"
      ],
      "confidence": 0.95
    }
  ],
  "threat_model": {
    "entry_points": [
      {"file": "api/v1/auth.py", "function": "login", "risk": "外部 HTTP 输入"}
    ],
    "data_flows": [
      {
        "from": "api/v1/auth.py:login",
        "via": ["services/user_service.py:authenticate"],
        "to": "models/user.py:get_by_name",
        "risk_type": "SQL 注入",
        "severity": "高"
      }
    ],
    "attack_surface_summary": "外部输入入口 5 处，其中 2 处缺乏输入校验直达数据库层。"
  },
  "compliance": {
    "owasp_coverage": ["A01", "A02", "A03"],
    "gb_t_22239": "等保 2.0 三级 — 应用安全 a/b/c 条款命中风险 2 项"
  },
  "risk_score": 65,
  "summary": "本项目存在 3 处严重敏感信息泄露 + 1 处可达 SQL 注入路径,建议优先处理。"
}
```

### 4.3 严重度与 risk_score 计算

沿用 v1.0 `app/ai/scoring.py` 的扣分模型：

```
deduct = 15 * severe + 8 * high + 3 * medium + 1 * low
risk_score = 100 - deduct        # 截断到 [0, 100]
```

并叠加"跨文件可达性放大"：若 finding 出现在 `threat_model.data_flows` 路径上，severity 自动升 1 档。

## 五、实现架构

### 5.1 包结构（新增文件）

```
backend/app/
├── agents/
│   └── security_sentinel_agent.py        # 新增 — 主 Agent 类
├── api/v1/
│   └── security.py                       # 新增 — API 路由
├── schemas/
│   └── security.py                       # 新增 — Pydantic Schema
└── ai/
    └── security_patterns.py              # 新增 — 确定性敏感信息正则库
```

### 5.2 修改清单（既有文件）

| 文件 | 修改 |
|---|---|
| `backend/app/agents/__init__.py` | 导出 `SecuritySentinelAgent` |
| `backend/app/agents/orchestrator.py` | 注册 + inject_db + 便捷方法 `audit_security_for_*` |
| `backend/app/agents/chat_agent.py` | 新增 intent `security_audit` + Clarify 字段映射 + handler |
| `backend/app/api/__init__.py` | `include_router(security.router, prefix="/security")` |
| `backend/app/services/agent_service.py` | `ALL_AGENTS` 不动（保持向后兼容），`_agent_codes_from_model_name` 新增 `security_sentinel` 后缀识别 |
| `frontend/src/api/security.ts` | 新增 API 封装 |
| `frontend/src/components/agent/AgentAvatar.vue` | 注册新 SVG path |
| `frontend/src/views/security/SecurityScanModal.vue` | 新增 — 扫描弹窗 |
| `frontend/src/views/project/ProjectDetail.vue` | 顶部新增「🛡 安全审计」按钮 |
| `frontend/src/views/review/ReviewTaskDetail.vue` | 右上角新增「🛡 安全复审」按钮 |
| `frontend/src/constants/dim.ts` | 新增 `security_sentinel` 类别映射 |

### 5.3 SecuritySentinelAgent 实现骨架

```python
# backend/app/agents/security_sentinel_agent.py
from typing import Optional, List
from sqlalchemy.orm import Session

from app.agents.base import BaseAgent, AgentContext, AgentResult
from app.ai.code_chunker import chunk_code
from app.ai.security_patterns import scan_secrets
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_task import ReviewTask
from app.models.review_issue import ReviewIssue
from app.models.user import User


SYSTEM_PROMPT = """你是 PRISM 棱镜平台的网络安全审计 Agent，
具备 OWASP Top10、CWE、SANS Top25、等保 2.0 的完整知识。
工作目标:在用户提供的代码中识别**确定的、可解释、可演示**的网络安全漏洞。
约束:
1. 严格 JSON 输出,字段见用户消息中的 schema
2. 每条 finding 必须给出 owasp 和 cwe 编号
3. severity 仅取 严重 / 高 / 中 / 低
4. 不臆造漏洞;不确定时 confidence < 0.6 并明确标注
5. 不输出风格、命名、注释类问题(那是 code_reviewer 的领域)
"""


class SecuritySentinelAgent(BaseAgent):
    name = "security_sentinel"
    description = "网络安全深度审查: OWASP Top10 / CWE / 敏感信息 / 跨文件威胁建模"
    icon = "security_sentinel"
    color = "#D93B3B"
    category = "security"
    skills = ("OWASP Top10", "CWE 漏洞分类", "敏感信息扫描",
              "跨文件威胁建模", "合规检查", "POC 演示")

    def __init__(self):
        super().__init__(system_prompt=SYSTEM_PROMPT,
                         temperature=0.1, max_tokens=4096)
        self._db: Optional[Session] = None
        self._user: Optional[User] = None

    def inject(self, db: Session, user: Optional[User] = None) -> None:
        self._db = db
        self._user = user

    # ---- 三种调用形态 ----

    def scan_file(self, file_id: int, scan_depth: str = "standard",
                  ctx: Optional[AgentContext] = None) -> AgentResult:
        """单文件: 正则秘钥扫描 + LLM 深度漏洞审查"""
        ...

    def scan_task(self, task_id: int,
                  ctx: Optional[AgentContext] = None) -> AgentResult:
        """任务复审: 对已有 ReviewIssue 打 OWASP/CWE 标签 + 补 POC"""
        ...

    def scan_project(self, project_id: int, top_n: int = 50,
                     trace_dataflow: bool = True,
                     ctx: Optional[AgentContext] = None) -> AgentResult:
        """项目级威胁建模: 聚合所有文件入口 + 跨文件数据流"""
        ...

    # ---- 内部 ----

    def _llm_audit(self, code: str, language: str, file_path: str,
                   line_offset: int = 0,
                   ctx: Optional[AgentContext] = None) -> AgentResult:
        """单片代码 LLM 安全审查 - 返回 SecurityFinding[]"""
        ...

    def _persist_findings(self, findings: list, task_id: int) -> None:
        """复用 review_issue 表写回; OWASP/CWE 写入扩展字段"""
        ...
```

### 5.4 确定性敏感信息扫描（不依赖 LLM）

`app/ai/security_patterns.py` 维护正则规则，参考 [TruffleHog / Gitleaks] 公开规则集精简版：

```python
SECRET_PATTERNS = [
    {
        "name": "OpenAI API Key",
        "cwe": "CWE-798",
        "regex": r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}",
    },
    {
        "name": "AWS Access Key",
        "cwe": "CWE-798",
        "regex": r"AKIA[0-9A-Z]{16}",
    },
    {
        "name": "GitHub Personal Token",
        "cwe": "CWE-798",
        "regex": r"gh[pousr]_[A-Za-z0-9]{36,}",
    },
    {
        "name": "JWT Token",
        "cwe": "CWE-522",
        "regex": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    },
    {
        "name": "RSA Private Key",
        "cwe": "CWE-321",
        "regex": r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
    },
    {
        "name": "Hardcoded Password",
        "cwe": "CWE-259",
        "regex": r"(?i)(?:password|passwd|pwd)\s*[:=]\s*[\"'][^\"'\s]{6,}[\"']",
    },
    # ...共 10+ 类
]


def scan_secrets(content: str) -> list[dict]:
    """返回硬命中的敏感信息 finding"""
    ...
```

正则发现 → 直接产出 `severity="严重"` finding，不送 LLM；LLM 仅负责"语义"类的注入/越权/反序列化/SSRF 等漏洞。这样：
- **召回率有保底**（正则永远不会漏掉常见秘钥模式）
- **token 节省**（小文件可以不进 LLM 就有结论）

### 5.5 跨文件威胁建模（project 形态）

思路：
1. 先对项目所有文件并行调用 `_llm_audit`，得到每个文件的 entry_points 和 dangerous_sinks
2. 把 (entry_points, sinks) 列表送入第二轮 LLM 调用，让模型推断"哪些 entry 可以通过 import / 函数调用 / 路由链路抵达 sink"
3. 输出 `data_flows[]`，可视化为前端的攻击路径桑基图

第二轮 prompt 模板：

```
你已经分析过本项目所有文件。下面是各文件的入口与危险接收点清单:
{entries_summary}

请推断哪些入口数据流可以通过 import/函数调用抵达哪些接收点。
对每条可达路径输出:
- from: 入口位置
- via: 中间经过的函数/模块列表
- to:   抵达的危险点
- risk_type: 攻击类型(SQL 注入 / RCE / SSRF 等)
- severity: 严重度

只输出 JSON 数组,无可达路径时输出 []。
```

性能保护：单项目并行度 4，文件数 > 100 时降级为 top_n 个最危险文件做跨文件分析。

## 六、API 设计

### 6.1 路由

```
GET  /api/security/checklist                 OWASP / 等保检查清单(静态)
POST /api/security/scan-file                 { file_id, scan_depth? }
POST /api/security/scan-task                 { task_id }
POST /api/security/scan-project              { project_id, top_n?, trace_dataflow? }
GET  /api/security/findings?task_id=&project_id=  列出已写库的安全 finding
```

### 6.2 Schema（`app/schemas/security.py`）

```python
from typing import Optional, List
from pydantic import BaseModel, Field


class SecurityFindingOut(BaseModel):
    title: str
    category: str
    owasp: str = ""
    cwe: str = ""
    severity: str          # 严重/高/中/低
    file_path: str
    lines: str
    evidence: str
    exploit_scenario: str = ""
    fix_suggestion: str = ""
    references: List[str] = []
    confidence: float = 1.0


class DataFlowOut(BaseModel):
    from_loc: str = Field(..., alias="from")
    via: List[str] = []
    to: str
    risk_type: str
    severity: str


class ThreatModelOut(BaseModel):
    entry_points: List[dict] = []
    data_flows: List[DataFlowOut] = []
    attack_surface_summary: str = ""


class SecurityScanOut(BaseModel):
    findings: List[SecurityFindingOut]
    threat_model: Optional[ThreatModelOut] = None
    compliance: dict = {}
    risk_score: int = 100
    summary: str = ""


class SecurityScanFileIn(BaseModel):
    file_id: int
    scan_depth: str = Field("standard", description="quick / standard / deep")


class SecurityScanTaskIn(BaseModel):
    task_id: int


class SecurityScanProjectIn(BaseModel):
    project_id: int
    top_n: int = Field(50, ge=1, le=200)
    trace_dataflow: bool = True
```

### 6.3 鉴权

沿用 v1.0 模式：
- 普通用户只能扫描归属自己的项目/任务/文件
- admin 可扫描全平台
- 鉴权点放在 Agent 内部 `_authz_*` 方法，与 AiPromptAgent 保持一致

## 七、ChatAgent 集成

### 7.1 意图分类扩展

`_INTENT_SYSTEM` 字符串新增：

```
- security_audit: 用户要做网络安全审计/漏洞扫描/威胁建模/敏感信息扫描
```

判断规则示例：

```
如果用户说'安全扫描''漏洞扫描''安全审计''威胁建模''密钥泄漏''OWASP''渗透测试',
intent=security_audit
根据上下文推断 scope: 单文件→file, 一个任务→task, 一个项目→project
```

payload 模板：

```
- security_audit: {
    "scope": "file|task|project",
    "file_id": 数字, "task_id": 数字, "project_id": 数字,
    "scan_depth": "quick|standard|deep"
  }
```

### 7.2 Clarify 字段映射

```python
INTENT_REQUIRED_FIELDS["security_audit"] = []  # 动态决定
# _security_audit_required(payload) 按 scope 推导
```

scope=file → 必填 `file_id`；scope=task → 必填 `task_id`；scope=project → 必填 `project_id`；scope 缺失 → 先追问 scope。

### 7.3 Handler

```python
def _handle_security_audit(self, intent, ctx):
    p = intent.get("payload", {})
    scope = (p.get("scope") or "").lower()
    if scope == "file":
        result = self._orchestrator.audit_security_for_file(
            file_id=int(p["file_id"]), scan_depth=p.get("scan_depth", "standard"),
            ctx=ctx,
        )
    elif scope == "task":
        result = self._orchestrator.audit_security_for_task(
            task_id=int(p["task_id"]), ctx=ctx,
        )
    elif scope == "project":
        result = self._orchestrator.audit_security_for_project(
            project_id=int(p["project_id"]),
            top_n=int(p.get("top_n", 50)),
            trace_dataflow=bool(p.get("trace_dataflow", True)),
            ctx=ctx,
        )
    ...
    # 渲染 Markdown 摘要,展示 top 3 个严重 finding + risk_score
```

### 7.4 调用示例（自然语言）

```
用户: 帮我对项目 12 做一次安全审计
ChatAgent → intent=security_audit, scope=project, project_id=12
         → SecuritySentinelAgent.scan_project(12)
         → emit DISPATCH/THINKING/PROGRESS×N/COMPLETE
         → 渲染 Markdown:
            **🛡 安全审计完成** (risk_score=65)
            发现 3 处严重 / 5 处高危 / 12 处中危
            Top 3:
            🔴 [严重] OpenAI API Key 硬编码 — services/llm.py:23
            ...
            完整报告请到 ProjectDetail 查看。
```

## 八、前端集成

### 8.1 Agent 办公室

`AgentRegistry.list_runtime()` 自动新增第 13 项后，`AgentOffice.vue` 无需改动即可显示。新增的工作要点：

- `AgentAvatar.vue` SVG 字典添加 `security_sentinel` 几何符号（盾牌 + 锁孔 + 雷达扇形）
- `category` 分组新增 `security` 桶，文案 = "安全审查"

### 8.2 项目详情页 — 主入口

`views/project/ProjectDetail.vue` 顶部按钮组：

```
[导出 PDF] [导出 Word] [⚡ AI 修复手册] [🛡 安全审计]   ← 新增
```

点击后弹出 `SecurityScanModal.vue`：

```
┌─────────────────────────────────────────────────┐
│  🛡 项目安全审计                                │
├─────────────────────────────────────────────────┤
│  扫描范围: ○ 当前项目  ○ 仅近 7 天文件          │
│  深度:    ○ 快速  ◉ 标准  ○ 深度(含跨文件分析) │
│  Top N:   [_50_]                                │
│  □ 启用跨文件数据流追踪                          │
│                                                 │
│  预计耗时: ~ 45s(标准) / ~ 3min(深度)           │
│                                                 │
│             [取消]  [开始扫描]                  │
└─────────────────────────────────────────────────┘
```

扫描过程：通过 SSE 实时显示步骤气泡，与 v2.0 AgentChatDrawer 同款。

### 8.3 扫描结果页

新建 `views/security/SecurityReport.vue`：
- 顶部：risk_score 大数字 + 严重度饼图
- 中部：findings 列表（按 OWASP 分类折叠）
- 底部：threat_model 桑基图（用 ECharts sankey）
- 右上角：「⚡ 生成 AI 修复包」一键对接 AiPromptAgent

### 8.4 ReviewTaskDetail 复审入口

右上角按钮组：

```
[导出报告] [⚡ AI 修复包] [🛡 安全复审]   ← 新增
```

点击直接调用 `scan-task`，结果在弹窗里显示。

### 8.5 IssueDetailDrawer 增强

已写库的 `ReviewIssue` 若 `extra.owasp` 字段存在，渲染 OWASP 标签：

```
[严重] 安全漏洞  [OWASP A03:2021]  [CWE-89]
SQL 注入风险 ...
```

## 九、数据库映射（不新表）

复用 `review_issue` 表。SQLAlchemy 模型已有的 `extra_meta`（若无则按 v2.0 不新增列原则，编码进 `description` JSON 块）：

```python
# 写库时
issue = ReviewIssue(
    task_id=task_id, file_id=file_id,
    line_number=finding.lines_start,
    end_line=finding.lines_end,
    issue_type="安全漏洞",
    severity=finding.severity,
    title=finding.title,
    description=finding.exploit_scenario or finding.evidence,
    suggestion=finding.fix_suggestion,
    fixed_code="",
    # v2.1: 安全元数据放进可选 extra (若模型未加列则 JSON-stringify 嵌入 description 末尾)
    extra={
        "owasp": finding.owasp,
        "cwe": finding.cwe,
        "references": finding.references,
        "confidence": finding.confidence,
        "source_agent": "security_sentinel",
    },
)
```

> **决策**：若 `review_issue` 没有 `extra` JSON 字段，本期采用 `description` 末尾追加 `\n\n<!-- meta: {...} -->` HTML 注释块的兼容方案；下一期再考虑加列。该兼容方案对前端透明，仅 SecurityReport 视图解析。

## 十、事件总线集成

`BaseAgent.call()` 自带 `THINKING/COMPLETE/FAILED` emit。SecuritySentinelAgent 需额外发：

| 时机 | 事件 | payload |
|---|---|---|
| 进入 scan_project 时 | `DISPATCH` | `{scope:"project", project_id, file_count}` |
| 每完成一个文件 | `PROGRESS` | `{file_id, index, total, findings_count}` |
| 数据流分析开始 | `PROGRESS` | `{phase:"dataflow_analysis"}` |
| 完成 | `COMPLETE` | `{risk_score, findings_count, duration_ms}` |

前端 Agent 办公室的工位卡进入 `working` 状态（旋转光环 + 内核脉冲），完成后 6 秒回到 `idle`。

## 十一、Prompt 工程

### 11.1 单文件审查 Prompt（user 消息模板）

```
请对以下 {language} 代码做网络安全审查。

## 检查范围(若不适用则跳过,不要硬凑)
1. 注入类: SQL/Command/LDAP/XPath/模板注入
2. 跨站脚本 XSS / CSRF / SSRF / Open Redirect
3. 反序列化 / RCE / 文件上传 / 路径遍历
4. 越权: 水平/垂直越权,IDOR
5. 认证授权: 弱密码、会话固定、JWT 缺陷、OAuth 配置错误
6. 加密: 弱算法、ECB 模式、未校验证书、随机数不安全
7. 敏感信息: 硬编码秘钥、日志泄露 PII、错误堆栈泄露
8. 业务逻辑: 竞态条件、整数溢出、订单价格篡改

## 严格按此 JSON Schema 输出

{
  "findings": [
    {
      "title": "...",
      "category": "...",
      "owasp": "A03:2021-Injection",
      "cwe": "CWE-89",
      "severity": "严重|高|中|低",
      "line_start": 12,
      "line_end": 18,
      "evidence": "易导致漏洞的关键代码片段(可截取 1-3 行)",
      "exploit_scenario": "30-200 字的攻击场景描述",
      "fix_suggestion": "30-200 字的修复方案,给出参数化查询/输入校验等具体做法",
      "references": ["https://owasp.org/...", "https://cwe.mitre.org/..."],
      "confidence": 0.0-1.0
    }
  ],
  "entry_points": [
    {"name": "<函数名>", "line": 数字, "input_source": "HTTP body | query | header"}
  ],
  "dangerous_sinks": [
    {"name": "<函数名>", "line": 数字, "sink_type": "SQL | exec | open | requests"}
  ]
}

## 严格约束
- 不报告代码风格 / 命名 / 注释类问题
- 不臆造漏洞;不确定的标记 confidence < 0.6
- 输出纯 JSON,不要 markdown 围栏,不要解释

## 代码(行号偏移: {line_offset})
```{language}
{code}
```
```

### 11.2 跨文件威胁建模 Prompt（project 第二轮）

见 5.5 节。

## 十二、测试要点

### 12.1 后端单测（`backend/tests/unit/`）

| 测试文件 | 用例 |
|---|---|
| `test_security_sentinel_agent.py` | 单文件扫描返回 findings 结构正确;鉴权: 普通用户不能扫他人项目;LLM mock 失败时正则秘钥仍能命中 |
| `test_security_patterns.py` | 10 类秘钥正则各 1 个正例 + 1 个反例;不误命中 random base64 字符串 |
| `test_security_api.py` | `/api/security/scan-file` 返回 200,数据结构匹配 Schema;未登录返回 401;非本人项目返回 403 |
| `test_chat_security_intent.py` | "帮我做项目安全审计" → intent=security_audit;缺 project_id → CLARIFY |

### 12.2 前端验证

- vue-tsc 零错误
- AgentOffice 显示当前全部 14 张工位卡，security_sentinel 卡有红色 SVG + skills 标签
- SecurityScanModal 弹出、提交、结果渲染端到端走通
- IssueDetailDrawer 在 issue.extra.owasp 存在时显示 OWASP 标签

### 12.3 人工冒烟（10 步）

| # | 路径 | 操作 | 预期 |
|---|---|---|---|
| 1 | `/agents` | 打开办公室 | 14 张工位卡，含安全哨兵（红色盾牌） |
| 2 | 点 security_sentinel | 弹出详情抽屉 | 显示 skills 6 项 |
| 3 | Agent 助手 | "帮我做项目 12 的安全审计" | 触发 security_audit intent |
| 4 | Agent 助手 | "帮我做安全扫描" | CLARIFY 追问 scope |
| 5 | ProjectDetail 顶部 | 点 🛡 安全审计 | 弹出 SecurityScanModal |
| 6 | 提交扫描 | 等待结果 | SSE 实时显示进度，5-60s 后返回 |
| 7 | 结果页 | 看 risk_score 与 findings | 数字一致，OWASP 标签可点 |
| 8 | 桑基图 | 点击某条 data_flow | 跳转到对应文件 highlight 行 |
| 9 | findings 列表 | 点 "⚡ 生成修复包" | 复用 AiPromptAgent |
| 10 | ReviewTaskDetail | 点 🛡 安全复审 | 在原任务上补充 OWASP 标签 |

## 十三、性能与成本

| 形态 | LLM 调用次数 | 预计耗时 | DeepSeek 成本 |
|---|---|---|---|
| `file` 标准 | 1 (单文件 ≤ 6KB) ~ N (分片) | 3-15s | ¥0.005 - ¥0.02 |
| `file` 深度 | 上述 × 2 (二次精审) | 6-30s | ¥0.01 - ¥0.04 |
| `task` 复审 | 不调用 LLM,只查表打标 | < 1s | ¥0 |
| `project` 标准 | N × 文件 + 1 (数据流) | 30s - 3min | ¥0.05 - ¥0.30 |

性能保护：
- 单文件 > 50 KB 直接降级到正则扫描 + 函数级抽样审查
- project 文件数 > 100 时只对入口文件（api/、controllers/、views/）和危险文件（含 eval/exec/sql/raw query 关键字的文件）做 LLM 审查
- 全部 LLM 调用纳入 `ai_call_log`，可在管理员页面追溯

## 十四、安全与隐私

- 项目代码不发送到任何第三方安全数据库（与 v2.0 一致，仅 DeepSeek）
- 扫描结果中的 `evidence` 字段对敏感信息**二次脱敏**：API key / 私钥只保留前 4 + 后 4 字符
- 普通用户不可见他人项目的 findings
- 管理员审计日志记录每次 `scan-project` 调用，进入 `audit_log` 表

## 十五、与现有 `SECURITY_AGENT` 画像的关系

| 维度 | `SECURITY_AGENT`（画像，v1.0） | `security_sentinel`（Agent，v2.1） |
|---|---|---|
| 形态 | Prompt 段落注入 | 独立 BaseAgent 子类 |
| 触发 | 仅 `review_type=security/full` | 用户主动或 ChatAgent intent |
| 注册 | 不在 AgentRegistry | 在 AgentRegistry,出现在办公室 |
| 输入视角 | 单分片 | 文件/任务/项目三种 |
| 输出 | 普通 issue | finding + threat_model + risk_score |
| OWASP/CWE | 无 | 强制要求 |
| 跨文件分析 | 无 | project 形态自带 |
| 敏感信息正则 | 无 | 内置 10+ 类 |

**协同关系**：
- 用户走 ReviewService 流水线时，仍然走 `SECURITY_AGENT` 画像（保留 v1.0 行为，无回归）
- 想要深度安全审计时，主动调用 `security_sentinel`（新增能力）
- 二者结果都通过 `review_issue` 表归集，UI 上用 `extra.source_agent` 区分

## 十六、里程碑

| M | 内容 | 完成标准 |
|---|---|---|
| M1 | 后端 Agent + 正则 + LLM 单文件 | `scan-file` 端到端通；单测 4 项过 |
| M2 | 任务复审 + 项目级 | `scan-task` / `scan-project` 通；跨文件数据流可输出 |
| M3 | ChatAgent intent + Clarify | 自然语言触发 + 缺字段追问 |
| M4 | 前端 SecurityScanModal + SecurityReport | 弹窗 + 结果页 + AgentAvatar 新 SVG |
| M5 | 与 AiPromptAgent 联动 + 文档同步 | 「⚡ 生成修复包」按钮接通；README/说明文档 Agent 数 12→13 |
| M6 | 性能调优 + 回归 | 大项目 100+ 文件可扫；v2.0 全部回归用例继续通过 |

## 十七、回滚预案

v2.1 全部新代码集中在：
- 后端：`app/agents/security_sentinel_agent.py`、`app/api/v1/security.py`、`app/schemas/security.py`、`app/ai/security_patterns.py`
- 前端：`views/security/*`、`api/security.ts`

回滚步骤：
1. 后端 `api/__init__.py` 注释 `security.router` 注册
2. 后端 `agents/orchestrator.py` 移除 `SecuritySentinelAgent` 注册
3. 前端 ProjectDetail / ReviewTaskDetail 移除安全按钮

无数据库迁移、无破坏性回滚成本。`review_issue` 表写入的 `extra.source_agent` 字段在前端未识别时不影响渲染。

## 十八、与项目其他文档的对接

| 既有文档 | 同步项 |
|---|---|
| `README.md` | 工程目录树 Agent 数 12 → 13；技术栈描述补"网络安全审计 Agent" |
| `说明文档.md` | 进度记录新增 v2.1 条目；Agent 数字同步 |
| `docs/03-系统设计文档.md` | 多 Agent 章节追加 SecuritySentinelAgent 角色卡 |
| `docs/05-API接口文档.md` | 新增 `/api/security/*` 5 条端点 |
| `docs/v2.0/00-v2.0总体规划.md` | 文档清单追加本文件 |
| `docs/v2.0/02-Agent图标与动画规范.md` | 视觉档案表追加 security_sentinel 行 |
| `docs/08-AI审查模块文档.md` | 第 5 节 multi_agent 表追加注释"独立 Agent 见 v2.1 设计" |

以上同步项随 M5 一并提交。
