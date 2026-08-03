# 根目录文件分类

根目录保留了一些启动入口、历史版本和排障产物。按下面分类处理，避免学习时混淆。

## 主线源码（优先学习）

| 路径 | 作用 |
|---|---|
| app_fastapi.py | 当前 FastAPI 后端入口，端口 7860 |
| agent/ | 核心业务源码 |
| frontend/ | 当前 Vue 3 前端 |
| config/ | 模型注册和配置 |
| knowledge/ | 知识库 Markdown 原文 |
| migrations/ | PostgreSQL/pgvector 数据库迁移 |
| eval/ | 评测数据、指标和报告 |
| tests/ | 自动化测试 |
| scripts/ | 数据导入、评测、迁移、验证和压测脚本 |

## 兼容入口和历史版本

- app.py、app_sync.py、app_original_sync.py：早期或兼容实现，不是当前主线入口。
- agent/runner.py.bak、agent/nodes.py.*：流式改造过程中的备份/恢复文件。
- archive/：已经归档的旧前端和历史资料。

## 一次性排障脚本

根目录的 _*.py、*_debug.py、trace_*.py、_dashboard.html 等文件主要用于某次修复、数据库检查或 SSE 排障，不是稳定 API。

## 运行产物

*.log、*.db、*.db-shm、*.db-wal、*.csv、结果 JSON 和 output 文本多数是运行/评测产物。源码学习时优先查看生成脚本。
