# CONSENSUS · 代码审计 Agent 集成与漏洞识别增强

> 任务名: `代码审计Agent集成与漏洞识别增强`
> 创建时间: 2026-06-25
> 阶段: Align → Architect 输入
> 状态: 所有需求边界与技术决策已与用户达成最终共识,可进入 Architect 阶段
> 输入: ALIGNMENT_代码审计Agent集成与漏洞识别增强.md(两轮对齐结果)

---

## 一、最终需求描述

### 1.1 总体目标

将项目中分离的 Agent 模块与代码审查主流程深度集成,引入静态规则+LLM 双引擎漏洞识别能力,补齐编辑器压缩包处理缺陷,同步引入 RBAC 细粒度权限、文件上传双引擎恶意扫描、四格式可配置报告体系,最终通过含已知漏洞的样本代码本地全栈验证,并同步部署到线上服务器(81.70.251.90)。

### 1.2 功能性需求清单

| 编号 | 需求 | 优先级 |
|------|------|--------|
| FR-1 | `review_service._execute_review()` 重构为通过 `BaseAgent.call()` 调用真实 Agent(`CodeReviewerAgent`/`SecuritySentinelAgent`/`ReviewOrchestratorAgent`),统一事件总线/调用日志/AiCallLog 归因 | P0 |
| FR-2 | `SecuritySentinelAgent` 的 20 条静态规则 + 20 类正则秘钥扫描接入 review 主流程作为前置过滤,先确定性命中再 LLM 深度审查 | P0 |
| FR-3 | `ReviewIssue` 表扩展字段:`cwe_id`/`owasp_category`/`cvss_score`/`cvss_vector`/`severity`/`confidence`/`compliance_mapping`/`evidence`/`exploit_scenario`/`remediation`/`references`,通过 Alembic 迁移 | P0 |
| FR-4 | 审查 Prompt 增强:强制 LLM 输出结构化漏洞元数据(JSON Schema 约束),失败时降级为旧格式并告警 | P0 |
| FR-5 | 编辑器压缩包 base64 修复:后端识别压缩包(zip/tar/gz/bz2/xz)自动解压入库为多个文件;前端 `CodeEditor.vue` 拒绝渲染 binary 文件改为下载入口;`CodeFileList.vue` 标记压缩包已展开 | P0 |
| FR-6 | 新建 6-8 个漏洞测试样本(SQL注入/XSS/硬编码密钥/路径遍历/反序列化/SSRF/命令注入),配合现有 `tests/fixtures/vuln_samples/` 与 `qa_security_fixture.py` 本地真实跑 review 任务验证 | P0 |
| FR-7 | RBAC 全量细粒度权限:新增 `role`/`permission`/`role_permission`/`user_role`/`menu`/`data_scope` 共 6 张表;权限点覆盖项目/审查/问题/Agent/规则/报告/用户/菜单/数据范围;前端新增角色管理、权限分配、菜单管理 3 个页面 | P0 |
| FR-8 | 文件上传安全:MIME 白名单(代码文件扩展名集合,拒绝 .exe/.dll/.so/.dylib/.bat);单文件 10MB;项目总 500MB;ClamAV+YARA 双引擎扫描,扫描失败降级为启发式校验并告警 | P0 |
| FR-9 | 报告输出:保留 PDF/Word,新增 JSON 结构化报告与 HTML 在线报告(可分享链接);新增"报告模板管理"页,支持简洁版/详细版/合规版 3 套模板,模板引擎 Jinja2(后端)+ 动态表单(前端) | P0 |
| FR-10 | 合规映射:建立 ISO 27001 + GDPR + PCI-DSS + HIPAA 4 套合规条款字典(独立常量模块),`compliance_mapping` 字段为 JSON,形如 `{"iso27001": ["A.14.2.1"], "gdpr": ["Art.32"], "pci_dss": ["Req-6.2.4"], "hipaa": ["§164.312(b)"]}` | P0 |
| FR-11 | 本地全栈验证:Docker MySQL(3307)+ 后端(8000)+ 前端(5173)真实点击 + API 调用,验证 Agent 调用链路与漏洞识别效果 | P0 |
| FR-12 | 服务器同步:rsync 同步 `backend/`/`frontend/`/`deploy/`/`docs/` 到 `81.70.251.90:/opt/code-review/`;Alembic 迁移自动升级数据库结构(保留线上数据);`.env` 不覆盖;执行 `deploy/deploy.sh` 重建容器 | P0 |

