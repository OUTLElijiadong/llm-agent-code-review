# CNVD 与 CNNVD 接续事项

1. 主线程按生产核验文档执行发布后的真实 API 验收：CNNVD 游客搜索和 CNVD AI 行业首页要求 available/connected=true、新观察 cached=false、真实 records 与证据哈希。此次完成的是生产出口执行本地最终服务验证，生产应用尚未发布验证。
2. 前端显示后端 coverage_label/status_message；CNVD 行业成功与主库 521 必须同时可见，不写死 not_configured，也不显示全库已接通。empty 表示本次零命中，connected=false；来源日期不改称发布日期。Luna 已收到契约与真实证据。
3. CNVD 主库仍 HTTP 521；若需要全库列表/详情或更广覆盖，向官方确认正常公开通道、数据服务授权或出口访问条件。当前 AI 首页仅行业局部；不执行反爬挑战。ICS 公开读接口无 token 拒绝，未使用 JS token，不当作接通。
4. CNNVD 正式 XML API/完整数据包如有需求，走官方单位认证/登录申请；当前无该配置，游客搜索已正常取数，无需个人 API Key。
5. 当前源码冻结后的主线程同组复测为409 passed，限定后端及权限为1228 passed，Ruff通过。此前8项失败原日志已保存，不忽略、不以删除测试处理。
6. 独立交叉复核、知识库 validate/postflight、前端全量、生产 API 与浏览器验收由主线程统一闭环。子任务不 commit/deploy，不改生产配置、应用或数据。
