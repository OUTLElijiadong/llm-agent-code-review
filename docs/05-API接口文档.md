# 05 · API 接口文档

> 本文提供核心接口的人工说明与示例。**当前契约权威来源是 FastAPI 路由、Pydantic Schema 和生成的 OpenAPI，而不是手工维护的数量。**截至 2026-07-10，代码包含 194 条 `/api/*` 业务 HTTP 操作和 1 条 WebSocket；本文件对后续新增的治理、RBAC、社区、知识库、工单等接口仅做分组索引，未逐条展开全部 194 条。
>
> 本地可在 `OPENAPI_ENABLED=true` 时通过 `/docs` 查看；当前生产虽关闭 `/docs`，但 `/openapi.json` 仍公开，这是待修复的 P1 偏差，不应视为既定安全策略。

## 1. 通用约定

### 1.1 基础信息

- **Base URL(开发)**:`http://localhost:8000`
- **Base URL(生产)**:`https://your-domain.com`
- **接口前缀**:`/api`
- **接口风格**:RESTful + JSON
- **字符集**:UTF-8
- **时间格式**:ISO-8601,如 `2026-05-26T10:30:00+08:00`

### 1.2 鉴权

除 `/api/auth/login` 与 `/api/auth/register` 外,所有接口都需要 JWT。

```
Authorization: Bearer <access_token>
```

token 过期返回 `401 40101`,前端需引导用户重新登录。

### 1.3 统一响应结构

**成功**

```json
{
  "code": 0,
  "message": "ok",
  "data": { ... }
}
```

**失败**

```json
{
  "code": 40001,
  "message": "用户名或密码错误",
  "detail": null,
  "request_id": "req_8f2a3c..."
}
```

> 所有接口都遵循该结构。HTTP 状态码与 code 字段共同表达语义:5xx 表示后端错误,4xx 表示客户端错误,2xx + code≠0 在本系统中不出现(成功固定 code=0)。

### 1.4 分页参数

| 参数 | 类型 | 默认 | 上限 |
| --- | --- | --- | --- |
| `page` | int | 1 | - |
| `page_size` | int | 20 | 100 |

分页响应:

```json
{
  "code": 0,
  "data": {
    "items": [...],
    "total": 128,
    "page": 1,
    "page_size": 20,
    "pages": 7
  }
}
```

### 1.5 错误码

| code | HTTP | 含义 |
| --- | --- | --- |
| 0 | 200 | 成功 |
| 40001 | 400 | 参数错误 |
| 40002 | 400 | 参数校验失败(字段级) |
| 40100 | 401 | 未登录 / token 缺失 |
| 40101 | 401 | token 过期 |
| 40102 | 401 | token 非法 |
| 40300 | 403 | 无访问权限 |
| 40301 | 403 | 账号已禁用 |
| 40400 | 404 | 资源不存在 |
| 40901 | 409 | 用户名/项目名重复 |
| 41301 | 413 | 上传文件超出限制 |
| 41500 | 415 | 不支持的文件类型 |
| 42900 | 429 | 请求过于频繁 |
| 50000 | 500 | 服务器内部错误 |
| 50201 | 502 | DeepSeek 服务不可达 |
| 50301 | 503 | 服务暂不可用(任务系统繁忙) |

### 1.6 当前接口基线与分组

统计口径：FastAPI `APIRoute` 的 HTTP 方法数；同一路径存在多个方法时分别计数；不含 `/healthz`，WebSocket 单独统计。

| 前缀/模块 | HTTP 操作数 | 说明 |
| --- | ---: | --- |
| `/api/auth` | 5 | 注册、登录、当前用户、改密、退出 |
| `/api/users` | 4 | 用户管理 |
| `/api/projects` | 5 | 项目 CRUD |
| `/api/projects/{project_id}/members` | 4 | 项目成员 |
| `/api/code-files` | 13 | 文件、上传、版本、回滚 |
| `/api/rules` | 5 | 审查规则 |
| `/api/review` | 6 | 审查任务 |
| `/api/issues` | 4 | 问题闭环 |
| `/api/reports` | 12 | 报告、预览、模板、导出 |
| `/api/dashboard` | 5 | 仪表盘 |
| `/api/ai-logs` | 2 | AI 调用日志 |
| `/api/ai` | 4 | AI 助手与调度 |
| `/api/ai-prompt` | 4 | 修复提示词 |
| `/api/security` | 7 | 安全审计 |
| `/api/agents` | 14 | Agent 中心与 Skill 调用 |
| `/api/evolution` | 11 | Agent 自进化 |
| `/api/admin/audit` | 1 | 操作审计 |
| API 配置相关路由 | 4 | 用户 API 配置 |
| `/api/maintenance` | 6 | 维修工单 |
| `/api/feedback` | 5 | 用户反馈 |
| `/api/forum` | 9 | 开发者论坛 |
| `/api/me` | 3 | 用户画像 |
| `/api/knowledge` | 8 | 个人知识库 |
| `/api/admin/llm` | 3 | 全局大模型配置 |
| `/api/admin/*` Agent 治理 | 34 | Profile、权限、策略、记忆、知识、任务、反思、奖励、产物、告警、指标 |
| `/api/rbac` | 15 | 角色、权限、菜单、数据范围 |
| `/api/discuss` | 1 | 圆桌讨论预检 |
| **合计** | **194** | 业务 HTTP 操作 |