### 1.3 非功能性需求

| 编号 | 需求 |
|------|------|
| NFR-1 | Python 3.9 兼容,类型注解用 `Optional[X]` 而非 `X | None` |
| NFR-2 | ruff + compileall 全部通过;所有新增函数有函数级注释(功能/参数/返回值) |
| NFR-3 | MySQL 8.0,严禁 SQLite;`DB_HOST=127.0.0.1`,`DB_PORT=3307` |
| NFR-4 | API Key 在 `.env`,不提交 git |
| NFR-5 | 所有新增/修改路由保留向后兼容,旧客户端不破坏 |
| NFR-6 | 压缩包解压严格防护 zip slip;解压文件数 ≤100;总大小 ≤50MB;单文件 ≤10MB |
| NFR-7 | ClamAV 容器 freshclam 自动更新;扫描超时 30s 降级;YARA 规则集针对 webshell/后门特征 |
| NFR-8 | LLM 双引擎可环境变量关闭(`REVIEW_ENABLE_STATIC_RULES=true/false`、`REVIEW_ENABLE_LLM=true/false`) |
| NFR-9 | 前端 vue-tsc 零错误,`npm run build` 通过 |
| NFR-10 | 所有新增单测通过,且 v1.0/v2.0 既有 38 项测试无回归 |

---

## 二、验收标准(可测试)

### 2.1 Agent 集成验收

- AC-1.1 `review_service._execute_review()` 调用栈中可见 `BaseAgent.call()`、`CodeReviewerAgent.call()` 或 `SecuritySentinelAgent.call()`,可通过断点/日志验证
- AC-1.2 `AiCallLog.agent_label` 字段值为真实注册的 Agent name(`code_reviewer`/`security_sentinel`),不再是 `general/security` 画像 code
- AC-1.3 `AgentEventBus` 在 review 任务执行期间发出 `DISPATCH/THINKING/COMPLETE` 事件,SSE 频道可订阅到
- AC-1.4 `quick/standard` 类型审查调用 `CodeReviewerAgent`;`security/full` 类型审查调用 `SecuritySentinelAgent`(含静态规则+正则秘钥+LLM+数据流)

### 2.2 漏洞识别验收

- AC-2.1 6-8 个漏洞样本中,每个样本至少 1 个漏洞被识别,且 `cwe_id`/`owasp_category`/`cvss_score`/`severity` 字段非空
- AC-2.2 `compliance_mapping` 字段对每个漏洞至少映射 1 个合规标准条款
- AC-2.3 `evidence` 字段包含漏洞代码片段(行号+代码内容);`exploit_scenario` 字段包含攻击场景描述;`remediation` 字段包含修复建议;`references` 字段包含官方参考链接
- AC-2.4 静态规则前置过滤命中率统计可查(`static_rule_hits` 字段)

### 2.3 编辑器压缩包验收

- AC-3.1 上传 `test.zip`(含 3 个 .py 文件)后,数据库 `code_file` 表新增 3 条记录,不再有 base64 内容字段
- AC-3.2 上传 zip slip 恶意压缩包(`../evil.py`)返回 400 错误,不解压
- AC-3.3 前端 `CodeEditor.vue` 对 binary 文件显示"该文件为二进制,请下载查看"提示与下载按钮,不再显示 base64
- AC-3.4 `CodeFileList.vue` 对原压缩包文件显示"已展开为 N 个文件"标记

### 2.4 RBAC 验收

- AC-4.1 6 张 RBAC 表通过 Alembic 迁移创建,`role` 表预置 `user`/`reviewer`/`auditor`/`admin`/`super_admin` 5 个角色
- AC-4.2 `permission` 表覆盖 ≥30 个权限点(项目/审查/问题/Agent/规则/报告/用户/菜单/数据范围)
- AC-4.3 路由层用 `@require_permission("review:start")` 装饰器鉴权,无权限返回 403
- AC-4.4 前端菜单按 `menu` 表配置动态渲染,无权限菜单不显示
- AC-4.5 数据范围:普通用户只能看自己项目,管理员可见全部

