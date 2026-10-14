# 论坛单帖链接与实际排序控件精准修复

观察日期：2026-10-15（台北）。范围：候选工作树源码、本地隔离数据库、HTTP 与组件测试。

## 需求与复现

主任务给出的生产线索是：模型以 newest 查询能够得到最新帖子，但业务数据没有返回单帖权威详情页面路径，并误称界面已有“按最新浏览”控件。本子任务只修复服务端单帖路径与实际排序控件，不把该生产线索当作本子任务已经重跑的生产证据。

修改前已完整读取论坛服务、Schema、REST API、前端接口、列表组件和对应测试。现有后端支持 default/newest 且过滤非 normal 帖子；default 为置顶优先，再按创建时间与 ID 倒序，newest 不受置顶优先影响。前端原先只有关键词与分类选择，没有排序选择。

先新增回归并执行红灯：后端 `test_forum_ordering.py` 为 **7 failed / 2 passed**，其中六个独立服务场景缺 detail_route，一个响应 Schema 场景丢弃 detail_route；前端排序专项为 **4 failed / 34 因筛选跳过**，缺少实际排序控件。红日志：`evidence/forum-routes-red.txt`、`evidence/forum-sort-red.txt`。

## 最小修改与契约

- `forum_service._post_dict` 从数据库实际帖子 ID 生成 `detail_route=/forum/{id}`。列表、详情、现有编辑/置顶/浏览计数公开投影复用该入口；没有新增业务读取或写入端点。
- `PostListItemOut.detail_route` 为必填输出字段，`PostDetailOut` 继承；FastAPI 的列表和详情 response_model 不再滤掉这个字段。
- 前端 `ForumPost` 声明 detail_route，列表行 RouterLink 使用这个服务端字段；新增 `ForumPostSort` 对齐现有服务端枚举。
- 列表新增有明确“帖子排序”名称的实际选择控件：“置顶优先”映射 default，“最新发布”映射 newest；默认选中 default。排序变化复用 search，重置 page=1，保持当前关键词与分类，并把 sort 放入每次 GET 参数。
- 原有请求 generation、迟到结果/错误保护、重试、空状态、分页越界恢复与搜索清除逻辑沿用。排序选择复用分类控件的换行、最小宽度、44px 操作高度和小屏布局规则；本子任务没有真实浏览器截图或视口点击验收。

隐藏帖边界没有扩大：列表仍限定 status=normal；详情仍在生成数据前拒绝 deleted/hidden 资源。GET 详情没有改为写浏览次数，显式浏览 POST 的原行为保持。

## 验收数据

| 层级与样本 | 已确认结果 |
|---|---|
| default/newest 各三轮独立 SQLite 服务用例 | 四个公开帖与 deleted/hidden 两个不可见帖；两页合并顺序稳定、总数仅四个；每项链接由真实 ID 生成；所有不可见详情拒绝 |
| Pydantic 包装投影 | Resp[PageOut[PostListItemOut]] 与 Resp[PostDetailOut] 均保留 `/forum/44` 测试字段 |
| FastAPI/TestClient 三轮独立用例 | 普通用户依赖、隔离 DB；实际列表/详情 HTTP200 JSON 保留真实链接；已删除帖 HTTP404 且无链接；GET 后浏览数仍为0 |
| 非法排序 HTTP | 依旧由项目异常处理器返回 HTTP400；没有更改已有状态语义 |
| 真实组件排序控件三轮 | 选项文字和值正确；default/newest 切换均回第一页；关键词 trim、分类和翻页排序保持 |
| 排序期间迟到响应、失败重试 | 旧置顶结果不能覆盖最新结果；失败不混入旧帖；重试继续用原搜索/分类/newest/page1 |
| 前端接口 | 最新列表只发 GET，原样保留服务端单帖链接；既有 GET/显式浏览 POST 分离回归通过 |

最终命令和结果：

```bash
cd backend
.venv/bin/python -m pytest -o addopts='' tests/unit/services/test_forum_ordering.py tests/unit/services/test_community_service_coverage.py tests/unit/test_schema_field_coverage_r1_r8.py tests/unit/services/test_forum_context_compaction.py -q
cd ../frontend
npm test -- src/views/forum/ForumViews.test.ts src/api/forum.test.ts
npx vue-tsc --noEmit
npx eslint src/api/forum.ts src/api/forum.test.ts src/views/forum/ForumList.vue src/views/forum/ForumViews.test.ts
git diff --check
```

- 后端四个文件 **63 passed**，其中本次论坛精确排序/路径/HTTP 文件 **12 passed**。其余是既有社区、Schema 和论坛上下文压缩回归；不合并宣称全部论坛功能已验收。
- 前端两个文件 **44 passed**，包含排序新增六个用例及既有论坛列表/详情/编辑/接口回归。
- vue-tsc、定向 ESLint、diff whitespace 检查均 exit 0。

绿日志：`evidence/forum-routes-green.txt`、`evidence/forum-http-green.txt`、`evidence/forum-sort-green.txt`。检查日志：`evidence/forum-typecheck.txt`、`evidence/forum-eslint.txt`。

首轮后端绿灯中六例因 SQLite 时间读回丢弃 tzinfo 而失败；修正测试夹具为比较前后都从数据库刷新后的值，没有为此更改产品时间逻辑。HTTP 增样另通过真实 GET 后 view_count=0 验证读取不写浏览次数。

## 独立复核与未覆盖项

两次尝试触发既有只读子代理复核，工具均返回 `agent thread limit reached`。主代理已收到此限制和明确的论坛契约复核范围，由主代理或已有可运行代理完成独立复核。本报告没有把未完成的独立子代理验收写成通过。

源码未 commit。全后端集成与全前端构建交由主任务在最终文件集上执行，避免与其并行构建争写 dist。未修改 provider、site completion guard、Agent Responses 或其他代理所有的源码；没有登录生产、操作生产页面、部署、修改真实帖子或发起项目模型调用。

本地自动化确认的是本次路径输出、排序控件和相关边界。真实账号生产表现、移动视口点击、模型最终是否忠实使用 detail_route、整个论坛和全站均不属于本子任务已通过的范围。知识门禁沿用本任务主代理已提供的 ready 收据上下文；没有新增长期用户偏好。