WebSocket：`/api/ws/discuss/{session_id}`，共 1 条。

> 逐条契约应从当前 OpenAPI 生成并归档。本文后续章节保留核心手工说明，但不再宣称覆盖“所有接口”。

---

## 2. 鉴权 `/api/auth`

### 2.1 用户注册

`POST /api/auth/register`

**Request**

```json
{
  "username": "alice",
  "password": "alice123",
  "email": "alice@example.com",
  "nickname": "Alice"
}
```

**Response**

```json
{
  "code": 0,
  "data": {
    "user_id": 12,
    "username": "alice"
  }
}
```

**错误**:`40001` 字段格式错误;`40901` 用户名重复。

### 2.2 用户登录

`POST /api/auth/login`

**Request**

```json
{ "username": "alice", "password": "alice123" }
```

**Response**

```json
{
  "code": 0,
  "data": {
    "access_token": "eyJhbGciOi...",
    "token_type": "Bearer",
    "expires_in": 604800,
    "user": {
      "id": 12,
      "username": "alice",
      "nickname": "Alice",
      "role": "user",
      "email": "alice@example.com"
    }
  }
}
```

**错误**:`40001` 用户名或密码错误;`40301` 账号禁用。

### 2.3 获取当前用户

`GET /api/auth/me`

**Response**

```json
{
  "code": 0,
  "data": {
    "id": 12,
    "username": "alice",
    "nickname": "Alice",
    "email": "alice@example.com",
    "role": "user",
    "status": 1,
    "last_login": "2026-05-26T10:30:00+08:00",
    "create_time": "2026-04-10T15:00:00+08:00"
  }
}
```

### 2.4 修改密码

`POST /api/auth/change-password`

```json
{ "old_password": "alice123", "new_password": "alice2026" }
```

**Response** `{ "code": 0, "data": null }`

### 2.5 退出登录

`POST /api/auth/logout`

> 后端无状态实现,接口仅做日志记录。前端清除本地 token 即视为登出。

---

## 3. 用户管理 `/api/users`(管理员)

### 3.1 用户列表

`GET /api/users?keyword=&role=&status=&page=1&page_size=20`

**Response**

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "id": 1,
        "username": "admin",
        "nickname": "管理员",
        "email": "admin@local",
        "role": "admin",
        "status": 1,
        "last_login": "2026-05-26T09:00:00+08:00",
        "create_time": "2026-04-01T00:00:00+08:00"
      }
    ],
    "total": 5, "page": 1, "page_size": 20, "pages": 1
  }
}
```

### 3.2 重置密码

`POST /api/users/{id}/reset-password`

**Response** `{ "code": 0, "data": { "default_password": "123456" } }`

### 3.3 启用/禁用

`POST /api/users/{id}/toggle-status`

```json
{ "status": 0 }
```

### 3.4 设置角色

`POST /api/users/{id}/role`

```json
{ "role": "admin" }
```

---

## 4. 项目 `/api/projects`

### 4.1 项目列表

`GET /api/projects?keyword=&language=&status=active&page=1&page_size=20`

**Response**

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "id": 7,
        "project_name": "在线商城系统",
        "description": "Flask + Vue 的电商示例",
        "language": "python",
        "status": "active",
        "file_count": 12,
        "last_review_at": "2026-05-25T20:00:00+08:00",
        "create_time": "2026-04-15T10:00:00+08:00"
      }
    ],
    "total": 3, "page": 1, "page_size": 20, "pages": 1
  }
}
```

### 4.2 创建项目

`POST /api/projects`

```json
{
  "project_name": "在线商城系统",
  "description": "Flask + Vue 的电商示例",
  "language": "python"
}
```

**Response**

```json
{ "code": 0, "data": { "id": 7 } }
```

**错误**:`40901` 项目名重复。

### 4.3 项目详情

`GET /api/projects/{id}`

```json
{
  "code": 0,
  "data": {
    "id": 7,
    "project_name": "在线商城系统",
    "description": "...",
    "language": "python",
    "status": "active",
    "file_count": 12,
    "create_time": "...",
    "update_time": "...",
    "recent_tasks": [
      {
        "id": 33, "score": 85, "total_issues": 18,
        "status": "success", "create_time": "..."
      }
    ]
  }
}
```

### 4.4 更新项目

`PUT /api/projects/{id}`

```json
{
  "project_name": "新项目名",
  "description": "更新描述",
  "language": "python",
  "status": "archived"
}
```

