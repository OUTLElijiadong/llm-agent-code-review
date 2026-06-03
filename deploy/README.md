# 部署指南

用 Docker Compose 一键部署，三个容器：**MySQL + 后端(FastAPI) + 前端(Vue/nginx)**。
前端 nginx 已把 `/api` 反代到后端，前后端同源，无需配跨域。

---

## 一、首次部署（在服务器上）

> 前提：服务器已安装 Docker（含 compose 插件）。
> 国内轻量应用服务器在创建时选「Docker CE」应用镜像即自带，省去装 Docker 的步骤。

```bash
# 1. 克隆代码
git clone https://github.com/OUTLElijiadong/llm-agent-code-review.git
cd llm-agent-code-review/deploy

# 2. 配置环境变量（只需填一次）
cp .env.example .env
vim .env          # 改 MySQL 密码、填 DEEPSEEK_API_KEY、JWT_SECRET

# 3. 一键启动
./deploy.sh
```

启动完成后访问：

- 前端：`http://你的服务器IP`
- 接口文档：`http://你的服务器IP/docs`

> ⚠️ 记得在云服务商的「安全组 / 防火墙」放行 **80** 端口（如需直连后端再放行 8000）。

---

## 二、日常更新（改完 bug 上线）

**本地电脑**（你的开发机）：

```bash
git add -A && git commit -m "fix: 修复了xxx" && git push
```

**服务器**：

```bash
cd llm-agent-code-review/deploy && ./deploy.sh
```

`deploy.sh` 会自动 `git pull` + 重新构建 + 重启，一条命令搞定。

---

## 三、常用运维命令

```bash
docker compose ps                 # 查看容器状态
docker compose logs -f backend    # 实时看后端日志
docker compose logs -f frontend   # 看前端/nginx 日志
docker compose restart backend    # 只重启后端
docker compose down               # 停止全部容器（数据保留）
docker compose up -d              # 重新拉起
```

---

## 四、数据持久化

MySQL 数据存放在名为 `mysql_data` 的 Docker volume 中：

- `docker compose down` **不会**删除数据，放心停服。
- `docker compose down -v` 会**连数据一起删除**（清库重来时才用，谨慎！）。
