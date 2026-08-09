# 应用层压测手册（Mock LLM 版）

> 一句话：**这套压测量的是应用能力，不是模型吞吐。**
> 把 LLM 换成固定延迟的假模型后，QPS / P95 / 瓶颈才反映异步化、限流、
> 会话管理、SSE 这些应用层工程质量——简历上"多 worker 压测 100 并发"
> 该证明的是这个。

---

## 1. 为什么必须 mock 掉 LLM

本项目线上接的是本地 35B，单次生成 **15~22 秒**。如果直接用 100 并发压
`/api/chat`：

| 现象 | 真实原因 | 你以为的原因 |
|---|---|---|
| P95 ≈ 20s | 模型单次生成就要 20s | 应用慢 |
| QPS ≈ 0.5 | 显卡只能同时跑 N 个序列 | 应用扛不住 |
| 加 worker 没用 | 瓶颈在 GPU，不在 Python | 代码没优化 |

也就是说：**被测对象根本不是你写的代码**，压出来的数字换个模型就全变，
既不能用来定位应用瓶颈，也不能写进简历当工程结论。

把 LLM 换成"固定 200ms 延迟的假模型"之后，延迟变成**已知常量**，于是：

* 任何超出 `200ms × 图内 LLM 调用次数` 的延迟，都是**应用自己的开销**
  （序列化、checkpoint 落盘、锁竞争、限流排队、事件循环阻塞）。
* 并发 100、LLM 200ms，理论 QPS 上限 = `100 / (0.2 × LLM调用次数)`。
  实测差多少，差的就是应用效率。
* 如果某处把 LLM 调用放在了**同步阻塞**路径上，QPS 会立刻卡在
  `worker数 / 延迟` 而不是 `并发数 / 延迟` —— 一压就现原形。
* 限流（RateLimiter）、并发闸门（`limiter.concurrency`）、SSE 长连接、
  会话/checkpoint 写入，这些是唯一还在动的变量，也正是要证明的东西。

Embedding 同理：不 mock 的话 RAG 那一步照样打 SiliconFlow，延迟被外部
服务绑死，还烧钱。

---

## 2. 分层压测方法论

| 层次 | 配置 | 并发 | 目的 | 结论能说什么 |
|---|---|---|---|---|
| **L1 协议层** | `--weights healthz=1` | 200~500 | 纯 HTTP 栈上限 | uvicorn/worker 数配置是否合理 |
| **L2 应用层**（主战场） | `MOCK_LLM=1 MOCK_EMBEDDING=1` | 100 | 异步化 / 限流 / 会话 / SSE | "多 worker 下 100 并发稳定服务，P95 xx ms，无 5xx" |
| **L3 端到端** | 真 LLM，`MOCK_*` 全关 | 5~10 | 真实用户体感 | "端到端 P95 xx s（受限于本地 35B）" |
| **L4 混沌** | 见 `README.md` 第 4 节 | 50 | 停 Redis / 停 LLM / 重启 PG | 降级与恢复行为 |

**不要混着说**：L2 的 QPS 不能拿去承诺线上容量，L3 的 P95 也不能拿去说
应用性能。报告里两个数字要分开列，并注明各自的 LLM 配置。

---

## 3. Mock 开关（默认全关，不影响正常运行）

实现在 `agent/mock_llm.py`，接线点只有 4 处，均为早返回（early-return）：

