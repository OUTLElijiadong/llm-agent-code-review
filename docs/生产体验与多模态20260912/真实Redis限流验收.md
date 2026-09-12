# 真实Redis限流验收

日期：2026-09-12。根代理要求补齐完整后端门禁中5项需要真实Redis的跳过；本子完整读取 `backend/tests/unit/core/test_login_rate_limit_real_redis.py` 后执行原有测试，没有修改应用或测试代码。

## 环境与隔离

- 本地Docker context：desktop-linux，服务版本29.3.1。
- 复用本机已存在的官方镜像 `redis:6.2-alpine`，镜像短ID `46884be93652`。
- 专用容器：`prism-login-audit-20260912-fabeb254`，网络模式 `none`，Redis TCP端口关闭，只启用 `/tmp/redis.sock`。
- 只读根文件系统、redis普通用户、删除全部Linux capabilities、禁止新增权限、64MB内存限制；临时目录使用tmpfs，未挂载任何宿主机目录或生产卷。
- 测试使用新锁Python3.11.15环境 `/tmp/prism-0912-dependency-security/venv311`，只通过显式 `PRISM_LOGIN_TEST_REDIS_CONTAINER` 指定容器，不读取生产Redis地址或凭据。

## 原有5项测试全部通过

结果：**5 passed in 6.48s**，无跳过。

1. 达到失败上限后，同一窗口持续拒绝新尝试。
2. 剩余不足1秒时向上报告1秒，窗口到期后恢复。
3. 临近过期允许的尝试不会重开整个窗口。
4. 临近过期的失败结算不会重开窗口，倒计时仍报告1秒。
5. 兼容旧递增接口与check不会延长亚秒窗口。

每例通过真实 `redis-cli EVAL` 执行Lua，并使用唯一测试键；fixture结束删除自己的键。测试进程完成后通过EXIT清理专用容器，停止及移除均成功；再次按完整容器名查询，残留数量为0。没有接触生产Redis或其它容器。

证据位于同目录 `证据/真实Redis-创建.log`、`真实Redis-隔离核验.log`、`真实Redis-Lua验收.log`、`真实Redis-停止.log`、`真实Redis-清理核验.log`。

本结论补齐这5个真实Redis跳过项；不把一次隔离Lua测试等同于生产账号完整登录界面验收，后者由根代理继续完成。
