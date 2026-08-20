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

- archive/legacy_backend/：已归档的旧后端入口（含 server_legacy.py）；它们不是当前主线入口。
- archive/legacy_root_tests/：已归档的根目录测试/排障脚本；正式测试只看 tests/。
- archive/root_artifacts/：本机历史输出与评测产物，默认不纳入版本管理。
- agent/runner.py.bak、agent/nodes.py.*：流式改造过程中的备份/恢复文件。
- archive/：旧前端、旧后端和历史资料；仅 archive/legacy_backend/ 中的源码会被版本管理。

## 一次性排障脚本

一次性排障脚本和历史输出统一放在 archive/legacy_root_tests/ 与 archive/root_artifacts/，不再堆在项目根目录。

## 运行产物

*.log、*.db、*.db-shm、*.db-wal、*.csv、结果 JSON 和 output 文本多数是运行/评测产物。评测源代码仍放在 eval/，评测结果放在 eval/reports/（忽略）；源码学习时优先查看生成脚本。