### 4.5 删除项目

`DELETE /api/projects/{id}`

软删除,response `{ "code": 0, "data": null }`。

---

## 5. 代码文件 `/api/code-files`

### 5.1 文件列表

`GET /api/code-files?project_id=7&language=python&keyword=&page=1&page_size=20`

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "id": 101,
        "project_id": 7,
        "file_name": "auth.py",
        "file_path": "src/auth.py",
        "language": "python",
        "size_bytes": 4321,
        "line_count": 132,
        "version_no": 3,
        "create_time": "...",
        "update_time": "..."
      }
    ],
    "total": 12, "page": 1, "page_size": 20, "pages": 1
  }
}
```

### 5.2 上传文件

`POST /api/code-files/upload` (multipart/form-data)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project_id` | int | 项目 id |
| `file` | binary | 单文件 ≤ 20MB,后端默认不限制扩展名但阻止系统/构建目录 |
| `file_path` | string | 选填,逻辑路径 |
| `language` | string | 选填,不填则自动识别 |

**Response**

```json
{ "code": 0, "data": { "file_id": 101, "language": "python", "version_no": 1 } }
```

### 5.2.1 批量上传文件夹

`POST /api/code-files/upload-folder` (multipart/form-data)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project_id` | int | 项目 id |
| `files` | binary[] | 多文件列表,每个文件沿用单文件上传校验 |

**Response**

```json
{
  "code": 0,
  "data": {
    "success_count": 2,
    "fail_count": 1,
    "files": [
      { "file_name": "src/a.py", "file_id": 101, "language": "python", "version_no": 1 }
    ],
    "errors": [
      { "file_name": "node_modules/x.js", "error": "不允许上传系统目录中的文件" }
    ]
  }
}
```

**错误**:`41301` 文件超出限制;`41500` 扩展名不支持。

### 5.3 在线新增文件

`POST /api/code-files`

```json
{
  "project_id": 7,
  "file_name": "utils.py",
  "file_path": "src/utils.py",
  "language": "python",
  "content": "def add(a, b):\n    return a + b\n"
}
```

**Response** `{ "code": 0, "data": { "file_id": 102, "version_no": 1 } }`

### 5.4 文件详情(含内容)

`GET /api/code-files/{id}`

```json
{
  "code": 0,
  "data": {
    "id": 101,
    "project_id": 7,
    "file_name": "auth.py",
    "file_path": "src/auth.py",
    "language": "python",
    "size_bytes": 4321,
    "line_count": 132,
    "version_no": 3,
    "content": "import bcrypt\n...",
    "create_time": "...",
    "update_time": "..."
  }
}
```

### 5.5 更新文件内容(生成新版本)

`PUT /api/code-files/{id}`

```json
{
  "content": "<新代码>",
  "change_desc": "修复登录校验"
}
```

**Response** `{ "code": 0, "data": { "version_no": 4 } }`

### 5.6 重命名文件

`POST /api/code-files/{id}/rename`

```json
{ "file_name": "auth_v2.py", "file_path": "src/auth_v2.py" }
```

### 5.7 删除文件

`DELETE /api/code-files/{id}`

软删除,返回 `{ "code": 0, "data": null }`。

### 5.8 文件版本列表

`GET /api/code-files/{id}/versions?page=1&page_size=20`

```json
{
  "code": 0,
  "data": {
    "items": [
      { "version_no": 3, "change_desc": "修复登录", "operator_id": 12, "create_time": "..." },
      { "version_no": 2, "change_desc": "添加注释", "operator_id": 12, "create_time": "..." },
      { "version_no": 1, "change_desc": "初始上传", "operator_id": 12, "create_time": "..." }
    ],
    "total": 3, "page": 1, "page_size": 20, "pages": 1
  }
}
```

### 5.9 查看历史版本内容

`GET /api/code-files/{id}/versions/{version_no}`

```json
{
  "code": 0,
  "data": {
    "file_id": 101,
    "version_no": 2,
    "content": "...",
    "change_desc": "添加注释",
    "create_time": "..."
  }
}
```

### 5.10 回滚到历史版本

`POST /api/code-files/{id}/versions/{version_no}/restore`

将指定版本内容作为新版本回写。

**Response** `{ "code": 0, "data": { "version_no": 5 } }`

---

## 6. 审查规则 `/api/rules`

### 6.1 规则列表

`GET /api/rules?enabled=&scope=mine`

`scope=mine` 表示当前用户视角(合并内置规则与个人覆盖)。

```json
{
  "code": 0,
  "data": [
    {
      "id": 1, "rule_code": "code_style", "rule_name": "代码规范",
      "rule_type": "style", "rule_content": "...",
      "enabled": 1, "is_builtin": 1, "sort_order": 1
    }
  ]
}
```

### 6.2 启用/禁用规则

`POST /api/rules/{id}/toggle`

```json
{ "enabled": 0 }
```

### 6.3 新增自定义规则

`POST /api/rules`

```json
{
  "rule_code": "my_rule",
  "rule_name": "自定义检查 N+1 查询",
  "rule_type": "performance",
  "rule_content": "重点检查 ORM 中的 N+1 查询模式..."
}
```

### 6.4 更新自定义规则

`PUT /api/rules/{id}`

### 6.5 删除自定义规则

`DELETE /api/rules/{id}`

> 内置规则不可删,只能 toggle。

---

## 7. 审查任务 `/api/review`

### 7.1 启动审查

`POST /api/review/start`

```json
{
  "project_id": 7,
  "file_ids": [101, 102, 103],
  "review_type": "full",
  "task_name": "登录模块审查"
}
```

字段说明:

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `project_id` | int | 是 | 项目 id |
| `file_ids` | int[] | 是 | 文件 id 数组,1-50 个 |
| `review_type` | string | 否 | `quick / standard / security / performance / full`,默认 standard。其中 `security/performance/full` 会启用多 Agent 组合 |
| `task_name` | string | 否 | 任务名,缺省取"项目名 + 时间" |

**Response**

```json
{
  "code": 0,
  "data": {
    "task_id": 33,
    "status": "running"
  }
}
```

> 一期同步执行,接口阻塞到任务完成才返回(单文件 < 30 秒)。

### 7.2 任务列表

`GET /api/review/tasks?project_id=&status=&start=&end=&page=1&page_size=20`

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "id": 33, "task_name": "登录模块审查",
        "project_id": 7, "project_name": "在线商城系统",
        "review_type": "full", "status": "success",
        "total_files": 3, "total_issues": 12,
        "severe_issues": 1, "high_issues": 3,
        "medium_issues": 5, "low_issues": 3,
        "score": 82, "duration_ms": 45230,
        "create_time": "2026-05-26T10:00:00+08:00"
      }
    ],
    "total": 8, "page": 1, "page_size": 20, "pages": 1
  }
}
```

