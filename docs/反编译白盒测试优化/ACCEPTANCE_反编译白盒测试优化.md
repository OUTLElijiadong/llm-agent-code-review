# ACCEPTANCE：反编译白盒测试优化

## 当前状态

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| Git 基线与工作树保护 | 通过 | `origin/main=4b698f2`；备份分支 `codex/pre-whitebox-backup-20260818164924`；未执行破坏性清理 |
| 输入识别与失败关闭 | 通过 | `backend/tests/unit/services/test_decompilation_service.py`；14 项通过 |
| 普通上传边界 | 通过 | APK/AAB 不进入普通 MIME 白名单；恶意扫描回归通过 |
| runner 证据协议 | 通过 | marker 单一性、字段类型、输入清单 SHA、逐制品原始 SHA、输出 SHA、退出码与制品引用测试通过；成功记录缺少任一原始制品 SHA 时失败关闭 |
| 反编译前置与派生源码门禁 | 通过 | runner 先执行反编译，再执行固定白盒链；派生 Java/Kotlin 源码进入 source-present 和静态完整性门禁 |
| 结构化报告证据 | 通过 | JSON 可解析；三套 HTML、PDF、Word 均显示工具版本、输入清单 SHA、原始制品 SHA、输出 SHA 和制品引用；PDF 已经 Poppler 提取及逐页 PNG 视觉检查 |
| 确定性报告兜底 | 通过 | 无 LLM 时仍生成事实门禁报告并发布 ReviewReport |
| 后端全量回归 | 通过 | `1890 passed, 1 PytestCollectionWarning`；未处理线程异常告警已提升为错误 |
| 前端回归与构建 | 通过 | Vitest `35 files / 232 tests`；`npm run build` 通过；测试存在既有 Vue warning |
| 部署脚本回归 | 通过 | `deploy/tests/test_scripts.sh` 通过；备份漂移、恢复回填、共享锁和 Playwright 保护均覆盖 |
| JADX Java 镜像构建 | 通过 | 生产固定 JADX CLI 1.5.6；官方 ZIP SHA-256 `545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974`；Java 镜像、profile digest 和 runner label 一致 |
| 真实 APK 沙箱执行 | 通过 | 官方 `small.apk` 原始 SHA-256 `3a47fa04968991670b5e417fa3b4daba32b5af59e764650f1a996be44b518bc1`；在 runsc、无网络、只读根文件系统、cap-drop 和资源限制下完成 JADX 及白盒完整性检查 |
| 生产备份与发布 | 通过 | 发布脚本生成并校验数据库备份，发布前既有隔离恢复记录为 84 张表、Alembic `036`；当前 release 证据目录留存备份大小/哈希、账本和健康证据，未另存本轮恢复过程日志。49 个未跟踪运维路径（41 个常规文件、8 个证书符号链接）已按原相对路径移到仓库外归档 `/opt/code-review-untracked-archive/20260819T014300+0800`；41 个常规文件 SHA-256 全部通过，8 个链接保留且无断链；服务器 `git status --short` 为空。Backend/Frontend 双容器 healthy，`deploy/.releases/current.env` 与 `/healthz` 均为 `d6536bd0a66f41d8558a421aa51ad17418154e48` |
| 公网基础验收 | 通过 | `https://www.lijiadong.cn/`、`/healthz`、`/readyz` 均为 200；浏览器登录页正常渲染且控制台无错误；最终 release 以权威账本和健康接口一致值为准 |
| 公网认证业务验收 | 部分通过 | 使用用户临时提供的账号在 `https://www.lijiadong.cn` 完成登录、项目 13“攻击小程序分析器”选择、项目安全审计弹窗扫描、真实任务创建与报告访问；新任务 #157（标准审查）页面显示成功、1/1 文件、评分 100、问题 0、25.2s；报告 #157 的“生成 JSON/HTML/PDF/Word、导出 Word/PDF”六个按钮逐一点击，浏览器应用错误日志均为 0。密码未写入代码、文档或日志；已点击退出登录，但页面仍停留在 dashboard，不能把会话清理状态记为已证实 |

## 证据边界

- 用户提供的 `jadx-gui.zip` 是 Windows GUI 包，不能直接作为 Linux 沙箱 CLI；生产使用官方 JADX CLI 1.5.6，Dockerfile 固定 SHA-256。
- APK/AAB 的动态 Android 运行不在现有 Java 沙箱能力内；报告必须区分反编译静态审计与动态部署未验证。
- 前端 lint 仍有既有无关文件错误，未因本任务扩大修改范围。
- `/opt/prism-release-state/current.env` 是遗留账本；当前发布脚本的唯一权威账本为 `/opt/code-review/deploy/.releases/current.env`。
- 报告与证据协议实现提交为 `0eb4c29`；最终生产提交还包含本组 6A 文档，精确完整 SHA 由发布账本记录。
- 项目安全审计弹窗是即时扫描证据：本次页面显示静态覆盖 `1/1`、8 个严重项和 6 条跨文件数据流，归因于 `1.py` 的动态执行/硬编码凭证链路；它不是任务 #157 的报告统计，也不能回写成任务 #143 的数据库问题数。
- 任务 #157 的公网报告页面证明了认证后的任务/报告业务链路，但浏览器工具不暴露 Blob 下载文件路径；因此本次没有声称已取得公网下载文件的本地字节或逐格式哈希。服务器侧 JSON、HTML、PDF、Word 解析与哈希证据仍以发布证据目录为准。
