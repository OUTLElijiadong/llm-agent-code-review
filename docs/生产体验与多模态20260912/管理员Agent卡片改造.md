# 管理员Agent卡片改造

根代理真实界面发现：管理员Agent目录35项使用宽表格，1280×900视口右侧指标挤出，长技能编码增加行高，7个现有分类被误标未知。本子仅修改 `/admin/agents` 对应的 `AgentGovernance → GovernanceWorkstation(mode=agents)` 视图，未改变配置、API、数据或其它模式的表格布局。

## 实现范围

- 将该分支9列表格替换为响应式卡片。名称、中文分类、职责摘要和启用配置状态常显。
- 优先级、审批阈值、记忆和知识四项保持原API字段和原数值，未重新缩放或推导；缺失值显示破折号，0仍为0。
- 内部编码、完整职责、授权边界及全部已登记能力默认折叠，点击原生按钮后挂载；按钮有展开状态与受控区域关联。长编码只在展开区按容器宽度换行。
- 卡片网格使用可收缩的最小列宽和`min-width:0`；数字区域独立四列，不需要横向表格滚动。真实浏览器最终尺寸复测仍由根代理完成。
- 复用共享CATEGORY_LABELS补7项：analytics→数据分析，analyzer→分析检测，custom_review→自定义审查，manager→协调管理，orchestrator→任务编排，output→报告输出，reviewer→代码审查。分类来源已对照backend对应Agent类及工坊发布代码，不把未知值猜成已知类型。
- 原有空值、异常类型、未知分类、原型键以及恶意标记的诚实回退保留；Vue按文本渲染。
- 保留原加载状态、防重复刷新、刷新失败保留上次成功结果；错误面板新增就地重新加载按钮。实测发现Element Plus Alert默认插槽会覆盖description，因此将保留旧结果说明直接放入插槽，避免按钮加入后说明被吞。

## 测试与文件

新增3项真实Element Plus挂载测试先全部失败，再修复通过：卡片主要信息与展开/收起、35项含7种真实分类及缺失计数、首次加载失败后就地重试。

现有插值回归仅将agents模式的表格定位迁移到卡片；其它模式仍验证真实表格单元格。联合既有调度测试及共享标签测试，共 **4文件46项通过**。ESLint与git diff --check通过。按根代理最新协调要求，本子未执行完整build或写入dist，避免与其它代理冲突；根代理将在所有文件冻结后统一完整测试与构建。

变更文件：

- `frontend/src/views/admin/GovernanceWorkstation.vue`
- `frontend/src/constants/adminGovernance.ts`
- `frontend/src/views/admin/GovernanceWorkstation.interpolation.test.ts`
- `frontend/src/views/admin/GovernanceWorkstation.agents.test.ts`（新增）

证据：`证据/管理Agent卡片-red.log`、`管理Agent卡片-green.log`、`管理Agent卡片-专项.log`、`管理Agent卡片-lint.log`。测试35条均为合成数据，不冒充已对当前生产35条逐项重新验收。

## 独立复核

dashboard子代理只读确认：四指标严格消费原字段，nullish回退保留0；后端前两项来自profile，后两项按agent_code计数，口径未变。7类中文映射全部找到真实后端声明；模板结构仅修改agents分支，CSS均为Agent专属类。共享映射会使其它位置遇到同枚举时显示中文，属于文字影响，没有改变其它模式结构。
