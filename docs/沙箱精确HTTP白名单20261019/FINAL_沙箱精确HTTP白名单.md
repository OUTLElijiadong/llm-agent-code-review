# 沙箱精确 HTTP 白名单实施总结

## 交付结果

本次为 Prism 本地黑盒测试增加了服务端强制的精确 HTTP 请求白名单。用户在沙箱工作台配置 1–20 条只读 GET 规则，必须包含 `GET /`；后端、Worker 和固定 runner 分别复核。runner 只访问容器自身的 loopback 应用，跳过归档内 AI 生成的启动补丁和动态脚本，不保存响应正文。后端将持久化计划与实际执行回执逐项比对，并在报告中列明请求证据和未覆盖范围。

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

最新前端测试 27 项、schema 测试 15 项、报告与 Worker 测试 178 项通过；前端生产构建、Python 编译、runner shell 语法和 diff 检查通过。五种本机 `linux/arm64` 镜像构建成功，镜像内 runner 摘要一致。固定 PHP LFI 提交已通过禁网容器中的四条精确 GET smoke，四条 HTTP 状态均为 200，多层编码穿越目标在应用启动前被 runner 拒绝。

以上证明的是隔离容器内固定 runner 与有限 HTTP 白名单工作正常。靶场尚未由 Prism UI 导入并提交为平台任务，也没有数据库持久化报告、账号权限、刷新恢复或生产部署证据；本机 Docker 默认 runtime 也不代表生产 `runsc` runtime。不得将本地 runner 烟测表述为完整产品或生产验收。后续门禁见 TODO 文档。
