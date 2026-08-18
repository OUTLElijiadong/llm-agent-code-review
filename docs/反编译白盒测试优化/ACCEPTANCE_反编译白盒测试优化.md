# ACCEPTANCE：反编译白盒测试优化

## 当前状态

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| Git 基线与工作树保护 | 通过 | `origin/main=4b698f2`；备份分支 `codex/pre-whitebox-backup-20260818164924`；未执行破坏性清理 |
| 输入识别与失败关闭 | 通过 | `backend/tests/unit/services/test_decompilation_service.py`；14 项通过 |
| 普通上传边界 | 通过 | APK/AAB 不进入普通 MIME 白名单；恶意扫描回归通过 |
| runner 证据协议 | 通过 | marker 单一性、字段类型、哈希、退出码与制品引用测试通过；runner shell 语法通过 |
| 反编译前置与派生源码门禁 | 通过 | runner 先执行反编译，再执行固定白盒链；派生 Java/Kotlin 源码进入 source-present 和静态完整性门禁 |
| 结构化报告证据 | 通过 | JSON/dict 上的 `evidence.decompilation` 回归通过；HTML/PDF/Word 复用同一上下文 |
| 确定性报告兜底 | 通过 | 无 LLM 时仍生成事实门禁报告并发布 ReviewReport |
| 后端全量回归 | 通过 | `1887 passed, 1 PytestCollectionWarning` |
| 前端回归与构建 | 通过 | Vitest `35 files / 232 tests`；`npm run build` 通过；测试存在既有 Vue warning |
| 部署脚本回归 | 通过 | `deploy/tests/test_scripts.sh` 通过；备份漂移、恢复回填、共享锁和 Playwright 保护均覆盖 |
| JADX Java 镜像构建 | 待公网发布 | 需在生产/构建主机执行固定版本 1.5.6 下载与 SHA 校验 |
| 公网完整验收 | 待公网发布 | 需确认发布后版本、登录、项目/源码、白盒、四格式导出和 `/healthz` |

## 证据边界

- 用户提供的 `jadx-gui.zip` 是 Windows GUI 包，不能直接作为 Linux 沙箱 CLI；生产使用官方 JADX CLI 1.5.6，Dockerfile 固定 SHA-256。
- APK/AAB 的动态 Android 运行不在现有 Java 沙箱能力内；报告必须区分反编译静态审计与动态部署未验证。
- 前端 lint 仍有既有无关文件错误，未因本任务扩大修改范围。
