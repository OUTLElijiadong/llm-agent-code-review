# CNVD 与 CNNVD 后端冻结交付

## 最终结果

CNNVD 游客搜索已真实取得记录；CNVD AI 行业首页已真实取得记录，范围为人工智能行业首页最多 20 条，非全库，主库 HTTP 521 保留；ICS 无 token 业务拒绝，未接入。最后出口取数时间为 2026-10-22 00:56:11（Asia/Taipei）。限定回归 1228 passed；主线程同组 11 文件冻结复测 409 passed/0 failed；限定 Ruff 通过。当前服务 SHA-256 与出口实测版本一致。未 commit 或部署，生产应用发布后验收仍需主线程执行。

## 冻结写集

- `backend/app/services/vulnerability_source_service.py`：公开查询、CNNVD 分类及普通 Cookie 初始化、独立签名、CNVD 主站 521 时固定 AI 首页回退、范围及主站失败证据、缓存退避。
- `backend/app/schemas/vulnerability_source.py`：来源/证据/记录契约，明确行业范围、主库失败和来源日期。
- `backend/app/api/v1/security.py`：两条登录及 security:view 只读 API。
- `backend/app/schemas/security.py`、`backend/app/services/security_rule_catalog_service.py`：目录复用真实最近观察。
- `backend/tests/unit/services/test_vulnerability_source_service.py`、`backend/tests/unit/api/test_vulnerability_sources_api.py`、`backend/tests/test_complete_permission_matrix.py`：服务、HTTP 序列化和权限基线。
- `docs/CNVD与CNNVD后端接入20261021/`：方案、真实上游证据、历史失败及最终回归。

本轮仅追加修改 source service/schema、两项 source 单元测试和主题文档；其余为此前本任务已完成修改。其他代理的前端、discussion/team、DeepSeek、依赖和锁文件未编辑。冻结文件哈希和核验结果见 `证据/冻结清单.json`。

## 契约与初始化

新增配置 key 为零，无个人 Key、迁移或种子导入要求。启动后首次请求触发实际查询，无后台轮询；成功/有效空缓存 300 秒，错误至少 60 秒且尊重 Retry-After（最高 3600 秒），每 worker 独立。CNVD 主站错误退避独立于行业回退成功。

发布后以现有 security:view 令牌调用 GET /api/security/vulnerability-records?source=cnvd&limit=2 及 source=cnnvd。只在真实 records、有效 evidence、新时间戳和 cached=false 时验收取数；CNVD 行业结果还必须显示 coverage_scope=ai_sector、coverage_label、primary_status=blocked、primary_http_status=521，并将 source_date 与 published_at 分开。详情与核验脚本见 `生产核验.md`。

当前最终取数证据为 `证据/最终服务-行业回退生产出口真实取数.json`。主库可用性仍需正常官方通道或官方授权支持；不得把行业 connected 解释成 CNVD 全库或所有查询已接通。后端写集冻结交给主线程集成。
