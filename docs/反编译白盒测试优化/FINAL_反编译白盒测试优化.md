# FINAL：反编译白盒测试优化

## 已交付

- 建立 APK/AAB/DEX/JAR/未知二进制的确定性识别与 JADX 决策协议。
- 在隔离 runner 前置执行固定 JADX CLI，增加超时、文件数/字节数上限、输入/输出哈希、退出码、日志与 manifest 引用。
- 将反编译结果接入白盒源码门禁；unsupported、工具失败、marker 缺失和日志缺失均失败关闭。
- 将 `evidence.decompilation` 接入 JSON、三套 HTML、PDF、Word 共用报告上下文；分别展示输入清单 SHA 和逐制品原始 SHA；LLM 不可用时生成确定性兜底报告。
- 加固备份归档漂移检测、恢复失败自动回填、共享维护锁和沙箱 Playwright 镜像保护。

## 验证摘要

- 后端：1890 passed，1 个既有 `PytestCollectionWarning`。
- 前端：35 个测试文件、232 个测试通过；生产构建通过。
- 部署脚本：`deploy/tests/test_scripts.sh` 通过。
- 报告：JSON、HTML、PDF、Word 均包含结构化反编译证据；PDF 可提取完整 SHA，逐页渲染后未发现文字重叠、裁切、乱码或不可见表头。
- 沙箱：官方 `small.apk` 原始 SHA-256 为 `3a47fa04968991670b5e417fa3b4daba32b5af59e764650f1a996be44b518bc1`，固定 JADX 1.5.6 在生产 runsc 隔离环境中完成反编译和白盒完整性检查。
- 生产：发布前备份可隔离恢复 84 张表，Alembic `036`；双容器 healthy；公网根页面、`/healthz`、`/readyz` 返回 200，登录页浏览器控制台无错误。
- 实现提交：`0eb4c29`；最终生产 release 的完整 SHA 由 `/opt/code-review/deploy/.releases/current.env` 与健康接口共同证明。

## 结论

代码、本地质量门禁、生产沙箱、报告解析和公网基础发布已完成。没有登录凭据，因此认证后的项目上传、真实审查任务和公网四格式下载没有执行；该边界不能用生产容器内导出测试替代。