### 7.3 任务详情

`GET /api/review/tasks/{id}`

```json
{
  "code": 0,
  "data": {
    "id": 33,
    "task_name": "登录模块审查",
    "review_type": "full",
    "status": "success",
    "total_files": 3,
    "processed_files": 3,
    "total_issues": 12,
    "severe_issues": 1, "high_issues": 3, "medium_issues": 5, "low_issues": 3,
    "score": 82,
    "summary": "整体代码结构清晰,但存在 1 处明显的 SQL 注入风险...",
    "model_name": "deepseek-v4-flash/multi-agent",
    "duration_ms": 45230,
    "start_time": "...",
    "end_time": "...",
    "create_time": "..."
  }
}
```

### 7.4 任务进度(轮询,预留未实现)

`GET /api/review/tasks/{id}/progress`

```json
{
  "code": 0,
  "data": {
    "status": "running",
    "total_files": 3,
    "processed_files": 2,
    "current_file": "utils.py"
  }
}
```

### 7.5 取消任务

`POST /api/review/tasks/{id}/cancel`

```json
{ "code": 0, "data": null }
```

### 7.6 任务问题列表

`GET /api/review/tasks/{id}/issues?file_id=&severity=&issue_type=&status=&page=1&page_size=50`

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "id": 501, "task_id": 33,
        "file_id": 101, "file_name": "auth.py",
        "line_number": 42, "end_line": 44,
        "issue_type": "安全漏洞", "severity": "严重",
        "title": "SQL 注入风险",
        "description": "auth.py 第 42 行使用字符串拼接构造 SQL,...",
        "suggestion": "改用参数化查询",
        "fixed_code": "cursor.execute(\"SELECT * FROM user WHERE name=%s\", (name,))",
        "status": "unfixed",
        "create_time": "..."
      }
    ],
    "total": 12, "page": 1, "page_size": 50, "pages": 1
  }
}
```

### 7.7 删除任务

`DELETE /api/review/tasks/{id}` (软删除)

---

## 8. 审查问题 `/api/issues`

### 8.1 问题详情

`GET /api/issues/{id}`

返回与 7.6 单条相同结构。

### 8.2 更新问题状态

`POST /api/issues/{id}/status`

```json
{ "status": "fixed", "note": "已用参数化查询替换" }
```

`status` 取值:`unfixed / fixed / ignored / pending_review`。

### 8.3 批量更新状态

`POST /api/issues/batch-status`

```json
{ "ids": [501, 502, 503], "status": "ignored" }
```

---

## 9. 审查报告 `/api/reports`

### 9.1 报告列表

`GET /api/reports?project_id=&start=&end=&page=1&page_size=20`

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "task_id": 33, "task_name": "登录模块审查",
        "project_name": "在线商城系统",
        "total_issues": 12, "score": 82,
        "create_time": "..."
      }
    ],
    "total": 8, "page": 1, "page_size": 20, "pages": 1
  }
}
```

