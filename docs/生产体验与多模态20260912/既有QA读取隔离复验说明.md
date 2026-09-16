# 既有 QA 读取隔离复验说明

## 交付状态

2026-09-12 已完成脚本和离线验证，未执行生产 HTTP。脚本固定绑定 QA 107–110、项目 164/165、文件 1894/1895；不创建夹具，不调用旧 runner 的 `run()`、`fixture_loop()` 或 `negative_matrix()`。

脚本位于 `证据/既有QA读取隔离复验.py`；离线自检位于 `证据/既有QA读取隔离离线自检.py`。6 项离线自检通过，覆盖方法白名单、未知 GET 阻断、普通登录请求结构、资源泄漏失败、审查者错误写入标记失败、资源摘要变动失败。最终 Python 3.11 环境重新构建 325 路由计划并通过脚本 dry-run，输出 `requests_sent=0`。这不是生产读取验收通过的声明。

## 执行范围与副作用

执行后固定 91 个 HTTPS 请求：4 次登录、8 次自身角色/权限 GET、79 次资源 GET。其中 30 次资源正向读取、18 次跨项目 404、19 次无权限 403、12 次结束后摘要核对。任何状态、身份、20 项权限集合、成员集合、项目/文件数量、版本或摘要不符立即失败；不自动重放登录或任何请求。

- owner A 与 reviewer 108 可读取项目 A；owner B 可读取项目 B。每人项目列表必须精确只有一个本方项目。
- A 与 member A 不能读 B，B 不能读 A；覆盖项目详情、成员、文件列表、文件正文、元数据和版本列表，均预期 404。
- 无权限账号对两个真实项目的以上列表/详情，以及任务、问题、报告列表均预期 403。
- reviewer 的项目详情 `can_update/can_delete` 必须为 false，owner 必须为 true；成员角色与既有关系一致。这只证明读态能力标记。
- 源码、成员响应中的身份字段仅在内存比较；磁盘仅保留请求状态、字节数、摘要、脱敏 ID 和断言，不保存正文、密码、令牌、邮箱。
- 登录会修改最后登录信息、令牌版本及认证审计/限流状态，旧 QA JWT 会失效。不得与其他 QA 浏览器或矩阵同时登录。业务资源无写请求；不声称认证与 HTTP 日志零写入。

## 最终容器执行命令

以下仅供发布完成后由主执行者运行，当前未执行。先确认 `cr_backend` 的镜像、发布 SHA、健康检查符合本轮最终版本；脚本会动态导入 `/app/scripts/verify_permission_acceptance_https.py`，重建校验完整路由计划，同时比对 10 个已复核业务源码文件指纹。指纹不符需重审，不能绕过。

先将本地脚本上传到服务器的 `/root/prism-existing-readonly-20260912.py`。服务器下列命令使用已存在的 `/root/prism-acceptance-20260908/credentials.json`；不得替换成 9/7 旧账号文件。

```bash
set -euo pipefail
qa_read_dir=$(docker exec cr_backend mktemp -d /tmp/prism-existing-readonly-20260912.XXXXXX)
qa_result_dir=$(mktemp -d /root/prism-existing-readonly-result-20260912.XXXXXX)
trap 'docker cp "cr_backend:$qa_read_dir/result.json" "$qa_result_dir/result.json" 2>/dev/null || true; chmod 600 "$qa_result_dir/result.json" 2>/dev/null || true; docker exec cr_backend rm -f "$qa_read_dir/credentials.json"' EXIT
docker cp /root/prism-existing-readonly-20260912.py "cr_backend:$qa_read_dir/check.py"
docker cp /root/prism-acceptance-20260908/credentials.json "cr_backend:$qa_read_dir/credentials.json"
docker exec -u 0 cr_backend chown 10001:991 "$qa_read_dir/credentials.json"
docker exec -u 0 cr_backend chmod 600 "$qa_read_dir/credentials.json"
docker exec -w /app cr_backend python /app/scripts/verify_permission_acceptance_https.py --build-plan --plan "$qa_read_dir/plan.json"
docker exec -w /app cr_backend python "$qa_read_dir/check.py" --source-root /app --plan "$qa_read_dir/plan.json"
docker exec -w /app cr_backend python "$qa_read_dir/check.py" --source-root /app --plan "$qa_read_dir/plan.json" --credentials "$qa_read_dir/credentials.json" --base-url https://lijiadong.cn --output "$qa_read_dir/result.json" --execute
```

`result.json` 独占创建，遇到同名文件拒绝覆写。trap 无论通过或失败均尽力保存脱敏结果，只删除本次容器凭据副本，原始凭据及既有 QA 资源保留；输出目录权限由 mktemp 设为 700。脚本失败须读取脱敏断言定位，不能重复运行后覆盖失败证据。

## 审查者写入 403 的最小补验方案（未执行）

真实接口为 `PUT /api/projects/164`，没有 PATCH。依据 `app/api/v1/projects.py` 的 `update_project` 和 `app/schemas/project.py` 的 `ProjectUpdateIn`：`description` 为可选字符串，最多 500 字；`app/services/project_service.py:update_project` 先调用 `require_project_access(..., need_write=True)`，再赋值和提交。reviewer 应在业务角色门禁返回 403。

若主执行者决定使用明确的 QA 夹具补验：先 owner GET 项目 164，在内存保存原 `description`、`update_time` 和相关字段摘要，确认描述是字符串且不超过 500 字；再 member PUT `{"description": 原描述字符串}`，预期 403。无效 body 会在业务角色门禁前被 Pydantic 返回 422，因此不可把 422 当作写入 403 通过。

若意外 200，必须记录为安全失败，立即 owner GET 核验真实字段；如有变化，用 owner 正常 PUT 恢复原描述，再 GET 验证。即使提交相同描述，也应如实保留更新时间和审计变化；不得声称没有发生写操作。若原描述为 null，停止该方案：当前服务仅在非 None 时赋值，不能用 null 恢复被污染描述。以上不改权限、账号、文件版本或历史 task11/task35 状态。

代码保存即使提交相同正文也会新增版本，因此不纳入本次只读脚本和上述最小描述补验。owner 正向写、撤权/恢复、重登旧 JWT 专项、重启持久化、组织实体级隔离、全路由矩阵和真实页面按钮均保持独立验收项。
