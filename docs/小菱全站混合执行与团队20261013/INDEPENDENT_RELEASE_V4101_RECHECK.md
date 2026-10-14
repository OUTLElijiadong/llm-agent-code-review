# v4.0.101 发布独立复核

- 核验时间：2026-10-14T09:48:45.763930+00:00（UTC；台北时间加8小时）。
- 实际版本与候选：`v4.0.101` / `1dd46c05db02a1794c459c6b4b0e688c03927fb1`。
- 结论：本次17项只读发布检查与三轮匿名HTTP共9样本全部通过。未登录账号、未操作GUI、未调用模型、未改变生产业务数据。

## 等待完整发布事务

检查前已读到当次准确完成行 `发布完成(target=all, sha=1dd46c05db02a1794c459c6b4b0e688c03927fb1, alembic=063_agent_browser_actions)`。检查器再次限定当次SHA/version/target预检后的最后尝试，并核对精确部署进程已经退出；未用切换期间health200代替完整发布。

## 实际核对范围

1. 当前发布树Git HEAD与当次SHA。
2. current ledger总SHA与BACKEND_RELEASE、FRONTEND_RELEASE、版本。
3. backend运行环境及默认`.env`的4个版本字段一致。
4. frontend/backend精确tag，且容器真实image_id匹配current ledger镜像摘要；实际Compose工作目录为候选发布树。
5. Alembic 063；healthz/readyz HTTPS状态200且版本/SHA正确；默认HTTPS证书验证未关闭。
6. 实际提供的buildInfo包含本次版本/SHA；Nginx配置测试及已部署模板与`frontend/nginx.conf.template`源文件匹配。
7. 发布bundle字节数与SHA256匹配预期；备份gzip可读、实际SHA256/sidecar/meta三者匹配。
8. 当次独立恢复回执为cr_testdb、105表、063，且与备份元数据一致；当次随机恢复schema查询为0，已清理。
9. 后端、前端、诱捕与ClamAV健康。
10. 匿名HTTP重复3轮：未知API404、错误方法405、browser pending无登录401；9样本均为JSON且Content-Type正确。

## 验收脚本自身复核与修正

原私有17项脚本缺少实际image_id/组件账本SHA/默认`.env`/当次精确完成和恢复成功回执的检查，仅看标签或删除恢复库不足。独立检查器补充这些条件，并在passed=false时非零退出；保留原脚本，不修改产品源码。

本地AST负例仅执行判定字典：正常17项通过；8个独立负例（组件SHA错误、image_id错误、默认环境错误、缺恢复成功、恢复表数错、恢复版本错、只有旧SHA完成行、部署仍运行）均拒绝。此证据是验收判定样本，不是对生产实施故障。

独立执行第一轮还发现原检查器模板路径为不存在的`deploy/nginx/default.conf.template`，脚本因此停止且没有产出通过JSON。按仓库Frontend Dockerfile实际COPY源纠正为`frontend/nginx.conf.template`后完整重跑成功。该错误属于验收工具路径，未误记成生产失败。

## 证据与边界

- `evidence/production/release-v4101-independent.json`：真实生产只读17项与匿名9样本。
- `evidence/independent/release-v4101-independent-strict.py`：本次实际使用的增强检查器。
- `evidence/independent/release-v4101-checker-negative-cases.json`：本地8个判定负例。
- `evidence/independent/release-v4101-checker-attempt1-template-path.err`：第一轮检查器错误，不是产品故障。
- `evidence/independent/release-v4101-independent-receipt.json`：文件bytes/SHA256收据。

这些检查证明本次发布身份、基础健康、备份恢复与匿名HTTP样本；不证明三角色功能、聊天隔离、浏览器点击、移动端、1百万token或长期稳定性。相关验收由主任务单独记录，Luna生产真实点击未执行。