### 9.2 报告详情(在线 HTML 渲染所需数据)

`GET /api/reports/{task_id}`

```json
{
  "code": 0,
  "data": {
    "project": { "id": 7, "name": "在线商城系统", "language": "python" },
    "task": { "id": 33, "name": "登录模块审查", "score": 82, "create_time": "..." },
    "stats": {
      "total_files": 3, "total_issues": 12,
      "severity": { "严重": 1, "高": 3, "中": 5, "低": 3 },
      "by_type": { "安全漏洞": 2, "代码规范": 4, "潜在Bug": 3, "性能问题": 3 }
    },
    "summary": "整体代码结构清晰...",
    "files": [
      {
        "file_id": 101, "file_name": "auth.py", "score": 78,
        "issues": [ ... ]
      }
    ],
    "rules_snapshot": [
      { "rule_code": "security", "rule_name": "安全漏洞", "enabled": 1 }
    ]
  }
}
```

### 9.3 导出 Word

`GET /api/reports/{task_id}/export/word`

响应头:

```
Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document
Content-Disposition: attachment; filename="review_report_33.docx"
```

二进制流。

### 9.4 导出 PDF

`GET /api/reports/{task_id}/export/pdf`

```
Content-Type: application/pdf
Content-Disposition: attachment; filename="review_report_33.pdf"
```

---

## 10. 仪表盘 `/api/dashboard`

### 10.1 汇总指标

`GET /api/dashboard/summary?scope=mine`

`scope`:`mine`(当前用户)或 `all`(仅 admin)。

```json
{
  "code": 0,
  "data": {
    "project_count": 5,
    "file_count": 48,
    "review_count": 22,
    "total_issues": 187,
    "severe_issues": 9,
    "avg_score": 81.2,
    "recent_tasks": [
      { "id": 33, "score": 82, "create_time": "..." }
    ]
  }
}
```

### 10.2 风险等级分布

`GET /api/dashboard/risk-distribution?days=30`

```json
{
  "code": 0,
  "data": [
    { "severity": "严重", "count": 9 },
    { "severity": "高",   "count": 32 },
    { "severity": "中",   "count": 84 },
    { "severity": "低",   "count": 62 }
  ]
}
```

### 10.3 问题类型分布

`GET /api/dashboard/issue-type-statistics?days=30`

```json
{
  "code": 0,
  "data": [
    { "issue_type": "代码规范",   "count": 52 },
    { "issue_type": "潜在Bug",    "count": 33 },
    { "issue_type": "安全漏洞",   "count": 12 },
    { "issue_type": "性能问题",   "count": 25 },
    { "issue_type": "异常处理",   "count": 18 },
    { "issue_type": "命名规范",   "count": 21 },
    { "issue_type": "可维护性",   "count": 14 },
    { "issue_type": "注释完整性", "count": 12 }
  ]
}
```

### 10.4 评分趋势

`GET /api/dashboard/score-trend?limit=10`

```json
{
  "code": 0,
  "data": [
    { "task_id": 24, "score": 76, "create_time": "2026-05-12T..." },
    { "task_id": 27, "score": 81, "create_time": "2026-05-15T..." },
    { "task_id": 33, "score": 82, "create_time": "2026-05-26T..." }
  ]
}
```

### 10.5 审查频次趋势

`GET /api/dashboard/review-frequency?days=30`

```json
{
  "code": 0,
  "data": [
    { "date": "2026-04-27", "count": 0 },
    { "date": "2026-04-28", "count": 2 },
    { "date": "2026-05-26", "count": 3 }
  ]
}
```

---

## 11. AI 调用日志 `/api/ai-logs`(管理员)

### 11.1 日志列表

