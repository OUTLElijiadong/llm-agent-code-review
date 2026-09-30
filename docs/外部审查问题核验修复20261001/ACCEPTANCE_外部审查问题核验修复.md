# 验收：外部审查问题核验与修复

## R2–R10 逐项结论

| 编号 | 核验结论 | 候选修复/复测证据 | 边界 |
|---|---|---|---|
| R2-01 | 报告说全站项目无法删除，与当前候选不符。列表卡片和表格均有编辑/删除入口；编辑/删除点击不会冒泡跳详情。 | `ProjectList.test.ts` 对 card/table 各跑一次编辑及确认删除，确认 API 参数和路由状态。 | 项目详情页内没有删除按钮，但项目列表可删；未对生产账号真点删除，未删除生产数据。卡片内交互语义仍可单独做可访问性评估。 |
| R2-02 | 已修复候选注册字段用语不一致。 | `Register.test.ts` 确认不再显示“账号 / 学号”。 | 生产未部署。 |
| R2-03 | 已修复候选注册页暴露供应商状态和旧版本常量。 | `Register.test.ts` 检查页面不含 “DeepSeek V4 在线” 和 “v1.0 · 2026”；版本构建信息改由构建注入。 | 生产未部署。 |
| R2-04 | 候选列表与详情语言缺失统一为“未设置”。 | `ProjectList.vue` 与 `ProjectDetail.vue` 使用相同缺省文案。 | 生产未部署。 |
| R2-05 | 已修复候选未审查项目把无评分显示为 0。 | 项目表格和卡片只对有效评分画分数，否则显示“未评分”；ProjectList 回归覆盖。 | 生产未部署。 |
| R2-06 | 报告已标为确认项；候选保留权限隐藏、面包屑和讨论角标修复。 | 相关 AppSidebar、任务页与路由组件测试包含对应文案/入口合同。 | 自动化不等于生产全角色真实点击。 |
| R3-01 | 已确认输入数字用户 ID 不适合实际操作，候选改为用户名/邮箱搜索选择。 | 后端新增受项目权限保护的候选搜索 API；项目成员服务、接口、权限矩阵与前端交互测试覆盖权限、空结果和搜索竞态。 | 生产 API/UI 未发布复测。 |
| R3-02 | 不是已证明漏洞。项目角色仅“审查员/负责人”是当前业务模型；只读分享需明确权限语义再设计。 | 未添加角色或接口。 | 不把产品功能建议记作安全缺陷。 |
| R3-03 | 候选服务层对用户不存在、成员/项目不存在等返回具体领域错误；未复现“所有失败均参数校验失败”。 | `project_member_service.py` 与新增候选查询 API 用例覆盖已知领域错误。 | 当前数据库条件未做线上枚举/泄漏测试；防止用户枚举的错误仍合并提示是有意的。 |
| R3-04 | 报告已确认前端 admin 守卫与 super_admin schema 限制正常。 | 既有权限路由矩阵与 Pydantic 请求 schema 回归保留。 | 本轮未复测生产管理页面。 |
| R3-05 | 报告已确认管理员菜单已使用统一下拉。 | AdminLayout 退出菜单行为与一致性测试保留。 | 本轮未复测生产管理页面。 |
| R4-01 | 已确认弱密码可以绕过；候选服务层、schema 与小菱改密工具统一加策略。 | 密码策略限定 15–64 个 Unicode 字符，结合强度、常见弱密码、账号匹配及 bcrypt 72 字节边界；注册/改密/Agent 工具均有回归。 | 生产弱密码帐号与线上改密流程未触碰。算式验证码不是密码策略替代。 |
| R4-02 | 报告场景已修复候选：明确反馈后清除旧会话并去登录页。 | ChangePassword 组件测试覆盖成功后的登录态清理和路由行为。 | 生产未复测。 |
| R4-03 | 部分成立。候选对下次受保护请求的单设备失效码显示专用说明；闲置标签页没有服务器推送时不能即时获知。 | `test_single_device_session.py` 与相关前端路由/提示回归。 | 未实现轮询/推送，不宣称所有空闲页面即时通知。 |
| R4-04 | 报告已确认两次输入不一致由客户端校验，接口只收最终密码符合职责。 | 注册与改密组件校验测试。 | 本轮不改。 |
| R5-01 | 已确认默认问题状态筛选未显式呈现会误导；候选筛选器展示实际状态并明确列表口径。 | IssueHub 恢复/筛选组件测试覆盖选项状态和结果更新。 | 未与生产数据库计数逐项核对。 |
| R5-02 | 报告已确认计数关系正确；保留候选口径提示。 | 既有仪表盘与项目详情测试。 | 不外推到生产全量统计。 |
| R6-01 | 已修复候选移动端抽屉默认遮挡问题，打开时提供遮罩/关闭行为。 | AppSidebar 测试验证窄视口折叠、遮罩与交互状态；全前端测试通过。 | 不是 iOS/Android 真机验收。 |
| R6-02 | 已补齐功能导航页面入口。 | 导航映射测试检查问题追踪、沙箱、个人中心可定位。 | 生产键盘真实点击未做。 |
| R6-03 | 原数字是 Agent 工具调用，不代表项目审查次数；问题属于标签和统计语义不明。 | 列表与详情将指标改称“Agent 工具调用”，加入范围提示和回归。 | 没有把工具调用次数改写为审查次数；未核对生产 30+ 记录。 |
| R6-04 | 报告已确认 ⌘K 文案已统一。 | 当前映射与文案测试保留。 | 本轮不改。 |
| R7-01 | 这是包大小优化建议，未证明为功能错误或性能回归。 | 候选生产构建成功；核心 vendor/Element Plus chunk 仍较大且 Monaco/语言 worker 为延迟资源。 | 本轮未做真实用户性能基准，不记为漏洞或“性能已优化”。 |
| R7-02 | 报告已确认仪表盘加载态与彩虹球问题未复现。 | 全前端回归通过。 | 不外推至所有设备/网络。 |
| R8-01 | 算式验证码可能被自动求解；未发现答案字段泄漏证据。验证码与注册接口的请求频率限制只限制频次，不能阻止自动解题。 | 未把频率限制当作 CAPTCHA 绕过缓解；没有新增第三方验证。 | 未测试自动解题规模，也未独立确认生产是否强制邀请码；不宣称抗机器人。 |
| R8-02 | 报告已确认内测码一次性展示、掩码、撤销确认与敏感值日志保护。 | 相关服务与 API 测试保留。 | 未在本轮生产创建/撤销内测码。 |
| R8-03 | 与 R4-01 同源，候选密码策略已修复。 | 共享密码策略服务和调用链测试。 | 生产未发布。 |
| R9-01 | 已修复候选待办卡片不可点击及标签混淆。 | Dashboard 组件测试确认带当前账号返回的会话 ID 唤起小菱，使用“小菱会话”类型。 | 生产真实会话恢复尚未复测。 |
| R9-02 | 已修复面向用户文案中的部分框架黑话。 | Agent 面板测试检查渲染内容不含 MetaGPT/Environment 等词。 | 代码注释、API 类型名等内部标识保留，不属于用户可见文案。 |
| R9-03 | 已补充历史问题保留审查所用 OWASP 版本说明，并统一当前目录为 OWASP 2025。 | 安全态势卡片及 OWASP 知识测试。 | 不据此断言已更新所有历史漏洞的标签。 |
| R9-04 | 报告已确认会话切换和官方评级免责声明工作正常。 | 当前会话抽屉与评分页面保留相关回归。 | 没有生产模型调用验收。 |
| R10-01 | 已确认关闭引导状态按账号和界面范围持久化，不是每个页面单独记。 | `ProactivePageGuide.test.ts` 与 localStorage key 测试覆盖账号/surface。 | 各角色生产浏览器偏好未复核。 |
| R10-02 | 报告已确认 404 页与空态有行动引导。 | 前端导航/空态组件回归。 | 404 资源响应仍可合法表示资源不存在；不承诺全系统不会出现 404。 |