### 2.5 上传安全验收

- AC-5.1 上传 `.exe` 文件返回 415 错误
- AC-5.2 上传 11MB 单文件返回 413 错误
- AC-5.3 上传超过 500MB 项目总文件返回 413 错误
- AC-5.4 上传含 EICAR 测试签名的文件被 ClamAV 拒绝
- AC-5.5 上传含 webshell 特征的 .php 文件被 YARA 拒绝
- AC-5.6 ClamAV 容器停机时上传不阻塞,降级为启发式校验,日志告警

### 2.6 报告验收

- AC-6.1 PDF/Word/JSON/HTML 四种格式均可下载/查看
- AC-6.2 JSON 报告字段包含 `summary/issues[]/metrics/compliance_summary`,每条 issue 含全部新字段
- AC-6.3 HTML 报告有独立可分享链接(`/api/reports/{id}/html`)
- AC-6.4 模板管理页可创建/编辑/删除模板,3 套预置模板(简洁版/详细版/合规版)可切换
- AC-6.5 合规版报告按 ISO 27001/GDPR/PCI-DSS/HIPAA 分章节汇总

### 2.7 服务器同步验收

- AC-7.1 `rsync -avz --exclude='.env' --exclude='__pycache__' backend/ frontend/ deploy/ docs/ root@81.70.251.90:/opt/code-review/` 执行成功
- AC-7.2 服务器执行 `alembic upgrade head` 数据库结构升级,数据未丢失
- AC-7.3 `deploy/deploy.sh` 重建容器成功,线上 `lijiadong.cn` 访问正常
- AC-7.4 线上 review 任务可正常启动并返回结构化漏洞结果

### 2.8 整体回归验收

- AC-8.1 既有 38 项单测全部通过
- AC-8.2 新增单测 ≥30 项全部通过(覆盖 RBAC/上传安全/双引擎/报告/压缩包)
- AC-8.3 `ruff check backend/` 零警告
- AC-8.4 `python -m compileall backend/app` 通过
- AC-8.5 前端 `vue-tsc --noEmit` 零错误,`npm run build` 通过

---

## 三、技术实现方案

### 3.1 后端核心改造点

| 模块 | 改造内容 |
|------|---------|
| `app/services/review_service.py` | `_execute_review()` 改为通过 `AgentRegistry.get("code_reviewer")`/`get("security_sentinel")` 获取 Agent 实例,调用 `agent.call(ctx)`,统一事件总线与 AiCallLog 归因。保留旧路径作为 `REVIEW_USE_BASE_AGENT=false` 时的 fallback |
| `app/agents/review_agent.py` | `CodeReviewerAgent` 实现 `handle()` 方法,内部组装 Prompt(含强制 JSON Schema 输出约束)、调用 `DeepSeekAgent.chat()`、解析结果为 `Issue` 列表 |
| `app/agents/security_sentinel_agent.py` | `SecuritySentinelAgent` 新增 `pre_scan(code_files) -> List[Finding]` 方法,执行 20 条静态规则 + 20 类正则秘钥扫描,作为 review 主流程前置过滤 |
| `app/ai/static_analyzer.py` | 新增 `StaticAnalyzer.scan()` 静态分析入口,聚合 `security_static_rules` + `security_patterns` |
| `app/ai/prompt_builder.py` | 增强 Prompt:强制 LLM 输出 JSON 数组,每条含 `cwe_id/owasp_category/cvss_score/cvss_vector/severity/confidence/compliance_mapping/evidence/exploit_scenario/remediation/references` |
| `app/ai/result_parser.py` | `Issue` dataclass 扩展上述字段;`parse_issues()` 兼容新旧两种 LLM 输出格式 |
| `app/models/review_issue.py` | ORM 模型扩展新字段;Alembic 迁移脚本 `006_review_issue_vuln_metadata_full.py` |
| `app/schemas/review.py` | Pydantic Schema 扩展对应字段 |
| `app/services/code_file_service.py` | `upload()` 入口识别压缩包并调用 `archive_extractor.extract_archive()`;调用 `MalwareScanner.scan()`(ClamAV+YARA);MIME 白名单与大小校验 |
| `app/utils/file_validator.py` | 新增 `ALLOWED_MIME_EXTENSIONS` 常量与 `validate_mime()` 函数 |
| `app/utils/malware_scanner.py` | 新增模块,封装 ClamAV(clamd 库)+ YARA(yara-python)双引擎,支持降级 |
| `app/services/rbac_service.py` | 新增 RBAC 服务,提供 `assign_role`/`check_permission`/`list_menus_by_user`/`get_data_scope` |
| `app/core/dependencies.py` | 新增 `require_permission(perm: str)` 依赖注入装饰器 |
| `app/api/v1/rbac.py` | 新增 RBAC 管理路由(角色/权限/菜单 CRUD) |
| `app/api/v1/reports.py` | 新增 `/reports/{id}/json`、`/reports/{id}/html`、模板管理路由 |
| `app/exporters/json_exporter.py` | 新增 JSON 报告导出器 |
| `app/exporters/html_exporter.py` | 新增 HTML 报告导出器(Jinja2 模板) |
| `app/exporters/templates/` | 新增 `simple.md.j2`/`detailed.md.j2`/`compliance.md.j2` 3 套 Jinja2 模板 |
| `app/constants/compliance.py` | 新增 ISO 27001/GDPR/PCI-DSS/HIPAA 4 套合规条款字典 |

