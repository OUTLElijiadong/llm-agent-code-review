# 支付宝沙箱隔离验收

已复现基线沙箱账单调用缺少环境参数的缺陷，新增服务层及 API 层拒绝用例。新增四个离线模拟用例检查沙箱交易操作只向固定网关发出签名请求，使用本地测试 RSA 密钥验证签名和响应。模拟不连接支付宝。

2026-11-17：支付网关、账单网关及账单 API、订单 API、通知 API 共 5 个测试文件 96 项通过；Ruff 与 git diff --check 通过。测试使用 mock 传输和内存数据库，不能证明真实渠道兼容或生产效果。

支付宝官方 Java v2 SDK [公共时间格式及时区常量](https://github.com/alipay/alipay-sdk-java-all/blob/master/v2/src/main/java/com/alipay/api/AlipayConstants.java) 固定 GMT+8，其 [请求签名组装](https://github.com/alipay/alipay-sdk-java-all/blob/master/v2/src/main/java/com/alipay/api/AbstractAlipayClient.java) 先按该时区格式化 timestamp 再签名。此前实现发出 UTC 时间；已用 UTC 及 UTC-5 的同一时刻复现 8 小时偏差，并调整为 GMT+8，覆盖跨日及签名。未真实请求支付宝。
真实沙箱下单、支付、通知、账单、退款及业务端到端尚未验收；未配置沙箱凭据，未发真实渠道请求。
