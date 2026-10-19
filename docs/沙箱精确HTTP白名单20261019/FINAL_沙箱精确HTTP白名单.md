# 沙箱精确 HTTP 白名单实施总结

## 交付结果

本次为 Prism 本地黑盒测试增加了服务端强制的精确 HTTP 请求白名单。用户在沙箱工作台配置 1–20 条只读 GET 规则；生产运行后发现根路径不应强制要求，当前源代码已改为至少一条且 `GET /` 可选。后端、Worker 和固定 runner 分别复核规则。runner 只访问容器自身的 loopback 应用，跳过归档内 AI 生成的启动补丁和动态脚本，不保存响应正文。后端将持久化计划与实际执行回执逐项比对，并在报告中列明请求证据和未覆盖范围。

白名单模式只证明列出的路径按预期 HTTP 状态响应，不等价于全量漏洞审计；报告将状态标为部分覆盖，明确跳过路由发现和 AI 动态用例。远程黑盒、白盒、组合测试和部署流程不接受该白名单字段。

## 实施文件

- `backend/app/schemas/agent_capability.py`
- `backend/app/services/sandbox_service.py`
- `deploy/prism_sandbox_executor.py`
- `deploy/sandbox/runner.sh`
- `deploy/sandbox/docker-compose.build.yml`
- `frontend/src/types/sandbox.ts`
- `frontend/src/views/sandbox/SandboxWorkstation.vue`
- 对应的 schema、service、Worker/runner 和前端回归测试

## 验收状态

4.1.11 原始实现阶段的测试与镜像构建记录见 ACCEPTANCE。本轮修订代码的前端 27 项、后端 106 项针对性回归通过，前端生产构建与静态检查通过。4.1.12 已按全量发布流程部署，生产 `/healthz` 和 `/readyz` 已核验版本与提交 SHA 正确。

生产版本 `4.1.11`（commit `974ba0d68bd1ea3baa29bc50b6a7fdb383418070`）曾用于先前失败任务。当前生产版本是 `4.1.12`（commit `80bac2cc56640d65477926f1820c2abbe90046ae`）。真实产品 UI 以普通用户 `outle` 创建并运行旧任务 `sbx_b12572a3c508401f9d305c16` 一次，执行器采用服务端强制的 4 条精确 HTTP 白名单，动态 AI 用例和模型审查按策略跳过，容器及任务目录确认回收。旧报告和结构化制品已生成。

先前真实黑盒最终状态为失败，不是通过：`GET /` 在 81 ms 内未收到 HTTP 状态行，0 字节；三条显式 PHP 路由分别返回 200。这个结果说明强制根路径导致了错误的整体失败；4.1.12 已允许仅配置真实入口路径。旧任务历史结果没有被改写。没有打开源码浏览器、未访问外部目标，响应正文未保存；该旧运行不证明靶场完整通过或漏洞审计完成。

4.1.12 生产当前只读巡检为 `degraded`、`can_continue=true`，唯一降级项是磁盘使用率 85% 达到告警阈值，需人工审阅。未运行任何磁盘清理。Safari 已配置好一次新的受限测试，但该次任务仍未提交，等待动作时确认；不得宣称新的靶场运行或报告已经产生。
