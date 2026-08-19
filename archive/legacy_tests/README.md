# Legacy tests

此目录保存已迁移架构对应的历史测试，不属于默认测试套件。

`test_feedback_improvement_legacy.py` 针对旧版 `agent.memory` 的 SQLite 风格反馈接口；当前生产实现已统一为 PostgreSQL `FeedbackStore`，且反馈改进队列尚未由应用层接入，因此不应通过恢复旧 API 来“修复”该测试。