| 位置 | 行为 |
|---|---|
| `agent/llm_client.py: LLMClient.chat` | `MOCK_LLM=1` → `time.sleep(延迟)` + 固定回复，不发 HTTP |
| `agent/llm_client.py: LLMClient.chat_json` | 按 system 提示词分流，返回**合法 JSON**（key 落在 `EXPECTED_JSON_KEYS` 白名单内，走正常代码路径而非兜底分支） |
| `agent/llm_client.py: LLMClient.chat_stream` | 逐 token 吐出，间隔 = `MOCK_LLM_DELAY_MS / token数` |
| `agent/llm_gateway.py: LLMGateway.chat` | `await asyncio.sleep(延迟)` + 假 `GatewayResponse`（绕过路由/缓存/预算/熔断） |
| `agent/embedding_client.py: EmbeddingClient.embed` | `MOCK_EMBEDDING=1` → 确定性伪向量（sha256 派生、已 L2 归一化），不发 HTTP、不需要 API Key |

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MOCK_LLM` | 关 | `1/true/yes/on` 打开 |
| `MOCK_LLM_DELAY_MS` | `200` | 单次生成模拟耗时。**设成什么值就要在报告里写什么值** |
| `MOCK_LLM_JSON_DELAY_MS` | 同上 | 意图/情绪等短请求的耗时，可单独调小更贴近真实 |
| `MOCK_LLM_TOKENS` | `48` | 流式切出的 token 数 |
| `MOCK_LLM_REPLY` | - | 覆盖固定回复文本 |
| `MOCK_EMBEDDING` | 关 | 伪向量开关（`MOCK_LLM` **不会**隐含打开它） |
| `MOCK_EMBEDDING_DIM` | `1024` | 伪向量维度（`EMBEDDING_DIMENSIONS`/`PGVECTOR_DIM` 优先） |
| `MOCK_EMBEDDING_DELAY_MS` | `5` | 单批 embedding 模拟耗时 |

异步路径一律用 `asyncio.sleep`，同步路径用 `time.sleep`——这个区分是故意的，
它让"哪条路径阻塞了事件循环"在压测数字上可见。

自检：`python -m unittest tests.test_mock_llm_pure -v`（14 条，纯 stdlib）。

---

## 4. 怎么跑

### 4.1 Windows 一键（推荐给本机 35B 环境）

```bat
REM 默认：100 并发 / 60s / 4 worker
scripts\loadtest\run_loadtest.bat

REM 自定义：users duration workers
scripts\loadtest\run_loadtest.bat 100 60 4

REM 调 mock 延迟（贴近真实模型的相对关系，但保持"快而恒定"）
set MOCK_LLM_DELAY_MS=500
scripts\loadtest\run_loadtest.bat 100 60 4
```

脚本自动：设 `MOCK_LLM=1`/`MOCK_EMBEDDING=1` → 起 `uvicorn --workers N` →
轮询 `/healthz` 等就绪（最多 60s）→ 跑压测 → 写 `reports\loadtest_*.json/.csv`
→ 按端口 kill 掉服务进程。一条命令跑完全程。

Linux/macOS 等价物：`scripts/loadtest/run_loadtest.sh 100 60 4`。

### 4.2 手动跑压测器

```bash
# 100 并发，60s，15s 爬坡，混合流量，带 metrics/进程采样
python scripts/loadtest/run_loadtest.py --host http://127.0.0.1:7860 \
    --users 100 --duration 60 --ramp 15 --profile \
    --json reports/load_100.json --csv reports/load_100.csv

# 梯度对比（画曲线找拐点）
for u in 10 25 50 100 200; do
  python scripts/loadtest/run_loadtest.py --users $u --duration 60 --ramp 10 \
      --json reports/load_$u.json --label "workers=4"
done

# 只压 HTTP 栈（排除业务）
python scripts/loadtest/run_loadtest.py --weights healthz=1 --users 200 --duration 20

# 零三方依赖模式（压测机没装 httpx 时）
python scripts/loadtest/run_loadtest.py --backend threads --users 50 --duration 30

# 接 CI gate：超阈值退码 1
python scripts/loadtest/run_loadtest.py --users 100 --duration 60 \
    --slo-p95-ms 1500 --max-fail-ratio 0.01
```

关键参数：`--users/--duration/--ramp`、`--weights chat=6,chat_sse=3,sessions=1,healthz=1`、
`--think-min/--think-max`（默认 0 = 最大压力；设 1~4 更像真人）、
`--api-key`（应用配了 `API_KEYS` 时必填）、`--backend auto|httpx|threads`。

locust 仍然可用（`locustfile.py` 未改动）；`run_loadtest.py` 是**零依赖备选**，
两者场景权重一致，可互相印证。

### 4.3 自测压测器本身

```bash
python scripts/loadtest/mock_app_server.py --port 7899 --chat-delay-ms 200 &
python scripts/loadtest/run_loadtest.py --host http://127.0.0.1:7899 \
    --users 40 --duration 12 --backend threads --weights chat=1
