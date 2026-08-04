# 棱镜小助·小菱 · 普通用户操作知识库(完整版)

> 这是你的操作手册。回答"怎么用/去哪点/出错怎么办/什么流程"时，优先引用本库并注明"按操作手册"，给出对应页面站内链接。事实操作以工具返回为准，不要编造。

## 一、工作台 /dashboard
看你的审查全景：项目/问题/风险分布/质量趋势/审查频率。
- 看板指标：汇总(dashboard.summary)、风险分布(risk_distribution)、问题类型(issue_types)、质量分趋势(score_trend)、审查频率(review_frequency)。
- 新用户第一站，点卡片可跳到对应页面。

## 二、项目 /projects
管理代码项目，是一切审查/测试/部署的入口。
- 新建项目：填名称+**主语言**(部署靠它选运行时，填错会导致部署失败)。
- 更新/删除项目；删除是高危(不可恢复)。
- 项目成员：添加/移除成员、改角色，共享项目给同事。
- 导入远程公开 HTTPS 源码归档(projects.import_remote)。
- 上传源码**两种方式，区别极大**：
  - 「上传代码文件」(走普通 CodeFile)：**可部署(deploy)**，推荐。
  - 「隔离审计归档」：把源码当可疑证据审计，**永远不能部署**，只能审计/测试。查它的状态用 audit_archive.status/result。

## 三、代码中心 /code
在线看、改项目代码。
- 浏览文件树、看文件内容/元数据。
- 在线新建/编辑/重命名/删除文件；每次修改自动生成新版本。
- 版本：查历史版本、回滚到指定版本(versions.restore，高危)。

## 四、审查 /reviews + /reviews/start
发起和查看代码审查。
- 发起：选项目+文件，选审查类型(快速/完整/安全等)→ 启动(reviews.start)。
- 查看：审查任务列表/详情/问题(reviews.list/get/issues)。
- 取消运行中的审查(reviews.cancel)；删除任务是高危。
- 流程：发起 → [审查记录](/reviews) 看进度 → 问题进 [问题](/issues)。

## 五、问题 /issues
跨任务集中管理审查发现的问题。
- 按严重度/类型/状态筛选(issues.list)。
- 看详情定位代码行(issues.get)。
- 更新处理状态(update_status)，支持批量(batch_update_status)。

## 六、报告 /reports + 模板 /report/templates
- 查/删报告(reports.list/get/delete)。
- 下载报告用固定工具 download_report(JSON/HTML/PDF/Word)。
- 模板：增删改查报告模板(report_templates.*)。

## 七、审查规则 /rules
自定义审查规则，让审查按你的团队规范走。
- 增删改规则、启用/停用(rules.toggle)。

## 八、安全中心 /security
安全专项审计。
- 安全清单(security.checklist)、安全态势汇总(dashboard)、已落库发现(findings)。
- 扫描：单文件(scan_file)、复审任务(scan_task)、**项目级白盒安全审计(scan_project)**、一键扫全部可审项目(scan_all_projects)。

## 九、Agent 中心 /agents + Agent 工坊 /agent-studio
- /agents：看 Agent 画像、运行态势、调用统计、Skill(agents.*)。
- /agent-studio：**创建你自己的 Agent 和 Skill**：
  1. 创建自定义 Agent(agent_studio.agents.create)。
  2. 建修订版、绑定 Skill(bind_skill)、测试(test)。
  3. 提交发布审批(submit)→ 管理员审 → 发布后可被调用。
  4. 也可创建 Skill(skills.create)复用。
- 调用已发布 Agent：先 search_published_agents 找，再 invoke_published_agent。

## 十、沙箱 /sandboxes(重点)
隔离环境里测试和部署项目。
- 测试(purpose=test)：白盒(静态检查+跑你的单测)、黑盒(起服务后外部发真实HTTP)、组合(先白后黑)。用固定工具 run_project_tests。
- 部署(purpose=deploy)：跑起来给预览链接，**自动跑 白盒→部署→黑盒/冒烟 测试链**。用 deploy_project_sandbox；关闭 close_sandbox、续期 extend_sandbox。
- 流程：选项目 → 选测试或部署 → 选语言/模式 → 提交 → 右侧看 Agent 实时进度。
- 部署就绪后点「打开预览」看真实应用；用完「关闭」释放资源。

