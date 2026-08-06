# Prism 沙箱执行器与远程 Worker

这些镜像是 `deploy/prism_sandbox_executor.py` 使用的固定 runner。上传的
Dockerfile、镜像名、命令、挂载、环境变量和宿主机路径都不会进入执行器 API。
严格模式要求 Docker 已注册 `runsc`，并且五个 profile 的 digest 与本地镜像
ID 完全一致。

## 构建并固化镜像

`docker-compose.build.yml` 和五个 Dockerfile 都不提供可变 tag 默认值。调用方必须给每一种语言同时
提供 `*_BASE_IMAGE` 镜像名称和 `*_BASE_IMAGE_DIGEST` 的 64 位十六进制摘要；
Compose 会把最终 `BASE_IMAGE` 强制拼成 `name@sha256:digest`，任一变量缺失会直接
失败。digest 应从受信任的 registry 签名/审计流程取得，不要把 `latest` 或只有
版本号的 tag 当成生产输入。即使绕过 Compose 直接调用 Dockerfile，未传入
`name@sha256:<64位小写十六进制摘要>` 也会在解析或构建门禁中失败。

```bash
export PYTHON_BASE_IMAGE='python:3.11-slim'
export PYTHON_BASE_IMAGE_DIGEST='<64-hex-digest>'
export NODE_BASE_IMAGE='node:20-bookworm-slim'
export NODE_BASE_IMAGE_DIGEST='<64-hex-digest>'
export JAVA_BASE_IMAGE='eclipse-temurin:17-jdk-jammy'
export JAVA_BASE_IMAGE_DIGEST='<64-hex-digest>'
export GO_BASE_IMAGE='golang:1.23-bookworm'
export GO_BASE_IMAGE_DIGEST='<64-hex-digest>'
export PHP_BASE_IMAGE='php:8.3-cli-bookworm'
export PHP_BASE_IMAGE_DIGEST='<64-hex-digest>'
docker compose -f deploy/sandbox/docker-compose.build.yml build
```

构建完成后，脚本从五个 profile 中现有的本地受信任 tag 读取 Docker image
ID。默认是 dry-run，不会改文件；只有 `--apply` 才会对 `profiles.json` 做
同目录临时文件、fsync、rename 的原子替换：

```bash
deploy/sandbox/pin-profiles.sh
deploy/sandbox/pin-profiles.sh --apply
```

不要手写 digest。`install.sh --apply` 会再次检查五个本地 image ID、profile
digest 和 Docker `runsc` runtime，不通过就停止。

## 安装前提与安装

安装前需要把主部署环境文件和沙箱环境文件放到同一个 deploy tree：

```bash
cp deploy/sandbox/.env.example deploy/sandbox/.env
chmod 600 deploy/.env deploy/sandbox/.env
chown root:root deploy/.env deploy/sandbox/.env
chown root:root deploy/prism-sandbox-executor.service
chmod 0644 deploy/prism-sandbox-executor.service
```

两份文件中的沙箱令牌必须完全相同。主文件使用小时配置
`SANDBOX_DEFAULT_TTL_HOURS`、`SANDBOX_MAX_TTL_HOURS`，沙箱文件使用秒配置
`SANDBOX_DEFAULT_TTL_SECONDS`、`SANDBOX_MAX_TTL_SECONDS`；安装预检会换算后比较，
同时比较并发上限和 UDS 路径。生产值固定为 `SANDBOX_ENABLED=true`、`strict`、
`runsc` 和 `allow_runc=false`；先完成镜像固化，再把主 `deploy/.env` 的
`SANDBOX_ENABLED` 改为 `true`。

在生产机（当前部署主机为 `81.70.251.90`）执行：

```bash
deploy/sandbox/install.sh --apply --deploy-dir /opt/code-review/deploy
```

