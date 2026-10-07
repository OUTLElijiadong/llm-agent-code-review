# v4.0.81

修正 Python 白盒 AI 用例 grounding 对标准库调用结果属性的误拒绝。现在只有经过真实导入绑定、且未被项目导入或赋值遮蔽的 `inspect.signature(...)`、`importlib.util.spec_from_file_location(...)` 等受限调用链，才会把其 AST 调用结果识别为标准库返回类型；其 `.parameters`、参数项 `.annotation`、模块 spec 的 loader 链可按既有精确白名单核验。

同时对静态可识别的模块属性写入/删除、动态命名空间修改、`getattr`/`setattr` 别名、直接 helper 参数传递及安全 builtin 同名遮蔽采取 fail-closed。没有把 `parameters`、`annotation`、`loader` 等名称加入通用放行名单；项目模块伪造同名 API、未导入标准库、导入别名被覆盖时仍拒绝。

本地候选验收：沙箱上下文定向 110 passed、2 warnings；后端全量 6556 passed、6 skipped、5 warnings（250.84 秒）；Ruff、compileall、diff-check 通过。提交 `b5c81173aed6784e8a3172cf9b212d550f02b1c0` 的 Business CI [37654707054](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/37654707054) 与 CodeQL [37654707139](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/37654707139) 全绿；CI 后端 6562 passed、5 warnings、0 skipped。bundle 校验值见证据/70。

这仍是候选，不是生产发布：生产当前 v4.0.80；SSH 公钥认证被拒，生产 Worker 和真实 Safari 复测尚未完成。
