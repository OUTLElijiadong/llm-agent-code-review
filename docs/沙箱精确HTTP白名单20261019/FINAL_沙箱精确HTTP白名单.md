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

4.1.11 原始实现阶段的测试与镜像构建记录见 ACCEPTANCE。本轮修订代码的前端 27 项、后端 106 项针对性回归通过，前端生产构建与静态检查通过；五种修订版运行镜像尚未构建，生产部署尚未执行。

生产版本 `4.1.11`（commit `974ba0d68bd1ea3baa29bc50b6a7fdb383418070`）已部署。真实产品 UI 以普通用户 `outle` 创建并运行任务 `sbx_b12572a3c508401f9d305c16` 一次，执行器采用服务端强制的 4 条精确 HTTP 白名单，动态 AI 用例和模型审查按策略跳过，容器及任务目录确认回收。报告和结构化制品已生成。

本次真实黑盒最终状态为失败，不是通过：`GET /` 在 81 ms 内未收到 HTTP 状态行，0 字节；三条显式 PHP 路由分别返回 200。这个结果说明强制根路径导致了错误的整体失败；当前源代码已允许仅配置真实入口路径。4.1.11 生产任务的历史结果不会因此改写；修订版还没有生产验证。没有打开源码浏览器、未访问外部目标，响应正文未保存。该运行不证明靶场完整通过或漏洞审计完成。一次授权的生产运行已经使用，不自动重跑同一靶场。
