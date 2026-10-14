# 发布自动回退与运行内存复核

观察日期：2026-10-14。部署失败发生于 04:38:49 UTC；保存的只读主机证据采集于 05:12–05:13 UTC。候选提交为 `c1066983b0abf9badea6fcd43d34e05fa94c8466`（4.0.101），回退应用提交为 `6534493efbf7ec6eb0365b28019602cf8abd92d8`（4.0.100）。生产仅执行日志、进程/容器元数据、文件发布键及数据库长度元数据读取；未修改生产配置、业务数据或服务，也未操作 GUI。

## 结论及不能外推的边界

部署事务的直接退出原因已确定：`post_release_ops_check` 的 `./ops-check.sh` 返回 1，触发自动应用回退。原始 `ops_check_output` 被命令替换捕获但没有写入部署日志，所以**无法还原这一轮 JSON 的具体 blocking_checks**。04:37:39 UTC 的 systemd 独立巡检发生在切换期间，不能替代 04:38:49 的最后巡检结果。

真实运行故障也已确定：04:38:47 UTC，候选运行窗口的 Docker cgroup `a2404894…` 达到 768 MiB 内存上限，内核杀死了常驻 `uvicorn`。这比部署回退触发早约两秒。04:36:23 UTC 同一 cgroup 已发生一次相同 OOM。两次 OOM 与失败窗口构成强关联，但缺失最后巡检 JSON，不能把某个具体检查项或某条业务函数声称为已经完整证明的唯一根因。

4.0.100 也存在相同压力：切换前旧容器 `82b8c87e…` 在 04:34:59 UTC 被 OOM；回退后的当前容器 `9484da60…` 在 04:40:27 UTC 被 OOM。因此不能认定这是 4.0.101 单独引入的回归。

## 时间链及证据

| UTC 时间 | 事件 | 事实边界 |
|---|---|---|
| 04:34:59 | 旧 cgroup `82b8c87e…` 的 uvicorn 被 OOM；新 `a2404894…` scope 同秒启动 | 旧应用也有内存压力 |
| 04:35:10 | 部署日志显示候选 Backend 健康，API release/version 为 c106 / 4.0.101 | 仅此时健康冒烟 |
| 04:36:23 | `a2404894…` 的 uvicorn PID 3744860 被 memcg OOM 杀死 | 同一候选运行窗口第一次 OOM |
| 04:37:46 | Frontend/HTTPS 冒烟通过，默认环境校准为 c106 / 4.0.101 | 并不保证随后稳定 |
| 04:38:47 | `a2404894…` 的 uvicorn PID 3746831 再被 memcg OOM 杀死 | usage = limit = 786432 kB |
| 04:38:49 | `post_release_ops_check` 返回 1，自动回退开始 | 原 JSON 未保留，具体阻断项未知 |
| 04:38:58 | 同一 cgroup 被人为停止，Docker 记录 `restartCount=2` | 与应用回退时间吻合 |
| 04:39:17–18 | Backend/Frontend 回退至 4.0.100，健康与 HTTPS 冒烟通过 | 数据库不 downgrade/restore |
| 04:40:27 | 当前 `9484da60…` 的 uvicorn PID 3753284 被 OOM | 当前容器 ID 与镜像可直接核对 |
| 04:40:32–33 | 回退应用再次完成启动 | 当前 `RestartCount=1`；`OOMKilled=false` 不能否定历史 OOM |

已保存：[内核及 Docker OOM 元数据](evidence/production/v4101_oom_recheck.txt)、[原部署日志副本](evidence/production/deploy-v4101-attempt1.txt)。`journalctl --utc` 只影响显示，采集命令的 `--since/--until` 已显式带 `UTC`，避免误查前八小时。

候选旧容器目录已经移除，`docker events` 对该历史窗口没有返回，无法再通过 `docker inspect` 得到候选标签。`a2404894…` 对候选 Backend 的映射依赖新 scope 启动时间、唯一 uvicorn 应用容器及随后的候选部署/回退时间链；没有把这种时间关联写成留存的直接标签证据。候选日志也随容器移除；保留的请求时序属于回退后的 4.0.100。

04:38:47 的进程表显示 uvicorn RSS 194803 个页（约 761 MiB），同 cgroup 的 alembic 仅 690 个页、runc INIT 2912 个页。04:40:27 的只读 Python 驱动约 27 MiB，而 uvicorn 约 778 MiB，实际被杀的是 uvicorn。故“只读驱动退出 137”不能取代内核进程身份，也不能把主要占用归因于 Alembic 或 ClamAV。

## 回退后的默认环境漂移

[发布键与主机元数据快照](evidence/production/v4101_release_environment_recheck.txt)直接表明：

| 状态源 | SHA / Version |
|---|---|
| 候选树 `deploy/.env` 的四个发布键 | c106 / 4.0.101 |
| 候选树 `.releases/current.env` | 6534493e / 4.0.100 |
| Backend/Frontend 实际镜像 | 6534493e / 4.0.100 |
| Decoy 实际镜像 | `prism-decoy:4.0.101` |

原 rollback 加载上一状态并导出发布变量，重建应用及提交回退账本，但没有调用默认环境校准；所以当前进程的 bound Compose 能正确回退，新的 default Compose 仍读取候选 `.env`。这是独立于首次巡检失败的明确回退缺陷。主代理负责 rollback 的校准和失败巡检日志修复。

Decoy 原脚本没有回退，release/containers 巡检也没有绑定它的版本。完成 `.env` 校准后仍应明确 Decoy 的发布/回退契约；不能宣称所有 Compose 镜像均已回到 4.0.100。