### 3.2 前端核心改造点

| 模块 | 改造内容 |
|------|---------|
| `views/code/CodeEditor.vue` | 检测 `detail.language === 'binary'` 或 `detail.is_binary` 时显示下载入口与提示,不传 Monaco |
| `views/code/CodeFileList.vue` | 标记压缩包已展开;过滤 binary 文件可点击进入编辑器 |
| `views/admin/RoleManage.vue` | 新增角色管理页 |
| `views/admin/PermissionAssign.vue` | 新增权限分配页(角色×权限矩阵) |
| `views/admin/MenuManage.vue` | 新增菜单管理页 |
| `views/admin/ReportTemplate.vue` | 新增报告模板管理页 |
| `views/report/ReportDetail.vue` | 新增 JSON/HTML 下载按钮;支持模板切换 |
| `stores/permission.ts` | 新增权限 store,登录后拉取用户权限点与菜单 |
| `router/index.ts` | 路由守卫接入权限点检查 |
| `api/rbac.ts` | 新增 RBAC API 封装 |
| `api/report.ts` | 扩展 JSON/HTML/模板 API |

### 3.3 数据库迁移

新增 Alembic 迁移脚本:
- `006_review_issue_vuln_metadata_full.py`:`review_issue` 表新增 11 个字段
- `007_rbac_tables.py`:创建 6 张 RBAC 表 + 预置 5 角色 + 30+ 权限点 + 默认菜单
- `008_report_template.py`:`report_template` 表(模板名/类型/内容/创建者)
- `009_code_file_binary_flag.py`:`code_file` 表新增 `is_binary`/`raw_size` 字段
- `010_malware_scan_log.py`:`malware_scan_log` 表(扫描记录)

### 3.4 Docker Compose 改造

`deploy/docker-compose.yml` 新增服务:
- `clamav`:ClamAV 官方镜像,挂载病毒库卷,暴露 3310 端口(仅内网)
- `yara-rules`:YARA 规则集卷(定期更新)

后端容器依赖 clamav,环境变量 `CLAMD_HOST=clamav:3310`。

### 3.5 集成方案

- **DeepSeek API**:沿用 `.env` 中 `DEEPSEEK_API_KEY`,通过 `DeepSeekAgent.chat()` 调用
- **AgentEventBus**:review 任务执行期间发出事件,前端 SSE 订阅
- **AiCallLog**:每次 `BaseAgent.call()` 自动写入,`agent_label` 用真实 Agent name
- **MySQL**:所有新表通过 Alembic 迁移,不手动改库
- **Caddy/HTTPS**:线上域名 `lijiadong.cn` 不变,新增 `/api/reports/{id}/html` 路由无需特殊配置

---

## 四、任务边界与限制

