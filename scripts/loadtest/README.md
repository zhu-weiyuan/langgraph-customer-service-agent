# scripts/loadtest/

这里是并发压测工具，不是业务实现。

- locustfile.py：Locust 场景。
- run_loadtest.py：命令行启动器。
- mock_app_server.py：不经过真实 LLM 的本地 Mock 服务。
- run_loadtest.bat / run_loadtest.sh：启动包装脚本。

当前本地 LLM 并发能力有限，做 100 worker 时应优先使用 Mock，观察 API、Redis、PostgreSQL、SSE 和指标链路，不要把 LLM 供应能力和服务并发能力混为一谈。
