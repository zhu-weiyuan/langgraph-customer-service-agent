# 归档：旧版前端 (7860 Jinja2)

## 文件
- `old_frontend_index.html` — Jinja2 模板渲染的前端页面，通过 FastAPI `templates/index.html` 在 `http://localhost:7860/` 加载

## 废弃时间
2026-07-28

## 原因
被 Vue 3 + Vite 前端取代（`frontend/` 目录，运行在 `http://localhost:5173/`）。

## 差异
| 维度 | 旧 (Jinja2) | 新 (Vue 3) |
|------|-------------|------------|
| 堆栈 | Jinja2 + 原生 JS | Vue 3 + TypeScript + Element Plus |
| 端口 | 7860 (FastAPI 同端口) | 5173 (Vite dev server) |
| API 代理 | 无 | Vite proxy → 7860 `/api/` |
| 状态管理 | 全局变量 | Pinia stores |
| Markdown | marked.js | marked + DOMPurify |
| 流式渲染 | 原生 EventSource | Pinia action polling |
| 打包 | n/a | Vite build |
| 容器化 | "全在一处" | 开发：Vite + FastAPI 分离 |

## 恢复方法
```bash
cd langgraph-customer-service-agent
Move-Item archive/old_frontend_index.html templates/index.html
# 然后访问 http://localhost:7860/
```