`GET /api/ai-logs?task_id=&user_id=&status=&start=&end=&page=1&page_size=20`

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "id": 999, "task_id": 33, "user_id": 12,
        "file_id": 101, "chunk_index": 0,
        "model_name": "deepseek-v4-flash",
        "prompt_tokens": 612, "completion_tokens": 488, "total_tokens": 1100,
        "duration_ms": 8420, "status": "success",
        "create_time": "..."
      }
    ],
    "total": 230, "page": 1, "page_size": 20, "pages": 12
  }
}
```

### 11.2 日志详情

`GET /api/ai-logs/{id}`

```json
{
  "code": 0,
  "data": {
    "id": 999,
    "task_id": 33,
    "model_name": "deepseek-v4-flash",
    "prompt": "<完整 prompt 内容>",
    "response": "<完整 response 内容>",
    "status": "success",
    "error_message": null,
    "duration_ms": 8420,
    "create_time": "..."
  }
}
```

---

## 12. Agent 中心 `/api/agents` (v2.0)

### 12.1 Agent 运行时列表

`GET /api/agents/runtime`

返回所有已注册 Agent 的元数据与调用统计（与 AgentRegistry 一致）。

```json
{
  "code": 0,
  "data": [
    {
      "agent_id": "general_quality",
      "agent_name": "通用质量代理",
      "icon": "shield",
      "color": "#4A90D9",
      "category": "审查",
      "skills": ["代码规范", "命名规范"],
      "call_count": 42,
      "success_rate": 0.95
    }
  ]
}
```

### 12.2 Agent 注册汇总

`GET /api/agents/runtime/summary`

```json
{
  "code": 0,
  "data": {
    "total": 12,
    "by_category": { "审查": 5, "管理": 3, "分析": 2, "导出": 2 }
  }
}
```

### 12.3 态势感知

`GET /api/agents/situation?minutes=60`

```json
{
  "code": 0,
  "data": {
    "on_duty": 12,
    "working": 2,
    "idle": 10,
    "today_calls": 47,
    "wave": [{ "minute": 0, "count": 3 }, ...],
    "hot_agents": [{ "agent_id": "general_quality", "call_count": 12 }]
  }
}
```

### 12.4 SSE 实时事件流

`GET /api/agents/events?replay=20`

Server-Sent Events, 推送 Agent 调度 / 思考 / 完成 / 失败 / 追问事件。默认回放最近 20 条历史。

```
Content-Type: text/event-stream
event: agent
data: {"event":"dispatch","trace_id":"abc123","agent_id":"security","timestamp":"..."}
```

### 12.5 Clarify 追问回填

`POST /api/agents/clarify`

```json
{
  "clarify_id": "clar_abc123",
  "answers": { "project_id": 7 }
}
```

合并 payload 后继续执行原 intent。追问 5 分钟未回填自动失效。

---

## 13. AI 助手对话 `/api/ai` (v2.0)

### 13.1 Agent 聊天

`POST /api/ai/chat`

**Request**

```json
{
  "messages": [
    { "role": "user", "content": "帮我审查项目 7" }
  ],
  "stream": false,
  "trace_id": "trace_abc"
}
```

**Response**

```json
{
  "code": 0,
  "data": {
    "content": "正在启动审查...",
    "model": "deepseek-v4-flash/multi-agent",
    "trace_id": "trace_abc",
    "clarify": null
  }
}
```

> 当 Agent 需要追问时, `clarify` 字段非空, 前端渲染追问卡片。

### 13.2 语言识别

`POST /api/ai/detect-language`

```json
{ "project_name": "订单服务", "description": "基于 Flask 的电商后端" }
```

**Response** `{ "code": 0, "data": { "language": "python", "language_name": "Python", "confidence": "high" } }`

### 13.3 文件夹分析

`POST /api/ai/analyze-folder`

```json
{ "folder_name": "src", "file_names": ["auth.py", "utils.py", "models.py"] }
```

### 13.4 Agent 列表

`GET /api/ai/agents`

返回所有已注册 Agent 的描述。

---

## 14. AI 提示词生成 `/api/ai-prompt` (v2.0)

### 14.1 支持工具列表

`GET /api/ai-prompt/tools`

```json
{
  "code": 0,
  "data": [
    { "tool": "cursor", "label": "Cursor" },
    { "tool": "copilot", "label": "GitHub Copilot" },
    { "tool": "chatgpt", "label": "ChatGPT" },
    { "tool": "claude", "label": "Claude Code" }
  ]
}
```

### 14.2 单条问题生成

`POST /api/ai-prompt/issue`

```json
{ "issue_id": 501, "target_tool": "cursor", "use_llm": true }
```

**Response** `{ "code": 0, "data": { "title": "...", "content": "...", "tool": "cursor" } }`

### 14.3 任务批量生成

`POST /api/ai-prompt/task`

```json
{ "task_id": 33, "target_tool": "chatgpt", "severity": ["严重", "高"], "use_llm": true }
```

### 14.4 项目级 AI 修复手册

`POST /api/ai-prompt/project`

```json
{ "project_id": 7, "target_tool": "claude", "top_n": 10, "use_llm": true }
```

按严重度优先,取前 `top_n` 条问题生成修复提示词合集。

---

## 15. 安全审计 `/api/security` (v2.1)

### 15.1 安全规则清单

`GET /api/security/checklist`

返回 OWASP Top10、CWE 和内置敏感信息正则规则清单,供安全中心知识页和扫描弹窗使用。

### 15.2 单文件安全扫描

`POST /api/security/scan-file`

```json
{ "file_id": 101, "scan_depth": "standard" }
```

### 15.3 任务安全复审

`POST /api/security/scan-task`

```json
{ "task_id": 33 }
```

### 15.4 项目级威胁建模

`POST /api/security/scan-project`

```json
{ "project_id": 7, "top_n": 50, "trace_dataflow": true }
```

### 15.5 全量项目安全扫描

`POST /api/security/scan-all-projects`

```json
{ "top_n_per_project": 50, "trace_dataflow": true }
```

扫描当前用户可见的全部活跃项目。普通用户仅扫描自己的项目;管理员扫描全平台活跃项目。响应复用 `SecurityScanOut`,会聚合所有项目的 findings、风险评分、扫描文件数、接口清单、代码联动关系、跨文件攻击路径和多 Agent 讨论结论。

响应中 `threat_model` 额外包含:

- `api_endpoints`: 识别到的接口方法、路径、文件位置、处理函数和认证线索。
- `code_links`: 接口到危险接收点、跨文件数据流等代码联动关系。
- `data_flows`: 跨文件攻击路径。

响应中 `discussion` 包含安全、可靠性、性能、可维护性和主持 Agent 的同步讨论摘要、共识和行动项。

### 15.6 查询任务安全发现

`GET /api/security/findings?task_id=33`

等价于对任务执行安全复审,返回结构化安全发现、OWASP/CWE 标签和修复建议。

### 15.7 工作台安全态势汇总

`GET /api/security/dashboard-summary?days=30`

返回当前用户可见项目范围内的风险评分、严重度分布、OWASP 热点、覆盖率和趋势数据。

---

## 16. Agent 自进化 `/api/evolution` (v3.0, 管理员)

所有接口均需管理员权限。

### 16.1 反馈信号总览

`GET /api/evolution/feedback?window_days=90`

### 16.2 经验记忆库

`GET /api/evolution/experiences?limit=50`

### 16.3 黄金回归集

`GET /api/evolution/eval-cases`

### 16.4 触发一轮进化

`POST /api/evolution/run`

```json
{ "window_days": 90 }
```

### 16.5 提案列表与详情

- `GET /api/evolution/proposals?status=&limit=100`
- `GET /api/evolution/proposals/{proposal_id}`

### 16.6 提案评估、审批、驳回与回滚

- `POST /api/evolution/proposals/{proposal_id}/evaluate`
- `POST /api/evolution/proposals/{proposal_id}/approve`
- `POST /api/evolution/proposals/{proposal_id}/reject`
- `POST /api/evolution/proposals/{proposal_id}/rollback`

`reject` 和 `rollback` 请求体:

```json
{ "note": "原因说明" }
```

---

## 17. 操作审计 `/api/admin/audit` (v2.0, 管理员)

### 17.1 审计日志列表

`GET /api/admin/audit?action=&keyword=&actor_id=&start=&end=&page=1&page_size=20`

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "id": 1001,
        "actor_id": 1,
        "actor_name": "admin",
        "action": "login",
        "target_type": "user",
        "target_id": "1",
        "detail": "管理员登录",
        "status": "success",
        "ip": "127.0.0.1",
        "create_time": "2026-05-27T09:00:00+08:00"
      }
    ],
    "total": 230, "page": 1, "page_size": 20, "pages": 12
  }
}
```

