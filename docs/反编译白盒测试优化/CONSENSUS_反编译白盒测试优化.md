# CONSENSUS：反编译白盒测试优化

## 明确需求与验收标准

| 编号 | 验收标准 |
| --- | --- |
| AC-01 | Git 基线为远端 `origin/main`，受控工作树无改动，原有环境/备份被保留。 |
| AC-02 | 源码归档和 GitHub 源码稳定判定为 `skip`，不调用反编译工具。 |
| AC-03 | APK/AAB/DEX 仅在 magic/扩展名一致且通过恶意扫描后判定 `jadx`；JAR/未知二进制不得伪装成 Android 输入。 |
| AC-04 | 反编译执行只允许固定工具、固定参数、固定超时/输出上限；原始制品不写回。 |
| AC-05 | 成功结果包含原始 SHA、输出 SHA、工具版本、退出码、输出文件数和日志引用；失败状态包含非敏感原因并 fail-closed。 |
| AC-06 | 派生源码以新的不可变快照进入现有白盒测试；报告分开标明“原始制品”和“反编译派生源码”证据。 |
| AC-07 | JSON/HTML/PDF/Word 导出均可生成、下载、解析；无 LLM/工具时不把确定性失败升级为通过。 |
| AC-08 | 本地后端/前端/部署脚本回归通过；生产发布前有备份和回滚引用，公网健康、登录、上传、白盒、报告导出链路真实验收。 |

## 技术约束

- 采用固定的 `DecompilationDecision`/`DecompilationEvidence` 数据协议，不将模型自由文本作为执行命令。
- 默认工具为官方 JADX CLI Linux/cross-platform bundle；生产镜像必须固定版本和 SHA-256，不能使用用户 ZIP 内的 Windows EXE。
- 工具执行在专用受限 worker/container 中，网络关闭、只读原始输入、单独输出目录、资源和时间上限 fail-closed。
- 反编译输出只作为审计派生物；报告结论必须绑定输入/输出 SHA 和制品，不把模型推测当作运行证据。
- 既有沙箱支持的运行时为 Python/Node/Java/Go/PHP；反编译 Java/Kotlin 输出按 Java 白盒 profile 运行，Kotlin 仅作为源码证据并在无法编译时准确失败。

## 集成契约

1. `inspect_decompilation_input(filename, raw)` 只读返回输入类型与候选成员。
2. `choose_decompilation_tool(input_info)` 只从固定 allowlist 返回 `skip`、`jadx` 或 `unsupported`。
3. `run_decompilation(...)` 接受已验证的输入和固定 runner 配置，返回证据或结构化失败，不接受任意 argv。
4. `sandbox_service` 在白盒前追加 `decompilation` 事件和证据，派生源码必须拥有新 SHA。
5. 报告 exporter 读取 `conclusion.evidence.decompilation`，缺失或失败时显示阻塞状态。

## 边界与回滚

- 先发布后端/沙箱能力，前端只在 API 已稳定后接入显示；任一阶段失败立即停止，不执行数据库 downgrade。
- 生产发布使用现有 `deploy.sh`、备份、`ops-check.sh` 和应用镜像回滚流程；不覆盖旧 release 目录。
- 生产现场发现 release 账本/健康标识漂移时，先修正发布状态记录或回滚，不以容器 healthy 代替版本一致性证明。
