# ACCEPTANCE - 权限隔离与圆桌修复与MetaGPT编排

> 任务名：权限隔离与圆桌修复与MetaGPT编排
> 创建时间：2026-06-25
> 阶段：Automate（自动化执行）- 验收记录
> 前置：TASK_权限隔离与圆桌修复与MetaGPT编排.md

---

## 一、执行进度记录

### T1: WebSocket SSH 排查诊断
- **状态**：进行中
- **开始时间**：2026-06-25
- **执行人**：AI Agent
- **诊断结果**：（待填写）

### T2: project_member 表 + ORM + 迁移
- **状态**：待执行
- **交付物**：（待填写）

### T3-T18: 后续任务
- **状态**：待执行

---

## 二、验收检查清单

### 2.1 WebSocket 修复
- [ ] WS 握手返回 101 Switching Protocols
- [ ] 前端不再出现"连接失败,正在尝试重连"
- [ ] Caddy 日志无 WS 代理错误
- [ ] 后端日志出现"[WS] 讨论连接已接受"

### 2.2 数据隔离
- [ ] project_member 表存在，字段完整
- [ ] 管理员视角显示所有账号数据
- [ ] 普通用户视角只显示 owner + member 项目数据
- [ ] 成员可见项目，非成员不可见
- [ ] SSE 隔离：用户A不收到用户B的事件
- [ ] 写权限：reviewer 不可修改/删除他人项目

### 2.3 审查员界面
- [ ] reviewer 可见同项目任务
- [ ] reviewer 访问非成员项目任务返回 404

### 2.4 MetaGPT 编排层
- [ ] environment.py/role.py/role_adapter.py/messages.py 存在
- [ ] 现有测试全部通过
- [ ] Environment.from_discussion 可创建实例
- [ ] 圆桌讨论走新编排层正常

### 2.5 双端同步
- [ ] 服务器 git log 与本地一致
- [ ] docker compose ps 全 Running
- [ ] healthz 返回 200
- [ ] alembic current 为 head

---

## 三、问题记录

（执行过程中遇到的问题记录于此）
