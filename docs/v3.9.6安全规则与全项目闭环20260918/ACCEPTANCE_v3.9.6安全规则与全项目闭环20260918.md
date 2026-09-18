# ACCEPTANCE_v3.9.6安全规则与全项目闭环20260918

## 验收状态

v3.9.6 已以不可变提交发布到生产并完成备份恢复、迁移、健康检查和真实模型回归；浏览器已确认登录页显示 `v3.9.6`，管理员后台二次登录因 Safari 密码库需要 macOS 解锁而未完成，不能把未登录页面写成后台功能通过。

## 功能验收矩阵

| 范围 | 验收证据 | 状态 |
| --- | --- | --- |
| 安全中心与审查规则 | `/security` 三标签；`/rules` 兼容跳到规则标签；菜单、搜索、页面引导同步 | 通过 |
| 报告模板重复页 | `/report/templates` 为唯一功能入口；`/admin/report-templates` 仅兼容重定向 | 通过 |
| 权威规则目录 | 提示规则、平台静态规则、敏感规则、CodeQL 能力统一只读目录；按 `rule:view` 裁剪可编辑规则 | 通过 |
| CodeQL 边界 | 仓库 CI 使用官方高级工作流与 `security-extended`；产品目录不宣称上传项目已执行 CodeQL | 通过 |
| DeepSeek 截断 | 画像输出预算传入真实调用；失败分片与失败文件继续后续项；生产任务 #178 完成 5/5 文件、33 条发现；8 次首轮截断均被重试恢复 | 通过真实生产运行 |
| 规则快照 | 新任务冻结完整内容和 SHA-256；合法空快照不回查实时规则；旧不完整任务兼容回查 | 通过 |
| 输入边界 | Schema 长度、治理枚举 fail-closed、模板沙箱、上传前置容量、目录逐文件流式上限 | 通过 |
| 角色范围 | 可分配角色仅用户、评审员、管理员；最高管理员唯一；评审员保留 Agent 自定义与渗透授权 | 生产核验：活动角色 user/reviewer/admin/super_admin，auditor disabled；唯一 super_admin=1 |

## 输入安全矩阵

| 类型 | 防线 | 自动化证据 | 边界 |
| --- | --- | --- | --- |
| SQL 注入 | SQLAlchemy 参数化与固定查询结构 | 全量服务/API 测试 | 未对每个普通文本框逐项发送攻击字典 |
| XSS/Markdown | Vue 文本绑定、DOMPurify、服务端字段长度 | 前端全量与 Schema 合同 | 富文本浏览器动态字典仍作为持续测试 |
| 模板注入 | Jinja `SandboxedEnvironment`、模板内容 256KiB | 报告导出回归 | 不允许任意 Python 对象访问 |
| 治理脏值 | effect/risk/permission 枚举及持久脏值 fail-closed | 策略引擎与工具网关测试 | 旧脏值被阻断，不自动猜测修复 |
| 单文件与归档 | Nginx/ASGI 21MiB 总请求，业务内容精确 20MiB | 413 与有界读取测试 | multipart 预留 1MiB 协议开销 |
| 文件夹上传 | Nginx 512MiB 流式转发；逐文件 20MiB；1000 文件；64KiB 后转临时文件 | 多文件、超限、关闭与暂存测试 | 总请求仍受 512MiB 硬上限 |
| 头像 | Nginx/ASGI 1MiB，总内容 512KiB，魔数与尺寸验证 | 头像 API 与中间件测试 | SVG 不允许 |
| 密码 | 输入 6-32，数据库 bcrypt 列 60 | Schema、迁移及生产前基线 | 不接受 255 位密码 |

## 本地质量门禁

- 后端最终全量：修复独立终审发现的 multipart 异常清理分支后，收集 4183 项，4178 项通过、5 项跳过、0 项失败（92.88 秒）。
- 前端全量：107 个文件，1191 项通过。
- 前端生产构建：3704 个模块通过，`vue-tsc` 与 Vite 均成功。
- ESLint：通过。
- Ruff：通过。
- Python 3.9 `py_compile`：通过。
- Alembic：唯一 head `053_role_pentest_authorization`。
- `git diff --check`：通过。
- 独立只读终审：发现的唯一发布阻断为 chunked 总量超限时临时文件未统一关闭；已修复并增加“先创建临时文件、再由上游中断流”的回归测试，相关 11 项测试与最终全量均通过。

## 生产验收

- 发布一致性：版本 `3.9.6`；生产 Git HEAD、前后端镜像 release、`/healthz` 与 `/readyz` 返回的 release 必须完全一致。精确 SHA 由发布完成后生成的 `deploy/.release/current_state` 和知识库闭环记录保存，避免在同一 Git 提交内写入无法成立的自引用 SHA。
- 数据库备份与恢复校验：通过；每次正式发布先生成带发布 SHA 的压缩备份并完成独立恢复校验（97 张表），Alembic=`053_role_pentest_authorization`。最终备份文件名及校验结果由发布状态文件和知识库闭环记录保存。
- `healthz` / `readyz`：同源 HTTPS 冒烟通过；backend/frontend/mysql/redis/clamav healthy，embedding 运行。
- QA 临时账号治理：`qa_claude_0825`（id=98）与 `xiaoling_accept_20260812_0547`（id=94）均事务软停用，token_version 分别 46→47、8→9；软停用时项目/任务/角色关联分别为 2/7/1、1/6/1，随后 id=98 新增真实验收任务 #178，因此最终重算为 2/8/1、1/6/1。
- 生产角色核验：活动 QA/测试前缀账号=0；活动 `super_admin`=1；评审员拥有 `agent_asset:create/update_own/test/submit` 与 `pentest:authorize`。
- 公网页面：Safari 刷新后登录页显示构建版本 `v3.9.6`；管理员页因系统密码库需要 macOS 解锁未二次登录，不能宣称后台截图验收通过。截图已存知识库附件。
- 真实 DeepSeek 项目级运行：任务 #178，`full`，5/5 文件完成，33 条发现；30 次 success、8 次首轮 failed（截断后重试成功），覆盖账本所有文件 `complete`，总 Token 278,036。

## 工具边界

Codex Security Deep Scan 因宿主缺少 managed filesystem permission profile 未能启动。不能把普通测试或独立代码复核写成该插件已完成；本轮使用全量测试、CodeQL 仓库 CI 配置和独立只读审查补充，但该工具环境问题继续列入 TODO。