---

## 18. 圆桌讨论审 `/api/discuss` + WebSocket (v2.3)

实时、对话式的多 Agent 讨论审查。先用 REST 预检注册会话,再用 WebSocket 建立双向实时连接。详见 `docs/圆桌讨论/圆桌讨论功能文档.md`。

### 18.1 预检并注册讨论

`GET /api/discuss/start?project_id=&file_id=&review_type=full`

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `project_id` | int | 是 | 项目 ID |
| `file_id` | int | 是 | 文件 ID(须属于该项目且 active) |
| `review_type` | string | 否 | 默认 `full`,决定参会 Agent 组合 |

```json
{
  "code": 0,
  "data": {
    "session_id": "disc_xxxxxxxxxx",
    "ws_url": "/api/ws/discuss/disc_xxxxxxxxxx",
    "file_name": "README.md",
    "language": "markdown",
    "review_type": "full",
    "agents": [
      { "code": "security", "name": "安全审查代理", "focus": "..." }
    ],
    "rules_count": 12
  }
}
```

> 预检只创建会话并暂存上下文,**讨论在 WebSocket 连接建立后才启动**;默认 2 轮。

### 18.2 讨论 WebSocket

`WS /api/ws/discuss/{session_id}?token=<JWT>`

- 鉴权:`token` 走 query string,服务端 `jwt.decode` 校验;缺失/非法以关闭码 `4001` 断开。
- 帧格式:JSON 文本帧。

**服务端 → 客户端**

| type | 关键字段 | 说明 |
| --- | --- | --- |
| `discuss` | `turn` | 一条发言(`turn_id/agent_code/agent_name/role/content/timestamp`) |
| `control` | `action`,`payload` | `round_start`/`speaker`/`paused`/`resumed`/`stopping`/`done`/`info` |
| `session_end` | — | 会话结束 |

**客户端 → 服务端**

| action | 字段 | 说明 |
| --- | --- | --- |
| `user_input` | `content` | 用户插话(广播为 `role:user` 发言,所有 Agent 可见) |
| `pause` / `resume` / `stop` | — | 暂停 / 恢复 / 终止 |
| `ping` | — | 心跳,服务端回 `{"type":"pong"}` |