预检会验证 `docker`、`python3`、`runsc`、`systemd-analyze`、普通文件/权限/归属、
service 模板的 root 所有权与只读安全模式、五个 Dockerfile 的摘要门禁、配置一致性、
五个本地镜像和 systemd 单元。只有预检全绿才会创建
`prism-sandbox` 账户、安装单元并启动服务；启动后必须通过
`/run/prism-sandbox/agent.sock` 的 Bearer `/health` 且返回 `ready=true`。令牌不会
被脚本打印。安装脚本默认 dry-run，未加 `--apply` 不会创建或启动任何资源。

`prism-sandbox` 属于 Docker 组，而 Docker 组在主机上等价于 root。因此建议把
执行器放在专用 Worker 主机；不要把它当作数据库主机的普通应用进程。生产主机上
的 API 只通过 socket 组访问执行器，Backend 不挂载 Docker Socket。

## 远程 HTTPS Worker 网关

需要把执行器放到另一台 Worker 主机时，使用
`worker-gateway.nginx.conf.example`。它只监听 HTTPS，把 Bearer 原样透传到本地
Unix Socket；执行器本身仍负责鉴权、profile allowlist 和任务状态。修改模板中的
域名和证书路径后复制到 Nginx 配置目录，并让 Nginx 账号加入 socket 组（不同发行版
可能是 `nginx` 或 `www-data`）：

```bash
install -m 0644 deploy/sandbox/worker-gateway.nginx.conf.example \
  /etc/nginx/conf.d/prism-sandbox-worker.conf
usermod -aG prism-sandbox nginx
nginx -t
systemctl reload nginx
```

部署前必须完成以下外部配置：

1. 为 `worker.example.com` 配置指向 Worker 主机的 DNS A/AAAA；若直接使用公网
   IP，请确认平台的 HTTPS 公网地址校验策略允许该地址。
2. 使用可信 CA 证书填入 `ssl_certificate` 和 `ssl_certificate_key`。Worker
   安全组的 TCP 443 来源应限制为 Backend 的固定公网出口；当前 Backend 若确实
   直接以 `81.70.251.90` 出网，可用 `81.70.251.90/32`，但必须先核对 NAT 后的实际
   出口地址。不要默认放行 `0.0.0.0/0`，证书签发临时端口用完后立即关闭。
3. 确认本机 `/run/prism-sandbox/agent.sock` 的组权限为 `0660`，并重启 Nginx
   使新增的组成员生效。
4. 在平台的超级管理员界面注册 Worker，或由超级管理员调用
   `POST /api/v1/sandboxes/workers`：`worker_type=managed`、`transport=https`、
   `endpoint=https://worker.example.com`，填入与执行器相同的 Bearer token，声明
   实际支持的语言和 `whitebox`、`blackbox`、`combined`、`deploy` 模式，然后调用
   `POST /api/v1/sandboxes/workers/{worker_id}/health` 执行健康检查。

示例注册数据（令牌只通过受保护的管理员请求提交，不写入仓库）：

```json
{
  "code": "worker-remote-01",
  "name": "专用沙箱 Worker",
  "worker_type": "managed",
  "transport": "https",
  "endpoint": "https://worker.example.com",
  "token": "<与执行器一致的随机令牌>",
  "supported_languages": ["python", "node", "java", "go", "php"],
  "supported_modes": ["whitebox", "blackbox", "combined", "deploy"],
  "runtime": "runsc",
  "max_concurrency": 1,
  "priority": 100,
  "enabled": true
}
```

网关模板已做静态配置审计，但当前交付没有宣称跨主机 DNS、证书、防火墙、Nginx
到执行器和平台注册的端到端验收；完成这些外部步骤后，应从 Backend 发起一次
带 Bearer 的 health、白盒、黑盒和停止/过期回收流程并保存审计证据。

## 运行边界

`local_development` 只允许在明确设置
`SANDBOX_ALLOW_RUNC_LOCAL_DEVELOPMENT=true` 的本机开发环境使用，不能用于共享或
生产主机。部署预览仍在 `network none` 容器内运行并绑定容器回环端口 8080；预览请求
只能经镜像内固定的 `/opt/prism/runner.sh proxy` 转发到该回环端口，不接受请求指定的
其他上游地址。
