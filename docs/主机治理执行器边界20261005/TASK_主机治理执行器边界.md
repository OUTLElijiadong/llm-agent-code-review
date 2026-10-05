# 主机治理与执行器边界任务拆分

## 执行状态

- [x] T1：通用高权动作已从模型目录、后端 schema 与 root 执行器移除，负向测试通过。
- [x] T2：日志、备份目录与 journal 范围已收紧，穿越、符号链接、相邻目录拒绝测试通过。
- [x] T3：SO_PEERCRED、runtime owner/mode、dotenv 隔离、容器 capabilities 已实现并通过生产正反请求验收。
- [x] T4：`4.0.47` 精确 SHA 已发布；生产备份独立恢复、HTTPS、服务健康、systemd 和非项目服务状态通过。
- [x] T5：ACCEPTANCE、FINAL、TODO 已根据本机及生产证据更新。

## T1：执行器高权动作封闭

- 输入：生产 SHA `70dfb9b08f8a950a40d8682a791ca882037b80b4` 的执行器源码与现有测试。
- 输出：移除任意文件写、systemd、package、docker container、firewall、account、SSH key 动作；补充负向测试。
- 验收：每个已移除动作在执行任何副作用前失败。
- 依赖：无。

## T2：文件和 journal 证据范围

- 输入：现有 `read_text_file`、`list_directory`、`journal_query` 行为。
- 输出：安全路径根与固定 journal unit 白名单及回归测试。
- 验收：`/root`、`/etc`、未授权 `/opt` 路径拒绝；批准日志路径仍能读取。
- 依赖：T1。

## T3：Unix socket 身份与环境隔离

- 输入：生产容器身份 UID 10001/GID 991 实测、当前 Compose 与 systemd unit。
- 输出：`SO_PEERCRED` 校验、后端不再注入静态 token、两个 root unit 均不继承整份 `.env`、安全封禁脚本按白名单读取 3 个设置、socket 目录验证 `root:prism-ops 0710`。
- 验收：正确 peer 通过；错误身份拒绝；无 token 的 socket 只读探针通过；root 子进程环境不含 dotenv 密钥；目录 owner/group/mode 不符则拒绝启动。
- 依赖：T1。

## T4：部署与生产复测

- 输入：T1-T3 本地测试通过、精确 commit SHA、可验证备份和上一版本镜像。
- 输出：`4.0.47` / `efd620d3b1cd1e989996385085b197fa888a84fb` 同版本发布；健康接口、镜像 SHA、备份恢复、运维执行器、timer 及主机监听状态证据。
- 验收：`deploy/RELEASE_CHECKLIST.md` 相关门禁通过；失败立即走内置回滚。
- 依赖：T1-T3。

## T5：治理交付记录

- 输出：ACCEPTANCE、FINAL、TODO，列明主机权限治理、未测范围、外部依赖和下一步。
- 验收：不把生产只读核验写成攻击验证，不把安全加固写成已证明无漏洞。
- 依赖：T4。