# 期望：p50 ≈ 200ms，QPS ≈ 平均并发 / 平均延迟
```

---

## 5. 结果怎么读

```
requests   : 4437   ok: 4437   failed: 0   429(rate-limited): 7658
throughput : 290.85 req/s
concurrency: max in-flight 100 / avg 89.8 (configured users=100)
```

* **429 单独统计，不计失败**。限流返回 429 是设计要的保护行为，把它算成
  失败会得出"应用不稳定"的错误结论。真正的失败是 5xx、连接错、SSE 没收到
  `done` 帧、业务 `error` 字段。
* **`max/avg in-flight`** 是"并发真的打上去了"的证据。如果 configured=100
  而 avg in-flight 只有 30，说明客户端自己是瓶颈（think time 太长、连接池
  太小、或压测机 CPU 打满），此时的 QPS 不可信。
* **自洽校验**：`QPS ≈ avg_inflight / avg_latency`。两边对不上，说明采样期
  内负载不稳（爬坡没跑完 / 服务端抖动），延长 duration 再测。
* **P99 >> P95** 通常不是应用慢，而是队列/GC/连接建立的尾部；关注它是否
  随并发单调恶化。
* **TTFT（SSE 首 token）** 单独看：它衡量的是"用户多久看到第一个字",
  是 SSE 唯一真正的用户体感指标，和总时长解耦。
* **一次 chat 的理论下限** = `MOCK_LLM_DELAY_MS × 该请求触发的 LLM 调用次数`
  （意图识别 + 生成 + 可能的情绪/满意度判断）。实测 P50 减去这个下限，
  就是应用自身开销。

---

## 6. 多 worker 下哪些指标会失真（重要）

`uvicorn --workers N` 是 **N 个独立进程**，进程之间不共享 Python 内存。因此：

| 指标/能力 | 多 worker 下的真相 | 怎么办 |
|---|---|---|
| `/api/metrics` | **per-worker**：每次请求被随机路由到某个 worker，返回的是那一个进程的计数。相加不等于全局，采样也会跳变 | 只看趋势不看绝对值；要全局就上 Prometheus 多进程模式（`PROMETHEUS_MULTIPROC_DIR`）或按 worker 抓取后聚合 |
| 进程内限流（内存 token bucket） | 实际限额 ≈ 配置值 × N；100 并发下更难触发 429 | 压测限流必须用 **Redis 后端**限流器，否则测的是"N 份各自的限流" |
| 熔断器状态 | 每个 worker 一套，可能一个 open 一个 closed | 观察日志而非单次 `/api/metrics` |
| 内存 RSS | 要把 N 个 worker 加起来看，单进程 RSS 会误导 | `--profile` 已按 `--proc-filter uvicorn` 求和 |
| SQLite checkpoint | 多进程写同一个 db 文件会产生锁竞争，是**真实瓶颈**不是失真 | 压测时如实记录；生产建议 Postgres |
| 内存里的会话/幂等缓存 | 同一 session 的两次请求可能落在不同 worker，幂等/缓存命中率虚低 | 说明书里注明；生产用 Redis |
| 单次请求延迟 | 不失真 | - |

**结论**：多 worker 压测的 QPS/延迟/失败率可信；**进程内计数类指标不可信**。
报告里凡是引用 `/api/metrics` 的数字，都要标注 "per-worker, N=4"。

---

## 7. 压测器自测：真实输出（不是被测应用的性能）

> ⚠️ **以下数字全部来自"压测器 vs 标准库模拟服务端"的自测**，
> 目的只有一个：证明 `run_loadtest.py` 的统计（QPS/分位数/并发/429）是对的。
> 靶子是 `scripts/loadtest/mock_app_server.py`（`ThreadingHTTPServer`，
> 线程一连接一线程模型），**不是** `app_fastapi.py`。
> 这些数字**不能**作为 langgraph-customer-service-agent 的性能结论。
>
> 环境：容器内 Python 3.11.15，客户端与服务端同机（回环网络）。

### 7.1 分位数与 QPS 正确性（chat-only，服务端固定 200ms）

```
python scripts/loadtest/mock_app_server.py --port 7897 --chat-delay-ms 200 &
python scripts/loadtest/run_loadtest.py --host http://127.0.0.1:7897 \
    --users 40 --duration 12 --ramp 2 --backend threads --weights chat=1
