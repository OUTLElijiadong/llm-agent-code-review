# FINAL：反编译白盒测试优化

## 已交付

- 建立 APK/AAB/DEX/JAR/未知二进制的确定性识别与 JADX 决策协议。
- 在隔离 runner 前置执行固定 JADX CLI，增加超时、文件数/字节数上限、输入/输出哈希、退出码、日志与 manifest 引用。
- 将反编译结果接入白盒源码门禁；unsupported、工具失败、marker 缺失和日志缺失均失败关闭。
- 将 `evidence.decompilation` 接入 JSON、HTML、PDF、Word 共用报告上下文；LLM 不可用时生成确定性兜底报告。
- 加固备份归档漂移检测、恢复失败自动回填、共享维护锁和沙箱 Playwright 镜像保护。

## 验证摘要

- 后端：1887 passed，1 个既有 `PytestCollectionWarning`。
- 前端：35 个测试文件、232 个测试通过；生产构建通过。
- 部署脚本：`deploy/tests/test_scripts.sh` 通过。
- 公网发布与真实 APK/JADX 镜像执行在生产验收完成后补写版本、镜像摘要和报告制品 ID。

## 结论

代码与本地质量门禁已完成；生产结论必须以发布后容器 digest、健康接口、浏览器/API 业务验收和四格式制品解析证据为准。
