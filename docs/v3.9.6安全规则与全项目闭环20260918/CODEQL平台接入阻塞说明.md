# CodeQL 平台接入阻塞说明

## 已完成范围

`.github/workflows/codeql.yml` 使用 GitHub 官方 CodeQL Action，对 Prism 仓库执行 Python 与 JavaScript/TypeScript 的 `security-extended` 查询套件。运行 `35320485649` 已真实成功，Code Scanning API 中存在两个分析记录。这证明仓库 CI 闭环可用，不证明用户上传项目已经在 Prism 平台执行 CodeQL。

## 为什么暂不把 CLI 接入产品 worker

GitHub 的 CodeQL CLI 条款对自动化分析、非公开代码库以及“作为托管服务提供给他人使用”有明确许可边界；付费 GitHub Code Security 许可是不同路径。当前没有可核验的书面许可，也没有专用的多租户隔离 worker，因此直接下载 CLI、在现有业务沙箱中执行并对用户开放会造成授权与数据隔离风险。

官方条款：[CodeQL CLI Terms](https://github.com/github/codeql-cli-binaries/blob/main/LICENSE.md)。

官方 CLI 说明：[CodeQL CLI](https://docs.github.com/en/code-security/concepts/code-scanning/codeql/codeql-cli)。

官方资源基线：[Recommended hardware resources](https://docs.github.com/en/code-security/reference/code-scanning/codeql/hardware-resources-for-codeql)。小型代码库起步建议至少 8GB RAM、2 核、14GB SSD；现有通用审查沙箱不是这个隔离和资源等级。

## 解锁条件

1. 取得适用于平台托管用户上传源码的书面许可或启用对应 GitHub Code Security 许可。
2. 部署专用、无宿主路径、无跨租户缓存的 CodeQL worker；为每个任务创建临时数据库、SARIF 和结果目录，任务结束后清理并验证租约回收。
3. 配置至少 8GB RAM、2 核、14GB SSD 的 worker 配额，并对大型项目实施队列、超时和磁盘水位保护。
4. 先实现 feature-off 配置、SARIF schema 校验、Finding 去重/租户隔离和失败回收测试，再开放 UI/API。

在上述条件完成前，规则目录只展示仓库 CI 的 CodeQL 能力状态，不能显示“用户项目 CodeQL 已执行”，也不能伪造 SARIF 或 Finding。
