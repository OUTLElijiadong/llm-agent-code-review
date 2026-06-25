# CONSENSUS · 代码审计 Agent 集成与漏洞识别增强

> 任务名: `代码审计Agent集成与漏洞识别增强`
> 创建时间: 2026-06-25
> 阶段: Align(对齐) → 共识产出
> 状态: 所有不确定性已解决,可作为 Architect 阶段输入

---

## 一、需求描述(锁定)

### 1.1 核心需求

1. **激活 Agent 集成**:让 `/api/review/start` 代码审查主流程真正调用 `CodeReviewerAgent`/`SecuritySentinelAgent` 等 BaseAgent,统一事件总线、调用日志、AiCallLog 归因,而非绕过 BaseAgent 直接调 `DeepSeekAgent.chat()`。
2. **双引擎漏洞识别**:在 review 主流程中接入 `SecuritySentinelAgent` 的 20 条静态规则 + 20 类正则秘钥扫描作为前置过滤,先确定性命中再 LLM 深度审查;所有 finding 强制带 CWE/OWASP/证据/攻击场景/修复建议等结构化字段。
3. **编辑器压缩包自动解压**:后端识别上传的压缩包(zip/tar/gz/rar)时自动解压并作为多个新文件入库,而非作为单个 binary 文件存 base64 内容;前端编辑器对剩余的真 binary 文件(图片/可执行文件等)显示友好提示而非 base64 字符串。
4. **端到端验证**:新建 6-8 个覆盖 SQL 注入/XSS/硬编码密钥/路径遍历/反序列化/SSRF/命令注入 的测试样本,本地启动服务真实跑 review 任务,验证 Agent 调用链路与漏洞识别效果。
5. **服务器同步**:本地验证通过后 rsync 同步到服务器 `81.70.251.90` 的 `/opt/code-review`,执行 `deploy/deploy.sh` 重建容器,保留数据库数据。

### 1.2 范围边界(明确不做)

- 不重构 `audit_service`(操作审计日志)
- 不重构圆桌讨论审功能(`/api/discuss/*`)
- 不重构 Agent 自进化/治理平台功能
- 不重构 `/api/security/scan*` 独立安全扫描接口(保留现有行为,但底层静态规则/正则库被 review 主流程复用)
- 不修改前端 Agent 办公室 UI

---

## 二、技术实现方案(锁定)

### 2.1 Agent 集成方案

**核心改动**: 重构 `review_service._execute_review()` 与 `_review_one_file()`,把 LLM 调用从 `DeepSeekAgent.chat()` 改为通过 `AgentRegistry` 获取对应 BaseAgent 并调用 `BaseAgent.call()`。

**调用链路**:
```
review_service.start(payload)
  └─ _run_review_task(task_id, user_id) [后台线程]
       └─ _execute_review(db, agent, task, user, files, rules, profiles, experience)
            └─ 对每个 file:
                 └─ _review_one_file(db, task, file, rules, user, profiles, experience)
                      ├─ [新增] 静态规则前置过滤(复用 SecuritySentinelAgent 静态规则 + 正则秘钥)
                      │    └─ 直接生成确定性 findings(无 LLM 调用)
                      ├─ [改造] LLM 深度审查(通过 BaseAgent.call())
                      │    ├─ quick/standard: CodeReviewerAgent.execute()
                      │    ├─ security: SecuritySentinelAgent.scan_file_style()
                      │    ├─ performance: CodeReviewerAgent.execute() with performance profile
                      │    └─ full: 多 Agent 协同(CodeReviewerAgent + SecuritySentinelAgent + 交叉复审)
                      └─ 合并去重 → 入库 ReviewIssue
```

**Agent 调用统一入口**:
- 新增 `CodeReviewerAgent.execute_review(code, rules, language, file_name, line_offset, experience_section, agent_section, api_config, ctx)`,内部通过 `self.call()` 调用 LLM(自动 emit 事件 + 重试),返回 `AgentResult` 含解析后的 `ReviewResult`。
- `SecuritySentinelAgent` 复用现有 `_llm_audit_chunk()`,但暴露一个 `scan_file_for_review(file, ctx, api_config)` 方法供 review_service 调用,返回与 `CodeReviewerAgent` 同结构的 `ReviewResult`。
- 所有 LLM 调用通过 `BaseAgent.call()` 走,`AiCallLog` 在 `BaseAgent.call()` 内统一写入(需扩展 BaseAgent 增加 `log_deferred` 钩子,或由调用方写入)。

**事件总线统一**:
- 移除 `_emit_review_event` 手动发事件的代码,改为依赖 `BaseAgent._emit()` 自动 emit `THINKING/COMPLETE/FAILED`。
- 保留 `DISPATCH` 事件(任务启动)和 `PROGRESS` 事件(文件进度)的手动 emit,因为这两个事件不属于单次 Agent 调用生命周期。

### 2.2 双引擎漏洞识别方案

