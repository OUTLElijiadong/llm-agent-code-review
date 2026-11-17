# 支付宝沙箱隔离验收

已复现基线沙箱账单调用缺少环境参数的缺陷，新增服务层及 API 层拒绝用例。新增四个离线模拟用例检查沙箱交易操作只向固定网关发出签名请求，使用本地测试 RSA 密钥验证签名和响应。模拟不连接支付宝。

2026-11-17：支付网关、账单网关及账单 API、订单 API、通知 API 共 5 个测试文件 96 项通过；Ruff 与 git diff --check 通过。测试使用 mock 传输和内存数据库，不能证明真实渠道兼容或生产效果。

支付宝官方 Java v2 SDK [公共时间格式及时区常量](https://github.com/alipay/alipay-sdk-java-all/blob/master/v2/src/main/java/com/alipay/api/AlipayConstants.java) 固定 GMT+8，其 [请求签名组装](https://github.com/alipay/alipay-sdk-java-all/blob/master/v2/src/main/java/com/alipay/api/AbstractAlipayClient.java) 先按该时区格式化 timestamp 再签名。此前实现发出 UTC 时间；已用 UTC 及 UTC-5 的同一时刻复现 8 小时偏差，并调整为 GMT+8，覆盖跨日及签名。未真实请求支付宝。

本地业务链测试运行独立 MySQL（容器数据目录为 tmpfs、端口仅绑定回环），执行已有的全新数据库迁移夹具；只在独立测试进程中注入销售条件，回环 HTTP 服务仅开放订单、查单及支付宝通知三条测试路由。真实业务函数完成下单幂等、主动查询到账、模拟 RSA2 支付通知及重放；网关调用是固定沙箱地址检查后的本地替身，不访问支付宝。容器隔离校验失败时在数据库建库前拒绝。运行 `uv run --offline --no-project --python 3.11 --with-requirements requirements-dev.lock python scripts/run_alipay_sandbox_local_test.py` 需要本地 Docker 及已有 mysql:8.0 镜像。真实沙箱交易和外部 HTTPS 回调未测试。
真实沙箱下单、支付、通知、账单、退款及业务端到端尚未验收；未配置沙箱凭据，未发真实渠道请求。