> 注:WebSocket 不受 §1.3 统一响应结构约束,直接收发上述 JSON 帧。

### 18.3 讨论产物:审查报告

讨论结束(自然结束或终止)后,后端自动创建 `ReviewTask(review_type="discuss", status="success")` 并抽取结构化 `ReviewIssue`,因此:

- 出现在 `GET /api/reports`、`GET /api/review`(任务列表)与仪表盘统计中;
- 详情走 `GET /api/reports/{task_id}`(评分、严重度分布、问题清单、共识小结);
- 讨论期间每次 LLM 调用写入 `AiCallLog`,出现在 `GET /api/ai-logs` 并计入 Agent 中心统计;
- `done` 控制帧的 `payload.task_id` 即该报告的 task_id,前端据此提供「查看报告」入口。

---

## 19. OpenAPI / Swagger

FastAPI 默认端点：

- Swagger UI：`GET /docs`
- ReDoc：`GET /redoc`
- OpenAPI JSON：`GET /openapi.json`

当前代码只根据 `OPENAPI_ENABLED` 设置 `docs_url` 和 `redoc_url`，没有同步设置 `openapi_url`。因此生产 `OPENAPI_ENABLED=false` 时：

- `/docs`、`/redoc` 关闭；
- `/openapi.json` 仍由 FastAPI 生成；
- `frontend/nginx.conf` 还显式代理 `/openapi.json`。

目标方案应二选一：生产全部关闭，或通过内网/管理员鉴权访问。修复前，文档不得再描述为“生产 OpenAPI 已关闭”。

## 20. 速率限制

| 接口 | 限制 |
| --- | --- |
| `/api/auth/login` | 5 次 / 分钟 / IP |
| `/api/auth/register` | 3 次 / 分钟 / IP |
| `/api/review/start` | 5 次 / 分钟 / 用户 |
| 其他 | 600 次 / 分钟 / 用户(全局保护) |

超出返回 `429 42900`。

## 21. 健康检查

`GET /healthz`

```json
{ "status": "ok" }
```

当前代码未提供 `/readyz`;容器健康检查由 MySQL 容器自身 healthcheck 和后端 `/healthz` 共同承担。

## 22. Schema 速查(Pydantic 命名约定)

当前 `backend/app/schemas` 有 28 个业务模块；下列仅为早期核心模块示例，不是完整目录。实际字段和新增模块以源码与 OpenAPI 为准。

```
schemas/
  ├── auth.py       (RegisterIn, LoginIn, LoginOut, UserOut, ChangePasswordIn)
  ├── user.py       (UserOut, UserListItem, UserResetPasswordOut, RoleIn, StatusIn)
  ├── project.py    (ProjectIn, ProjectUpdateIn, ProjectOut, ProjectDetailOut)
  ├── code_file.py  (CodeFileIn, CodeFileUpdateIn, CodeFileOut, CodeFileDetailOut, RenameIn)
  ├── code_version.py (VersionOut, VersionDetailOut)
  ├── rule.py       (RuleIn, RuleUpdateIn, RuleOut, RuleToggleIn)
  ├── review.py     (ReviewStartIn, TaskOut, TaskDetailOut, ProgressOut)
  ├── issue.py      (IssueOut, IssueStatusIn, IssueBatchStatusIn)
  ├── report.py     (ReportOut, ReportItem)
  ├── dashboard.py  (SummaryOut, RiskItem, IssueTypeItem, ScoreTrendItem, FrequencyItem)
  ├── ai_log.py     (AiLogOut, AiLogDetailOut)
  ├── agent.py      (AgentProfileOut, AgentRuntimeOut, AgentRuntimeSummaryOut, AgentSituationOut)
  ├── ai_prompt.py  (AiPromptBundleOut, AiPromptIssueIn, AiPromptTaskIn, AiPromptProjectIn)
  ├── security.py   (SecurityScanOut, SecurityChecklistOut, SecurityDashboardSummaryOut)
  ├── evolution.py  (ExperienceOut, ProposalOut, EvalCaseOut, RunIn, RejectIn)
  └── audit.py      (AuditLogOut)
```

`In` 后缀为请求体,`Out` 后缀为响应体,所有字段类型严格。

## 23. 请求示例(curl)

```bash
# 登录
curl -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"alice123"}'

# 上传代码
curl -X POST http://localhost:8000/api/code-files/upload \
  -H 'Authorization: Bearer <token>' \
  -F 'project_id=7' \
  -F 'file=@./auth.py'

# 启动审查
curl -X POST http://localhost:8000/api/review/start \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{"project_id":7,"file_ids":[101,102,103]}'

# 下载报告 Word
curl -OJ -H 'Authorization: Bearer <token>' \
  http://localhost:8000/api/reports/33/export/word
```