```

```
requests      : 2210   ok: 2210   failed: 0   429(rate-limited): 0
throughput    : 181.24 req/s
fail ratio    : 0.00%
concurrency   : max in-flight 40 / avg 36.7 (configured users=40)
------------------------------------------------------------------------------
scenario  reqs  ok    fail  429  qps    p50  p90  p95  p99  max  ttft_p95
--------  ----  ----  ----  ---  -----  ---  ---  ---  ---  ---  --------
chat      2210  2210  0     0    181.2  201  202  203  204  211  -
ALL       2210  2210  0     0    181.2  201  202  203  204  211  -
(latency in ms)
status distribution: 200=2210
```

校验：
* 服务端固定延迟 200ms → 实测 **P50 = 201ms**（+1ms 为回环 + 线程调度），分位数计算正确。
* `QPS ≈ avg_inflight / avg_latency = 36.7 / 0.201 = 182.6` vs 实测 **181.2**，误差 <1%。
* 分位数另有单测覆盖（`tests/test_mock_llm_pure.py::TestLoadtestStats`）：
  1..100 序列的 P50/P90/P95/P99 = 50/90/95/99（nearest-rank）。

### 7.2 100 并发 + 混合流量 + SSE（httpx 后端）

```
python scripts/loadtest/run_loadtest.py --host http://127.0.0.1:7897 \
    --users 100 --duration 15 --ramp 5
```

```
requests      : 4437   ok: 4437   failed: 0   429(rate-limited): 0
throughput    : 290.85 req/s
fail ratio    : 0.00%
concurrency   : max in-flight 100 / avg 89.8 (configured users=100)
------------------------------------------------------------------------------
scenario  reqs  ok    fail  429  qps    p50  p90  p95  p99   max   ttft_p95
--------  ----  ----  ----  ---  -----  ---  ---  ---  ----  ----  --------
chat      2364  2364  0     0    155.0  254  267  285  2840  3847  -
chat_sse  1231  1231  0     0    80.7   218  241  261  1820  3560  75
healthz   389   389   0     0    25.5   51   66   71   1682  2299  -
sessions  453   453   0     0    29.7   57   76   734  3014  3650  -
ALL       4437  4437  0     0    290.9  248  264  281  2583  3847  75
(latency in ms)
status distribution: 200=4437
```

校验：
* 配置 100 并发，**实测 max in-flight = 100**，并发确实打上去了。
* SSE 场景 **TTFT P95 = 75ms**，远小于总时长 261ms —— 流式统计生效
  （靶子 24 个 token 均摊 200ms，首帧 `progress` 立即到）。
* P99 高达 2.6s 而 P95 只有 281ms：这是**靶子**（一连接一线程的
  `ThreadingHTTPServer`）在 100 并发下的排队尾部，不是压测器的问题——
  它恰好说明压测器能抓到尾延迟。
* 靶子进程退出时自报 `served=15629 max_inflight=96`，与客户端观测吻合。

### 7.3 429 归类正确性（服务端并发上限 20，客户端 100 并发）

```
python scripts/loadtest/mock_app_server.py --port 7896 --chat-delay-ms 200 --max-concurrency 20 &
python scripts/loadtest/run_loadtest.py --host http://127.0.0.1:7896 \
    --users 100 --duration 15 --ramp 5