**静态规则前置过滤**:
- 新增 `app/ai/static_analyzer.py` 模块,封装"对单个文件应用静态规则 + 正则秘钥扫描"的逻辑,返回标准 `Finding` 列表。
- 静态规则库复用 `app/ai/security_static_rules.py`(20 条)和 `app/ai/security_patterns.py`(20 类正则)。
- 静态分析结果直接作为 `ReviewIssue` 入库,issue_type 标记为"安全漏洞",severity 按规则定义,confidence=0.95+。

**LLM 深度审查**:
- 增强审查 Prompt,强制 LLM 输出以下结构化字段:
  ```json
  {
    "issues": [{
      "line_number": 12,
      "end_line": 18,
      "issue_type": "安全漏洞",
      "severity": "严重",
      "title": "SQL 注入",
      "description": "...",
      "suggestion": "...",
      "fixed_code": "...",
      "owasp": "A03:2021-Injection",
      "cwe": "CWE-89",
      "evidence": "cursor.execute(\"SELECT * FROM user WHERE name='\" + name + \"'\")",
      "exploit_scenario": "攻击者通过 name 参数注入 ' OR 1=1 -- 绕过认证",
      "references": ["https://owasp.org/..."],
      "confidence": 0.9
    }]
  }
  ```
- 结果解析器 `result_parser.Issue` 新增 `owasp/cwe/evidence/exploit_scenario/references/confidence` 字段。
- `ReviewIssue` 模型新增对应字段(需 Alembic 迁移)。

**双引擎合并策略**:
- 静态规则命中的 finding 优先入库(severity 不调整)。
- LLM 发现的 finding 与静态规则去重(按 `file_id + line_number + cwe` 指纹去重),若 LLM 与静态规则同时命中同一位置,以 LLM 的描述为准但保留静态规则的 confidence=0.99。
- LLM 发现的 finding 若没有 cwe 字段,后端尝试用 `SecuritySentinelAgent._infer_owasp_cwe()` 推断补全。

### 2.3 编辑器压缩包自动解压方案

**后端改动**:
- 新增 `app/utils/archive_extractor.py` 模块,支持 zip/tar/tar.gz/tgz/tar.bz2/tar.xz/rar(可选) 解压。
- 严格安全校验:
  - 拒绝 `..` 路径(zip slip 防护)
  - 拒绝绝对路径
  - 限制解压后文件数量 ≤ 100
  - 限制解压后总大小 ≤ 50MB
  - 限制单个文件大小 ≤ 10MB
  - 跳过隐藏文件(`.git/`、`.svn/` 等)
- `code_file_service.upload()` 检测压缩包扩展名时:
  - 解压后逐个文件入库(递归调用 `_create_file`)
  - 返回值改为 `(file_ids: list[int], languages: list[str], version_nos: list[int])`
  - 上传响应 Schema 新增 `is_archive: bool` 和 `extracted_files: list[FileSummary]`
- 非压缩包 binary 文件(图片/可执行文件等)仍按原逻辑存 base64,但 `get_file()` 返回时标记 `is_binary: true` 且 `content` 返回空字符串。

**前端改动**:
- `CodeEditor.vue` 检测 `fileDetail.is_binary` 时:
  - 不渲染 Monaco Editor
  - 显示提示"该文件为二进制文件,无法在编辑器中查看"
  - 提供"下载原文件"按钮(调用新增的 `GET /api/code-files/{id}/download` 接口)
- `CodeFileList.vue` 对 binary 文件:
  - 文件名后加 `[二进制]` 标签
  - 点击仍可进入详情页,但显示提示而非 base64

### 2.4 数据库迁移

新增 Alembic 迁移 `003_review_issue_vuln_metadata.py`:
- `review_issue` 表新增字段:
  - `owasp VARCHAR(32) NULL` — OWASP 编号
  - `cwe VARCHAR(32) NULL` — CWE 编号
  - `evidence TEXT NULL` — 漏洞证据代码片段
  - `exploit_scenario TEXT NULL` — 攻击场景说明
  - `references_json JSON NULL` — 参考链接列表
  - `confidence FLOAT NULL` — 置信度 0.0-1.0
  - `source VARCHAR(16) NULL DEFAULT 'llm'` — 来源(static/llm/regex)
- `code_file` 表新增字段:
  - `is_binary TINYINT(1) NOT NULL DEFAULT 0` — 是否二进制文件
  - `original_blob LONGBLOB NULL` — 二进制原始字节(仅当 is_binary=1 时使用)

### 2.5 测试样本设计

新建 `backend/tests/fixtures/vuln_samples/` 目录,包含 7 个测试文件:
1. `sqli_python.py` — SQL 字符串拼接注入
2. `xss_javascript.js` — DOM XSS 内联拼接
3. `hardcoded_secrets.py` — 硬编码 AWS Key + DB 密码
4. `path_traversal_python.py` — 用户输入拼接到文件路径
5. `deserialization_python.py` — pickle.loads 用户输入
6. `ssrf_python.py` — requests.get(user_url) 无校验
7. `command_injection_python.py` — os.system(user_input)

每个样本包含明确的漏洞代码 + 注释标注预期发现位置和 CWE,作为 review 任务的输入。

### 2.6 服务器同步方案