### 4.1 做(范围内)

见 §1.2 功能性需求清单 FR-1 ~ FR-12。

### 4.2 不做(范围外)

- 不重构 `audit_service`(操作审计日志)
- 不重构圆桌讨论审功能(`/api/discuss/*`、`/api/ws/discuss/*`)
- 不重构 Agent 自进化/治理平台功能(`/api/evolution/*`、`/api/admin/audit/*`)
- 不修改数据库已有的 15 张业务表结构(仅新增字段或新增表)
- 不重构 `/api/security/scan*` 独立安全扫描接口的对外行为(内部复用 `SecuritySentinelAgent` 实例)
- 不引入新的前端 UI 框架(沿用 Element Plus + Monaco Editor)
- 不引入新的后端框架(沿用 FastAPI + SQLAlchemy)
- 不修改前端 Agent 办公室 UI(除非必要)
- 不引入消息队列(沿用 EventBus 内存版)
- 不引入 Redis(沿用内存存储 ClarifyStore)

### 4.3 验收边界

- **本地验收**:本地 Docker MySQL + 后端(8000)+ 前端(5173)真实跑通 6-8 个漏洞样本的 review 任务
- **线上验收**:服务器同步后,线上 `lijiadong.cn` 访问正常,review 任务可启动并返回结构化漏洞结果
- **回归验收**:既有 38 项单测无回归,新增 ≥30 项单测通过

---

## 五、不确定性确认

| 不确定性 | 解决状态 | 共识 |
|---------|---------|------|
| 漏洞分类体系粒度 | ✅ 已确认 | 全量 CWE+OWASP+CVSS v3.1+4 合规标准 |
| 权限模型复杂度 | ✅ 已确认 | RBAC 全量+菜单+数据范围(6 张新表) |
| 上传安全策略 | ✅ 已确认 | MIME 白名单 + 大小限制 + ClamAV+YARA 双引擎 |
| 报告格式 | ✅ 已确认 | PDF+Word+JSON+HTML + 3 套可配置模板 |
| 交付策略 | ✅ 已确认 | 一次性全量交付,严格按 6A 推进 |
| 服务器同步方式 | ✅ 已确认 | rsync + Alembic 迁移 + deploy.sh 重建 |
| Agent 集成深度 | ✅ 已确认 | 通过 BaseAgent.call() 深度集成 |
| 压缩包修复方式 | ✅ 已确认 | 后端自动解压入库为多个文件 |
| LLM API Key 来源 | ✅ 已确认 | 沿用 `.env` 中 DeepSeek Key |
| 测试样本策略 | ✅ 已确认 | 新建 6-8 个 + 现有 fixture |

**所有关键不确定性已解决,可进入 Architect 阶段。**

---

## 六、质量门控(Align 阶段自检)

| 门控项 | 状态 |
|--------|------|
| 需求边界清晰无歧义 | ✅ 做/不做清单明确,16 项功能性需求 + 10 项非功能性需求 |
| 技术方案与现有架构对齐 | ✅ 沿用 FastAPI/SQLAlchemy/Element Plus/Monaco/DeepSeek/MySQL/Docker |
| 验收标准具体可测试 | ✅ 8 大类 30+ 条可执行验收标准 |
| 所有关键假设已确认 | ✅ 10 项不确定性全部解决 |
| 项目特性规范已对齐 | ✅ Python 3.9/MySQL 8.0/ruff/函数注释/.env/Alembic 全部对齐 |

---

## 七、进入 Architect 阶段准备

输入文档已就绪:
- `ALIGNMENT_代码审计Agent集成与漏洞识别增强.md`(项目上下文分析 + 两轮对齐决策)
- 本 CONSENSUS 文档(最终共识)

下一步: 进入 Architect 阶段,生成 `DESIGN_代码审计Agent集成与漏洞识别增强.md`,包含:
- 整体架构图(mermaid)
- 分层设计与核心组件
- 模块依赖关系图
- 接口契约定义(新增/修改 API 详细 schema)
- 数据流向图
- 异常处理策略(降级/重试/超时)
- RBAC 权限点完整清单
- 合规条款字典设计
- 报告模板 Jinja2 结构
