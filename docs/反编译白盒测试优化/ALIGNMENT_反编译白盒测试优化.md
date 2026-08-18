# ALIGNMENT：反编译白盒测试优化

## 项目与当前状态

- 基线：远端 `origin/main`，当前 SHA `4b698f2ce2a07647a86fd5d67b26483ec58ecf55`。
- 生产只读核验：Frontend 容器 `4b698f2...`、Backend 容器 `1d248cc2...` 均 healthy；公网 `/healthz` 返回 `release=3ffbfe3...`，服务器 `/opt/prism-release-state/current.env` 仍为旧账本 `e693d211...`，发布状态存在漂移，发布新版本前必须修正并复核。
- 当前工作分支：`codex/whitebox-decompilation`；原工作分支已保存为 `codex/pre-whitebox-backup-20260818164924`。
- 用户提供的 `jadx-gui.zip` 是 Windows `jadx-gui-1.4.7.exe` 及捆绑 JRE，不是 Linux CLI，也不是待测 APK/JAR 制品。

## 原始需求

1. 先获取最新镜像并保持 Git 工作树干净。
2. 优化白盒测试内容；对上传文件或 GitHub 下载源码自动判断是否需要反编译。
3. 必要时使用子 Agent/工具反编译，再在隔离沙箱中部署运行白盒测试。
4. 导出报告完整、可解析、无未处理错误，并在公网真实部署后验收。

## 需求理解与范围

### 纳入范围

- 源代码归档和 GitHub 源码：跳过反编译，沿用现有不可变源码快照、固定 worker、白盒/黑盒、四角色报告链路。
- APK/AAB/DEX 等 Android 字节码：确定性识别后进入反编译前置阶段；记录原始 SHA、工具选择、工具版本、输出清单、输出 SHA、退出码和日志制品，再把派生 Java/Kotlin 源码交给既有白盒链路。
- 反编译失败、超时、输出为空、工具不可用或证据不完整：fail-closed，报告为阻塞/失败，不得伪造“白盒通过”。
- JSON/HTML/PDF/Word 既有导出接口：增加反编译证据段，保持现有格式和权限控制。
- 本地回归、生产备份后部署、`www.lijiadong.cn` 公网 API/浏览器验收。

### 明确不纳入

- 不运行用户提供的 Windows GUI EXE；不在宿主机直接执行上传制品。
- 不把普通 `.jar` 误判为 Android 输入；JAR 仅作为未知/不支持的二进制报告，除非后续明确批准并提供专用 Java 反编译器。
- 不修改原始上传文件，不连接生产数据库，不允许任意命令、任意镜像、任意挂载或任意网络。
- 不执行 `git clean -fdx`，不删除 `.env`、数据库、日志、虚拟环境、测试结果或备份。

## 关键澄清

- “最新镜像”本轮解释为 Git 远端 `origin/main`；Docker 生产镜像仍以发布前现场核验为准。
- 反编译工具选择由确定性输入识别和固定工具 allowlist 决定；Agent 只负责展示/编排，不拥有任意命令权限。
- “无报错”验收含义：测试进程无未处理异常，失败被准确降级，证据 SHA 可校验，四种报告可下载并被解析器打开。

## 现有系统复用点

- `project_source_service`：归档完整性、恶意扫描、不可变原包证据。
- `sandbox_service`：项目行锁、worker/profile/runsc、测试生命周期、白盒/黑盒事实门禁和多 Agent 报告。
- `deploy/prism_sandbox_executor.py` 与 `deploy/sandbox/runner.sh`：固定协议和隔离运行时。
- `report_exporter.py`、PDF/Word exporter、报告 API：四格式导出和 RBAC。

## 待验证事实

- 本轮未收到实际 APK/AAB/DEX 制品，因此先用最小确定性 fixture 验证选择器、清单和失败路径；真实反编译端到端需用户提供制品或 GitHub 下载地址。
- 生产当前 release 账本与容器/健康端点漂移，发布新代码前必须重新备份、部署、重读账本和健康标识。