1. 本地全部验证通过后,执行:
   ```bash
   rsync -avz --exclude='.git' --exclude='node_modules' --exclude='.venv' \
     --exclude='__pycache__' --exclude='*.pyc' \
     /Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/ \
     root@81.70.251.90:/opt/code-review/
   ```
2. SSH 到服务器执行:
   ```bash
   cd /opt/code-review/deploy
   ./deploy.sh
   ```
3. 验证线上服务:
   - `https://lijiadong.cn/healthz` 返回 200
   - `https://lijiadong.cn/docs` 可访问
   - 线上跑一次 review 任务验证 Agent 调用链路

---

## 三、验收标准(锁定)

### 3.1 功能验收

| 编号 | 验收项 | 验证方法 |
|------|-------|---------|
| AC1 | `/api/review/start` 启动审查任务时,后端日志和 EventBus 事件显示 `code_reviewer`/`security_sentinel` Agent 被真实调用 | 本地启动服务,启动一个 review 任务,观察后端日志和 SSE 事件流 |
| AC2 | `AiCallLog` 表的 `agent_label` 字段值为真实 Agent name(`code_reviewer`/`security_sentinel`),而非画像 code(`general`/`security`) | 启动 review 任务后查 `ai_call_log` 表 |
| AC3 | 对 SQL 注入样本执行 review,能识别出 SQL 注入漏洞,CWE 标注为 CWE-89,OWASP 标注为 A03:2021-Injection | 用 `sqli_python.py` 样本启动 review 任务,查看 ReviewIssue |
| AC4 | 对硬编码密钥样本执行 review,静态规则前置命中,issue_type=安全漏洞,confidence≥0.95,source=regex | 用 `hardcoded_secrets.py` 样本启动 review 任务 |
| AC5 | 对 XSS/路径遍历/反序列化/SSRF/命令注入样本,review 能识别对应漏洞并给出修复建议 | 逐一用样本启动 review 任务 |
| AC6 | 上传 zip 压缩包,后端自动解压并创建多个 CodeFile 记录,响应包含 `is_archive=true` 和 `extracted_files` 列表 | 通过 `/api/code-files/upload` 上传测试 zip |
| AC7 | 上传图片文件(如 png),`CodeFile.is_binary=1`,前端编辑器显示"二进制文件无法查看"提示而非 base64 字符串 | 上传 png,前端打开编辑器 |
| AC8 | `ReviewIssue` 表新增字段(owasp/cwe/evidence/exploit_scenario/references_json/confidence/source)存在且数据正确写入 | Alembic 迁移后查表结构 + review 任务后查数据 |
| AC9 | 服务器 `81.70.251.90` 上同样能跑通 AC1-AC8 | 部署后线上验证 |

### 3.2 非功能验收

| 编号 | 验收项 | 验证方法 |
|------|-------|---------|
| NFR1 | 后端 `pytest` 全量通过,新增测试 ≥ 10 个,无回归 | `cd backend && ./.venv/bin/python -m pytest -q` |
| NFR2 | `ruff check app tests` 通过 | ruff 检查 |
| NFR3 | `compileall app tests` 通过 | compileall 检查 |
| NFR4 | 前端 `npm run build` 通过,vue-tsc 零错误 | 前端构建 |
| NFR5 | Alembic 迁移在本地和服务器均能成功执行 | `alembic upgrade head` |
| NFR6 | 所有新增函数有函数级注释(功能/参数/返回值) | 代码审查 |
| NFR7 | zip slip 防护:上传恶意 zip(含 `../evil.py`)被拒绝 | 上传恶意 zip 验证 |
| NFR8 | 解压限制:超过 100 个文件或 50MB 的压缩包被拒绝 | 上传大压缩包验证 |

---

## 四、技术约束

- Python 3.9 兼容(`Optional[X]` 而非 `X | None`)
- MySQL 8.0,严禁 SQLite
- 复用现有 `BaseAgent.call()` / `AgentEventBus` / `AgentRegistry` / `DeepSeekAgent.log_deferred`
- 复用现有 `security_static_rules.py` / `security_patterns.py`,不重复实现
- 复用现有 `code_chunker.py` 分片逻辑
- API Key 通过 `.env` 管理,不提交 git
- 服务器部署使用现有 `deploy/deploy.sh`,不修改部署脚本(除非必要)

---

## 五、任务边界限制

- 本次任务仅修改 `/api/review/start` 主流程和 `/api/code-files/upload` / `/api/code-files/{id}` 相关接口
- 不修改 `/api/security/scan*` 接口行为(但其底层静态规则/正则库被 review 主流程复用)
- 不修改前端 Agent 办公室 UI
- 不重构 `audit_service`
- 不新增独立的"代码审计"页面,在现有"审查任务"页面展示增强后的漏洞信息

---

## 六、进入 Architect 阶段条件

- [x] 需求描述清晰无歧义
- [x] 技术实现方案与现有架构对齐
- [x] 验收标准具体可测试
- [x] 所有关键假设已确认(8 个决策点全部用户确认)
- [x] 项目特性规范已对齐(MySQL/Python 3.9/ruff/6A)

下一步: 进入 Architect 阶段,生成 `DESIGN_代码审计Agent集成与漏洞识别增强.md`。
