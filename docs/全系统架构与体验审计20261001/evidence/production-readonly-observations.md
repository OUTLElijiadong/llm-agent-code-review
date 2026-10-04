# 生产只读观察

观察时间：2026-10-01 06:38–06:46 UTC。以下为工具实际输出的摘录，不是完整生产验收。

## 服务基线

`https://lijiadong.cn/healthz`：`{"status":"ok","version":"4.0.35","release":"310ea1a8095428d670c0404a05b4dc643ad0027e"}`。

`https://lijiadong.cn/readyz`：同版本与 release，`status=ready`。`cr_frontend/cr_backend/cr_mysql/cr_clamav/cr_redis` healthy；embedding/testdb 在运行。`df -h /` 为 180G、已用约157G、剩余24G、87%；页面以 GiB 显示约156.22/179.94G。

## 证书状态执行器错误

对 `ToolCallLog` 只读查询最近6条 failed 记录：均为 `operations.certificate_status`，ID 为 `136020/136012/136004/135996/135988/135980`，时间分别为 `06:42:40/06:37:39/06:32:39/06:27:39/06:22:39/06:17:39` UTC。

错误前缀：

```text
命令失败 exit=1: Could not open file or uri for loading certificate from /opt/prism-releases/310ea1a8095428d670c0404a05b4dc643ad0027e/deploy/certbot/conf/live/lijiadong.cn/fullchain.pem
```

`stat` 证实该路径不存在。`docker inspect cr_frontend` 的实际 TLS 挂载是 `/opt/code-review/deploy/certbot/conf -> /etc/letsencrypt`。在宿主机对实际公开证书执行 `openssl x509 -noout -enddate` 得：

```text
notAfter=Dec 29 17:45:44 2026 GMT
```

`systemctl list-timers/list-unit-files` 实际单位名称为 `prism-cert-renew.timer`，enabled；上次 2026-10-01 03:21:01 CST，下次 2026-10-02 03:20:15 CST。最初查询不存在的 `certbot-renew.timer` 返回 inactive/not-found，不能据此认定续期未配置；已按实际单位纠正。

## 旧危急审批

审批 `234`：2026-09-11 19:24:07 创建，`pending`，`risk=critical`，`agent=manager`，`action=responses.admin_execute_capability`，标题“Responses Agent 请求执行 更新并应用全局 LLM 配置”。本轮只读确认仍待处理，没有审批/驳回，也没有完整追溯请求来源。

## 真实点击范围

普通 Safari 使用当前已登录管理员会话，打开运行与审计中心；点击管理员小菱，等待历史恢复；进入 Safari 响应式模式375×844。截图：

- `admin-operations.png`：当前管理员运行总览。
- `admin-xiaoling-restored.png`：历史恢复后的小菱面板。
- `admin-xiaoling-375.png`：375px响应式布局。

历史消息仅证明界面显示了服务器恢复的消息，不证明这些旧结论正确，也不代表本轮再次执行了模型测试。未发送聊天、创建团队、发布Agent或提交审批。系统既有自动监控继续运行；未手动修改生产业务对象。
