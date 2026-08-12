<<<<<<< HEAD
# scripts/loadtest/

这里是并发压测工具，不是业务实现。

- locustfile.py：Locust 场景。
- run_loadtest.py：命令行启动器。
- mock_app_server.py：不经过真实 LLM 的本地 Mock 服务。
- run_loadtest.bat / run_loadtest.sh：启动包装脚本。

当前本地 LLM 并发能力有限，做 100 worker 时应优先使用 Mock，观察 API、Redis、PostgreSQL、SSE 和指标链路，不要把 LLM 供应能力和服务并发能力混为一谈。
=======
# 压测与混沌演练手册（P5）

对象：`app_fastapi.py`（uvicorn 多 worker）+ Redis（+ 可选 Postgres）。
工具：[Locust](https://locust.io)（压测机安装 `pip install locust`，应用容器不装）。

## 1. 场景

`locustfile.py` 混合流量（详见文件 docstring）：

| 场景 | 权重 | 说明 |
|---|---|---|
| POST /api/chat 非流式 | 6 | 带 `X-Idempotency-Key`，429 视为成功（限流是预期行为） |
| POST /api/chat SSE 流式 | 3 | 逐帧解析 `data:{json}`，要求收到 `{"done":true}` |
| GET /api/sessions + /api/analytics | 1 | 面板只读轮询 |
| POST /api/rating | ~5% | chat 成功后按概率追加 |

会话多轮：15% 概率发送结束语（"谢谢/再见"），覆盖满意度/finalize/escalate 分支。

## 2. 运行

```bash
# 基线：100 并发梯度（10/s 爬升），10 分钟，无 UI
locust -f scripts/loadtest/locustfile.py --host http://localhost:7860 \
       --users 100 --spawn-rate 10 --run-time 10m --headless --stop-timeout 60

# 梯度对比：分别跑 10 / 25 / 50 / 100 users，记录各档 P50/P95/失败率
for u in 10 25 50 100; do
  locust -f scripts/loadtest/locustfile.py --host http://localhost:7860 \
         --users $u --spawn-rate 10 --run-time 5m --headless \
         --csv reports/load_$u
done
```

配置了 `API_KEYS` 时：`export LOADTEST_API_KEY=<key>`。
阈值可调：`CHAT_P95_MS`（默认 15000）、`MAX_FAIL_RATIO`（默认 0.02）。

## 3. 观察面

- `GET /api/metrics`：`http_request_duration_seconds`、`llm_requests_total`、
  `rate_limit_events_total`、`circuit_breaker_state`
- Grafana（`--profile monitoring`）：agent-overview dashboard
- 应用日志：JSON 结构化，按 `request_id` 关联

## 4. 混沌演练步骤与预期行为

每项演练在 50 并发稳态流量下执行，观察 3–5 分钟后恢复。

### 4.1 停 Redis

```bash
docker stop langgraph-redis      # 恢复: docker start langgraph-redis
```

预期行为清单：

- [ ] 应用不崩溃、不重启；日志出现一次
      `Redis rate limiter unavailable ... fail-closed degradation` 告警
- [ ] 限流进入本地保守模式（限额降到 50%）：429 比例上升而非放开全量
- [ ] `/api/ready` 返回 200，`checks.redis.degraded_mode=true`（可降级项）
- [ ] chat 成功率（非 429）保持 > 95%；无 5xx 突增
- [ ] Redis 恢复后 60s 内 429 比例回落到基线

### 4.2 停 LLM 后端

```bash
# 停掉 OPENAI_BASE_URL 指向的推理服务（或改成黑洞地址模拟）
```

预期行为清单：

- [ ] 熔断器在连续失败后打开：`circuit_breaker_state` 指标 → 2 (open)
- [ ] chat 返回 500/504 兜底 JSON（`{"error": "服务暂时不可用..."}` /
      `请求处理超时`），单请求耗时不超过 `GRAPH_TIMEOUT_SECONDS`(55s)
- [ ] SSE 流返回 `{"error": ...}` 帧后正常收尾，连接不悬挂
- [ ] `/healthz` 保持 200（存活不受影响）；`/api/health` 的
      `llm.reachable=false`
- [ ] LLM 恢复后熔断半开→闭合，成功率回升，无需重启应用

### 4.3 重启 Postgres（USE_POSTGRES=1 部署）

```bash
docker restart langgraph-postgres
```

预期行为清单：

- [ ] 重启窗口内 chat 报 5xx（checkpointer 不可写），比例与窗口时长成正比
- [ ] 应用进程存活；Postgres 就绪后无需重启应用即自动恢复
- [ ] 恢复后旧会话上下文完整（checkpoint 持久在 Postgres）
- [ ] 无连接泄漏：恢复 5 分钟后 `pg_stat_activity` 连接数回到基线

### 4.4 滚动重启应用（graceful drain）

```bash
docker compose -f docker-compose.prod.yml restart customer-service
```

- [ ] SIGTERM 后在途请求在 30s drain 窗口内完成（`SHUTDOWN_TIMEOUT_SECONDS`）
- [ ] SSE 连接收到 TCP 关闭，前端按断线重连处理
- [ ] 重启后会话上下文保留（checkpoints 卷持久化）

## 5. 验收标准

| 指标 | 目标 | 测法 |
|---|---|---|
| chat 非流式 P95 | < 15s（本地 LLM）/ < 8s（生产推理集群） | locust `[SLO]` 输出 |
| chat 非流式 P50 | < 5s | locust 报表 |
| SSE 首 token 延迟 | < 3s（人工抽查/前端计时） | 浏览器 devtools |
| 失败率（非 429） | < 2% | locust `[SLO]` 输出 |
| 100 并发下 429 行为 | 返回 429+`Retry-After`，无 5xx 雪崩 | locust 报表 + metrics |
| 停 Redis 演练 | 4.1 清单全过 | 人工演练 |
| 停 LLM 演练 | 4.2 清单全过 | 人工演练 |
| 重启 Postgres 演练 | 4.3 清单全过（若启用） | 人工演练 |
| drain 演练 | 4.4 清单全过 | 人工演练 |
| 内存 | 10 分钟压测 RSS 增长 < 20% | docker stats |

locust 进程退出码非 0 即 SLO 断言失败（可直接接 CI gate）。
>>>>>>> origin/master