## 十一、个人知识库 /knowledge
你的私人知识，**按你的账号严格隔离**，别人看不到。
- 沉淀：审查过的问题、报告、论坛/反馈历史自动沉淀；也可手动建文档(knowledge.docs.create)或让我"记住这个"。
- 检索(knowledge.search)、统计(knowledge.stats)、从平台数据同步(knowledge.sync)。
- 我回答时会检索它，越用越贴合你的风格与历史。

## 十二、论坛 /forum + /forum/new
- 看帖/发帖/回帖/删自己的帖(论坛是公开共享的)。
- forum.assist 用知识库辅助写帖。

## 十三、支持 /support
- 维修工单 /support/maintenance：提交/跟进/关闭工单(maintenance.*)。
- 意见反馈 /support/feedback：提交/查看反馈(feedback.*)。

## 十四、个人中心 /profile
- 个人资料/画像(profile.get/update)，重新学习画像(profile.relearn)。
- 个性化画像 /profile/personalization。
- 修改密码 /profile/password。
- API 配置 /profile/api-config：配置你自己的模型 API(api_config.get/delete)。

## 十五、常见报错怎么解决

### 「没有可用的隔离 worker，任务未运行」(50301)
沙箱是**单并发**：有一个「就绪」状态的部署占着 worker。
→ 去 [沙箱](/sandboxes) 把「就绪」的旧部署「关闭」，再重新发起。

### 白盒/黑盒「测试未通过」
多为项目自身问题被如实测出：
- Python `No module named 'tests'`：测试代码 `from tests.xxx import` 但 `tests` 包没一起传，把依赖包一并上传。
- Python 部署 `no supported Python deployment entry`：项目**根目录**要有 `app.py` 或 `main.py` 入口。
- PHP 用内建 `php -S`，无需入口文件。

### 部署报「隔离源码归档只能审计或测试，不得部署」(40341)
你用「隔离审计归档」传的源码，该方式**永不部署**。
→ 改用「上传代码文件」重新上传，再部署。

### LLM「503 Service is too busy」
大模型上游临时过载，平台已自动重试。稍等几秒重发；持续不行去 [意见反馈](/support/feedback)。

### 部署后想再测试却提示 worker 不可用
部署会一直占着单并发 worker 直到到期。先关闭该部署再测。

### 权限相关
- 「需要管理员权限」：该能力仅管理员开放，普通用户去对应用户页操作。
- 看不到某项目：项目是私有的，需项目所有者把你加为成员([项目](/projects)→成员)。

## 十六、固化流程(按此执行)

### 流程A：从零审查一个项目
1. [项目](/projects) 新建，填对主语言。
2. 用「上传代码文件」传源码(要部署就别用隔离归档)。
3. [发起审查](/reviews/start) 选类型启动。
4. [审查记录](/reviews) 看进度 → [问题](/issues) 处理 → [报告](/reports) 导出。

### 流程B：项目级安全审计
1. [安全中心](/security) → 项目级白盒审计(scan_project)。
2. 看安全发现(findings) → 处理问题。

### 流程C：部署并自动验证
1. 确认「上传代码文件」方式上传、根目录有入口(Python 要 app.py/main.py)。
2. [沙箱](/sandboxes) → 部署，提交。
3. 等自动链：白盒→部署→黑盒/冒烟，结论"自动测试链 白盒✓/黑盒✓/预览冒烟✓"。
4. 打开预览 → 用完「关闭」释放单并发。

### 流程D：创建并发布自定义 Agent
1. [Agent 工坊](/agent-studio) 创建 Agent → 建修订版 → 绑 Skill → 测试。
2. 提交发布审批 → 等管理员通过。
3. 发布后在对话里让我调用它。

### 流程E：沉淀个人知识
1. 你说"记住这个/沉淀一下"，或去 [个人知识库](/knowledge) 手动建/同步。
2. 之后提问我会优先引用你的个人沉淀。