## 附录：报告中的第一轮遗留项

第三方合订本复述 R1-01 至 R1-15。候选修正和逐项状态见 [2026-09-29 完整体验问题验收表](../完整体验问题修复20260929/ACCEPTANCE_完整体验问题修复.md)；本轮前端全套自动化通过，但不等同生产逐项真实点击。R1-01 危急全局 LLM 配置审批单及遗留生产审批动作没有通过浏览器/API批准、驳回或删除；R1-02 磁盘水位、R1-03 当日 Token 与主机增长率未读取当前监控，报告旧数值不作为现状。

## 新增公网/主机发现

| 发现 | 重复观察与当前结论 | 候选修复 | 未完成项 |
|---|---|---|---|
| TCP/8888 公网开放 | 主机只读 `ss -lntp` 显示 `BT-Panel` 进程绑定 `*:8888`；三次匿名公网根路径请求返回 `HTTP/1.1 404 Not Found`、`Server: nginx`。根路径响应不能证明管理路径不可达。 | 用户明确选择“暂不改安全组”；未停用 BT-Panel，也未动主机防火墙。Prism Compose 的 80/443 映射与此主机服务无关。 | 8888 仍对公网开放，管理员路径/访问控制未测试；之后若要收紧，需另行调整安全组并复测。 |
| CSP Report-Only | 生产主站 HEAD 返回 HTTP 200，响应仍含 `Content-Security-Policy-Report-Only`、`script-src 'unsafe-inline'` 和裸 `wss:`。`docker exec cr_frontend nginx -T` 确认运行配置相同。 | 候选 Nginx 模板改为强制 `Content-Security-Policy`、`script-src 'self'`、`script-src-attr 'none'`；移除 HTML inline `onload`，改用同源 defer 脚本。`connect-src` 固定为构建域名和别名 `wss://lijiadong.cn wss://www.lijiadong.cn`，不再反射请求 Host。候选保留 `style-src 'unsafe-inline'`，因 Vue 动态样式仍依赖 style 属性。 | 候选配置已在生产当前 Nginx 1.27.5 镜像中、以只读挂载和独立容器运行 `nginx -t` 成功，并用 `nginx -T` 核实 envsubst 后的三个 CSP 头均含精确 WebSocket 域名。该操作未重启或修改生产 Frontend。生产强制 CSP 的页面兼容仍需发布后正常浏览器验收。当前生产 compose 文件为 `/opt/prism-releases/62b6afbdc891ab915658ec2ca3382db2403bf82f/deploy/docker-compose.yml`；前端容器没有 Nginx 配置 bind mount，实际配置来自运行镜像。候选未发布。移除唯一 inline event handler 后没有 inline script block；后续若加入应使用每响应 nonce 或 hash。 |
| 证书有效期与续期 | 三次 TLS 握手证书 `notAfter=Oct 28 08:13:26 2026 GMT`；普通 HTTPS 校验成功。systemd、root crontab、`/etc/cron.*` 与 BT-Panel `crontab` 表均无续期任务。生产配置下全量 Certbot ACME staging `--dry-run` 对 `lijiadong.cn`、`www.lijiadong.cn`、`radar.lijiadong.cn` 全部通过。 | 候选加入每日 `prism-cert-renew.timer`、服务模板与安装器集成；`renew-cert.sh` 在 reload 与 restart 均失败时返回非零，供 systemd 发现故障。 | 候选 timer 尚未部署到生产。安装后须核对 timer enabled/next run 并实际运行续期服务；实际证书尚未轮换，未来自动续期仍需监控。 |
| Nginx 版本 | HTTPS HEAD 暴露 `nginx/1.27.5`；生产配置来自运行镜像，未挂载 Nginx 配置文件。 | 候选主配置和 TLS server 加 `server_tokens off`，部署合同测试检查配置。 | 候选未发布；发布后需确认实际响应不再泄露版本号。 |
| SSH 版本与认证 | 本轮生产 SSH 只读复核后，将发行版提供的 OpenSSH RPM 更新到 `openssh-server-9.3p2-16.oc9.x86_64`，同时升级 openssh 与 openssh-clients。RPM changelog 列出修复 CVE-2026-35414、CVE-2026-35385、CVE-2025-26465，并注明 CVE-2026-35386 已由既有回补覆盖。更新后 `sshd -t` 成功、`sshd` active、BatchMode 公钥 SSH 新连接成功；`sshd -T` 仍是 root 仅密钥登录、`pubkeyauthentication yes`、`passwordauthentication no`、`kbdinteractiveauthentication no`。公网三次仍显示协议标识 `SSH-2.0-OpenSSH_9.3`。 | 采用 OpenCloudOS 官方 RPM 安全回补，不自行编译替换发行版 sshd，也不伪造 SSH 协议 banner。 | banner 仍暴露上游基线 `9.3`；版本字串不是补丁状态。此次只确认该发行版 RPM changelog 与本机包状态，不据此声称所有 OpenSSH 漏洞均已修复；`dnf check-update --security openssh-server` 当前无待更新安全包。 |

