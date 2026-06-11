# 响应式布局与完整测试 TODO

## 当前无阻塞待办

- 响应式布局改造已完成。
- 前端构建和后端测试已通过。

## 环境注意

1. 后端测试请使用 `backend/.venv/bin/python -m pytest -q`，不要使用系统 `python3`。
2. 浏览器登录验收依赖本地 MySQL，若 `/api/auth/login` 返回 500，先启动 Docker 并确认 `cr_mysql` 健康、`3307 -> 3306` 端口映射存在。
3. 前端 dev server 当前使用 `http://127.0.0.1:5173/`。
