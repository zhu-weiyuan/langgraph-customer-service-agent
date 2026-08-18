# ⚠️ 注意：Vue 前后端版本说明

## 当前架构

- **后端（FastAPI）**: `app_fastapi.py`（Docker 容器启动入口）
- **前端（Vue 3）**: `frontend/` 目录，Vite 开发服务器（端口 5173+）
- **数据库**: PostgreSQL（`langgraph-postgres` 容器）

## 当前入口与旧后端归档

| 文件 | 用途 | 状态 |
|------|------|------|
| `app_fastapi.py` | **生产入口**，FastAPI + uvicorn 多 worker | ✅ **当前使用** |
| `archive/legacy_backend/app_legacy.py` | 旧版 `http.server` 单线程实现 | ❌ **已归档，仅供追溯** |

`archive/legacy_backend/app_legacy.py` 不支持：
- Vue 前端的 API 端点（`/healthz`、`/api/auth/login`、`/api/auth/me` 等）
- 用户认证 / 会话管理
- 多 worker 并发
- Stream SSE 的正确实现

**不要切换回归档后端**，否则 Vue 前端无法正常工作。

## Vue 前端启动

```bash
cd frontend
npm install          # 首次使用
npm run dev          # Vite 开发服务器，默认 5173
```

前端通过 Vite proxy 将 `/api` 请求转发到后端 `http://127.0.0.1:7860`。

## PostgreSQL

`USE_POSTGRES=1` 已配置在 docker-compose.yml 中。FastAPI 版本通过 `runner.py` → `make_checkpointer()` → `AsyncPostgresSaver` 自动连接。

## 日常操作

```bash
# 构建并启动后端
docker compose build
docker compose up -d

# 启动 Vue 前端开发服务器
cd frontend && npm run dev
```