## 自动化验证

- Frontend Vitest：129 文件、1483 项通过；ESLint 通过；`npm run build` 的 vue-tsc 与 Vite 构建通过。
- 部署脚本测试：19 项发布绑定案例通过，完整部署/运维 shell 测试通过。
- 后端全量首轮：5491 通过、5 跳过、5 失败。5 个失败已定位并同步合同：新增路由计数基线、旧弱密码工具契约样例、Nginx 测试仍期待 Report-Only。定向复测 154 项通过后，全量复测为 **5496 passed, 5 skipped, 5 warnings**（146.97 秒）。
- 独立上下文回归：预算门限与 ReviewAgent/输入容量定向用例 34 项通过，Agent Mesh dispatcher 29 项通过；长规则拆分和失败覆盖标记有 mock 回归，未调用真实模型。
- 合成百万级上下文：`test_semantic_compaction_handles_more_than_one_million_estimated_tokens` 与 `test_chat_compacts_more_than_one_million_estimated_tokens_with_ordered_constraints` 通过；使用合成 transcript、估算 token 与 mock compactor/transport，检查头/中/尾约束和来源锚点。它们不证明真实供应商的 token 计量、模型语义理解或生产无截断。真实 provider 的 1M-token 端到端验收仍未完成。
- 生产运行中的 Nginx `nginx -t` 已通过；候选模板也在当前生产 Nginx 镜像和 backend 网络命名空间做了只读语法检查，并核对渲染后的域名 CSP。候选强制 CSP 的业务浏览器兼容仍待发布后真实浏览器复测。
- 所有现存证书的 ACME staging dry-run 均通过；只验证可通过 staging challenge，不代表生产证书已续期或 timer 已安装。

## 生产证据边界

公网 8888、安全组、TLS 响应与当前 HTTPS CSP 核验为只读；用户明确选择暂不修改安全组。生产 SSH RPM 已从 `9.3p2-15.oc9` 升级到 `9.3p2-16.oc9`，未更改 sshd 配置；更新后重新验证公钥连接、服务状态与认证参数。未升级或重启生产 Nginx、未改 BT-Panel。候选 4.0.34 尚未发布；代码修复只代表候选工作树，生产 CSP、Nginx 版本和 UI/审批行为仍须正式发布后验收。证书续期 timer 与正式续期尚未执行。
