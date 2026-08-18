# TODO：反编译白盒测试优化

| 待办 | 当前原因 | 完成条件 |
| --- | --- | --- |
| 生产构建 Java/JADX 镜像 | 需要生产 Docker/runsc 与固定 base image digest | Java 镜像 label、runner SHA、JADX SHA 三者一致 |
| 公网发布 | 当前生产仍是前端 `4b698f2`、后端 `1d248cc2` | 发布后 `/healthz` release、容器 digest、release ledger 一致 |
| 真实 Android 制品验收 | 本地无可执行 runsc/JADX 环境 | 在生产沙箱用 APK/AAB/DEX fixture 产出成功/失败报告 |
| 四格式公网导出验收 | 需登录用户和已发布 sandbox report | JSON 可解析、HTML/PDF/Word 可下载并包含 `evidence.decompilation` |
| 前端 lint 基线 | 既有 6 个无关 `no-unused` 错误 | 独立清理这些文件后再把 lint 设为零错误门禁 |

## 生产操作顺序

1. 备份并验证数据库与源码归档。
2. 构建/固定 Java sandbox image，确认 JADX 版本和 runner SHA。
3. 发布后检查 backend/frontend digest、`/healthz`、登录和业务链路。
4. 失败时只做应用层回滚；数据库恢复使用显式确认口令和已验证安全备份。