```

```
requests      : 9527   ok: 1869   failed: 0   429(rate-limited): 7658
throughput    : 623.5 req/s
fail ratio    : 0.00%  (429 excluded)
concurrency   : max in-flight 100 / avg 90.1 (configured users=100)
------------------------------------------------------------------------------
scenario  reqs  ok    fail  429   qps    p50  p90  p95  p99   max   ttft_p95
--------  ----  ----  ----  ----  -----  ---  ---  ---  ----  ----  --------
chat      5181  622   0     4559  339.1  82   260  266  1681  3962  -
chat_sse  2579  333   0     2246  168.8  82   279  290  1422  3511  2063
healthz   871   871   0     0     57.0   72   82   86   1010  2616  -
sessions  896   43    0     853   58.6   73   84   92   2283  3587  -
ALL       9527  1869  0     7658  623.5  80   259  274  1635  3962  2063
(latency in ms)
status distribution: 200=1869, 429=7658
```

校验（服务端自报计数交叉验证）：

```
[mock-app] served=998 max_inflight=20 rejected_429=7658
```

* 客户端统计 **429 = 7658**，服务端拒绝计数 **7658**，完全一致。
* 服务端实际放行 `chat 622 + chat_sse 333 + sessions 43 = 998`，与
  服务端 `served=998` 完全一致（`/healthz` 不走并发闸门）。
* 服务端 `max_inflight = 20` = 配置上限，限流确实生效。
* **失败率 0.00%**：7658 个 429 被正确归类为"限流"而非"失败"。

结论：QPS、P50/P90/P95/P99、并发达成度、429 归类、SSE TTFT 五项统计
均经过独立交叉验证，压测器输出可信。

---

## 8. 跑完把数字填进这张表（交给用户）

> 填表前先记下环境：CPU 型号/核数、内存、Python 版本、`WORKERS=`、
> `MOCK_LLM_DELAY_MS=`、限流后端（内存 / Redis）、checkpoint 后端（SQLite / PG）。

### 8.1 L2 应用层（`MOCK_LLM=1`，主表）

| 并发 | workers | 总请求 | QPS | P50 (ms) | P90 | P95 | P99 | 失败率 | 429 数 | SSE TTFT P95 | RSS 总计 (MB) | CPU % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 10  | 4 | | | | | | | | | | | |
| 25  | 4 | | | | | | | | | | | |
| 50  | 4 | | | | | | | | | | | |
| 100 | 4 | | | | | | | | | | | |
| 200 | 4 | | | | | | | | | | | |

单 worker 对照组（证明多 worker 确实带来收益）：

| 并发 | workers | QPS | P95 (ms) | 失败率 | 相对 4 worker 的 QPS 提升 |
|---|---|---|---|---|---|
| 100 | 1 | | | | 基准 |
| 100 | 2 | | | | ×__ |
| 100 | 4 | | | | ×__ |
| 100 | 8 | | | | ×__ |

### 8.2 L1 协议层 / L3 端到端 对照

| 层次 | 配置 | 并发 | QPS | P95 | 失败率 |
|---|---|---|---|---|---|
| L1 `/healthz` only | mock | 200 | | | |
| L3 真实 35B | `MOCK_*` 全关 | 5 | | | |
| L3 真实 35B | `MOCK_*` 全关 | 10 | | | |

### 8.3 结论模板（填完抄进简历/文档）

* 拐点并发：`____`（超过它 P95 开始非线性上升 / 429 显著增加）
* 100 并发下应用层 QPS：`____`，P95：`____ ms`，失败率：`____%`
* 瓶颈定位：`____`（候选：SQLite checkpoint 写锁 / 限流闸门排队 /
  worker 数不足 / 事件循环被同步调用阻塞 / 压测机自身）
* 判据：`____`（例如 "worker 从 1→4，QPS 3.6× 近线性 → 瓶颈在 CPU 而非锁"）
* 端到端（真 35B，10 并发）P95：`____ s`，其中模型占 `____ s`（≈ 应用层 P95 之外的部分）

**写简历时的正确说法**（两句都要有，否则会被追问）：

> 基于 mock LLM 的应用层压测：4 worker 下 100 并发稳定服务，
> QPS ___、P95 ___ms、失败率 <__%，限流按预期返回 429 无 5xx 雪崩；
> 端到端（本地 35B，10 并发）P95 ___s，瓶颈在模型推理而非应用。

---

## 9. 文件清单

| 文件 | 作用 |
|---|---|
| `agent/mock_llm.py` | mock 实现（纯 stdlib，默认全关） |
| `agent/llm_client.py` / `llm_gateway.py` / `embedding_client.py` | 各 1~2 处早返回接线 |
| `scripts/loadtest/run_loadtest.py` | 零依赖压测器（httpx 优先，可降级线程池） |
| `scripts/loadtest/mock_app_server.py` | 压测器自测靶子（标准库，行为已知） |
| `scripts/loadtest/run_loadtest.bat` | Windows 一键（起服务→压测→出报告→关服务） |
| `scripts/loadtest/run_loadtest.sh` | Linux/macOS 一键 |
| `scripts/loadtest/locustfile.py` | 原 locust 脚本（未改动，可继续用） |
| `scripts/loadtest/README.md` | 原压测/混沌演练手册（真 LLM 口径） |
| `tests/test_mock_llm_pure.py` | mock 层 + 压测器统计的纯 stdlib 单测 |
