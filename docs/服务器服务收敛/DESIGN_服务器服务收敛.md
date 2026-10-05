# 宿主机服务收敛设计

## 服务拓扑

```mermaid
flowchart LR
  Internet -->|80/443| PrismFront[Prism 前端容器]
  PrismFront --> PrismBack[Prism 后端容器]
  PrismBack --> MySQL[(Prism MySQL)]
  PrismBack --> Redis[(Prism Redis)]
  PrismBack --> ClamAV[ClamAV]
  PrismBack --> Embed[嵌入模型]
  PrismBack --> Sandbox[Prism 沙箱执行器]
  PrismBack --> Ops[Prism 运维执行器]
  Backup[备份与验证定时器] --> MySQL
  Cert[证书续期定时器] --> PrismFront
  Block[安全防御定时器] --> Ops
  Internet -.停服.-> Radar[势头雷达]
  Internet -.停服.-> Portfolio[个人站]
  Local -.停服.-> AutoSurface[Auto Surface]
  Admin -.停服.-> BT[宝塔面板]
```

## 执行原则

- 用 systemd 停止并禁用独立服务；宝塔通过 SysV `chkconfig` 关闭自启动后停止。
- 仅改变运行状态和自启动配置，保留应用文件、数据、数据库卷与云安全组。
- 发布使用标准备份、隔离恢复、迁移、镜像构建及健康门禁；生产审计修复复用同一请求编号，避免重复触发 root 动作。
- 停服后按目标监听端口、systemd 状态、容器健康、Prism HTTPS 与关键定时器逐项复核。