运维执行器 PID 3750444、Unix socket 均在 04:37:48 创建并保持 active，socket 模式 660 / root:991。04:25–04:40 区间没有留存的防御 `ops_execution` 回执或同窗口执行账本；数据库可查的采集从 04:45:33 开始。此缺口不能被解释为防御采集“已通过”。ClamAV 容器 healthy，有活跃 clamd/freshclam；其低 RSS 不构成 OOM 证据。

## 数据容量核验

通过 MySQL 容器内部已有凭据执行固定 SELECT；只返回 COUNT、SUM/MAX/OCTET_LENGTH、数值 ID、状态、时间和会话键 SHA256，未返回检查点、对话、Prompt、Response 或凭据正文。每个长度查询设置 10 秒 MAX_EXECUTION_TIME。原始结果保存为[数据库容量元数据](evidence/production/v4101_db_payload_size_recheck.txt)。

| 数据 | 行数 | 总字节数 | 最大单行字节数 |
|---|---:|---:|---:|
| agent_response_run.checkpoint_json | 1222 | 188204325 | 2094272 |
| agent_team_task.result_json | 648 | 6132682 | 102625 |
| agent_team_task.input_json | 648 | 254875 | 47348 |
| agent_team_task.errors_json | 648 | 9746 | 760 |
| ai_call_log.prompt | 14036 | 269365566 | 251251 |
| ai_call_log.response | 14036 | 129607198 | 227980 |
| agent_response_transcript_message.message_json | 3962 | 15023641 | 50520 |

单个最大会话有 312 条运行，其 checkpoint 合计 37755507 字节。实际 Team result 最大约 100 KiB，现有容量证据不足以把 768 MiB OOM 唯一归因于 `project_team_result`。全历史检查点物化属于更直接、可以合成复现并局部修复的内存放大路径。

## 已实现的本地窄列修复

本子任务仅修改 `backend/app/services/agent_mesh_service.py` 的两个状态读取函数：

- `_authoritative_run_state` 保留相同用户、surface、session 和 ID 降序语义，只 SELECT run_id/status。
- `_latest_run_states` 在同账号与精确 surface/session 配对范围内，按每会话 MAX(id) 子查询后 join，只 SELECT surface/session_key/run_id/status。仍按 ID 降序输出，重复引用不增加返回行，不加载历史 checkpoint 或 ORM 完整实例。

新回归文件为 `backend/tests/unit/services/test_agent_mesh_run_state_projection.py`，验证真实发出的 SELECT 列、每会话 MAX(id)、ORM load 事件、同账号隔离、交叉 surface/session 排除、较旧 create_time 但较新 ID 的状态语义、空/不存在会话与 36 个 1 MiB 检查点。

| 核验 | 结果 |
|---|---|
| 原 c106 两函数在隔离进程注入新测试 | 2 failed / 2 passed；失败点均为 SELECT 包含 checkpoint_json |
| 修复后的新测试、既有 mesh service、dispatcher、RBAC 回归 | 81 passed |
| 单文件 Ruff | 既存 E501 长行原892/当前902；本次变更未引入新错误 |

红/绿日志：[原函数红测](evidence/local/mesh_state_projection_red.txt)、[相关 81 项绿测](evidence/local/mesh_state_projection_green.txt)。原函数仅在本地测试进程内通过 AST 加载，未回写工作树，也未调用生产服务。

合成 SQLite 样本（36 × 1048600 字节）使用 Python tracemalloc 得到：

| 查询 | Python 查询期堆峰值 | 状态数 | 是否 SELECT checkpoint |
|---|---:|---:|---|
| 原全历史 ORM 查询 | 37977410 B | 1 | 是 |
| 最新状态窄列查询 | 114909 B | 1 | 否 |

此处只测 Python 堆，排除 SQLite C 分配和进程 RSS，不能冒充生产峰值或已修复全部 OOM。可重跑[合成测量脚本](evidence/local/measure_mesh_state_projection.py)及[JSON 结果](evidence/local/mesh_state_projection_memory.json)：

```bash
cd backend
PYTHONPATH=. .venv/bin/python ../docs/小菱全站混合执行与团队20261013/evidence/local/measure_mesh_state_projection.py
.venv/bin/python -m pytest tests/unit/services/test_agent_mesh_run_state_projection.py tests/unit/services/test_agent_mesh_service.py tests/unit/services/test_agent_mesh_dispatcher.py tests/unit/services/test_agent_mesh_dispatch_rbac_regression.py --no-cov -q
```

## 接下来需要核验

保留的 4.0.100 请求时序显示：04:39:07–04:40:18 共 14 次 session GET、13 次团队列表 GET、260 次团队详情 GET、90 次会话 heartbeat、67 次 inbox pull；04:40:27 OOM 后服务再次启动。精确计数保存在[回退后路由时序](evidence/production/v4100_post_rollback_route_timing.json)。这是历史观测，没有为复现新发压力请求，也不能据此断言某个具体请求唯一触发 OOM。

本次源修复由主代理独立复测。后续候选验收需要覆盖相同账号会话列表/Agent 发现、session 历史和团队轮询场景，同时记录 cgroup memory.events / 峰值、RestartCount 变化及完整 ops-check JSON。History API 中不必要的运行大列加载由主代理处理。本报告不把尚未发布的窄列源码写成生产稳定性已通过。

05:12 UTC 保存的主机快照总内存约 7.50 GiB、可用约 4.91 GiB；3 GiB Swap 剩余约 157 MiB（约 95% 已使用）。当前 Backend 的硬限制 768 MiB 已被明确撞到。若另行选择临时增容，必须结合整个服务峰值预算和实际复测，不能用修改限额代替去掉不必要的大列查询。此子任务没有更改限额。
