# 分层评测指标速查表（面试可背版）

客服 Agent 的评测分四层：**检索 → 生成 → Agent → 工程**。每层回答一个不同的问题：

| 层 | 回答的问题 | 坏了会怎样 |
|----|-----------|-----------|
| 检索 Retrieval | 该找到的证据找到了吗、排得靠前吗 | 上下文缺失/靠后 → 生成没料可用 |
| 生成 Generation | 答案忠实、切题、完整、用了上下文吗 | 幻觉 / 答非所问 / 漏答 |
| Agent | 选对工具、传对参、能收尾、扛得住重试吗 | 乱调用 / 参数错 / 卡死 / 不稳定 |
| 工程 Engineering | 输出结构合法、延迟/成本/重试可控吗 | 解析失败 / 尾延迟爆炸 / 拒答异常 |

所有指标在 `eval/metrics.py` 中都是**纯函数**，可手算验证；涉及 LLM/embedding 的
用 `judge_fn` / `embed_fn` **注入**，缺省走**规则降级**（关键词命中 / token 重叠 /
长度启发式）。目标阈值定义在 `eval/harness.py: TARGETS`（`dir=higher` 越高越好，
`dir=lower` 越低越好）。

---

## 1. 检索层 Retrieval
输入统一：`retrieved`=按 rank 高→低的 doc_id 列表；`relevant`=golden 相关 doc_id 集合。

| 指标 | 公式 | 怎么读 | 目标 |
|------|------|--------|------|
| **Recall@k** | `|相关∩top-k| / |相关|` | 该找到的召回了多少 | ≥0.85 |
| **HitRate@k** | top-k 里有≥1 个相关 → 1 否则 0 | 至少命中一条没 | ≥0.90 |
| **MRR** | `1 / rank(第一个相关)` | 第一个对的排多前 | ≥0.80 |
| **Precision@k** | `|相关∩top-k| / k` | 返回结果里有多少是相关的（噪声） | ≥0.30\* |
| **Context Precision** | `Σ_k[Precision@k·rel_k] / 命中相关数`（AP 形式） | 相关块是否尽量排前 | ≥0.80 |
| **Context Recall** | 参考答案要点被上下文覆盖的比例 | 上下文够不够回答 | ≥0.85 |

\* Precision@k 上限被 `|相关|/k` 天然压制：单相关文档 + k=3 时最高 0.33，故阈值定低。
Context Recall 规则降级=要点关键词子串命中；可注入 `judge_fn(point, ctx)->bool`。

**手算例**：`retrieved=[A,B,C]`, `relevant={B}` → Recall@3=1，Precision@3=1/3，MRR=1/2，
Context Precision=（B 在第 2 位）(1/2)/1=0.5。

---

## 2. 生成层 Generation
输入：`query, answer, contexts, key_points`。拒答题单独用 **refusal_correctness** 衡量。

| 指标 | 公式 / 做法 | 怎么读 | 目标 |
|------|-----------|--------|------|
| **Faithfulness** | 有 context 支撑的句子 / 全部句子（句子级） | 忠实度；`1-它`=幻觉率 | ≥0.90 |
| **Answer Relevance** | judge 打分 / embed 余弦 / 内容词覆盖率 | 有没有答到点上 | ≥0.80 |
| **Completeness** | 参考要点被答案覆盖的比例 | 有没有漏答 | ≥0.80 |
| **Context Usage** | `|tokens(ans)∩tokens(ctx)| / |tokens(ans)|` | 有没有真用检索结果 | ≥0.50 |
| **Noise Sensitivity** | `max(0, 干净答案分 − 注噪答案分)`（对比实验） | 抗噪；越低越稳 | ≤0.15 |
| **Refusal Correctness** | 越权/危险题该拒答且确实拒答的比例 | 对抗安全 | ≥0.90 |

**规则降级**：Faithfulness 句子 token 被 context 覆盖 ≥ 阈值(0.6)即“有支撑”；
Answer Relevance 去停用词后比内容词覆盖率，命中拒答封顶 0.3。
**LLM judge**：`faithfulness(judge_fn=句子级 YES/NO)`、`answer_relevance(judge_fn=0-5 分)`。
**位置偏差治理**：成对比较用 `pairwise_judge_debiased`——交换 A/B 顺序评两次，
两次结论一致且非平局才判胜负（复用 shadow_eval 模式）。

**手算例**：answer 3 句，2 句被 context 覆盖 → Faithfulness=2/3；幻觉率=1/3。

---

## 3. Agent 层
输入：`trajectory`=实际工具序列 `[{"tool","args","ok"}]`；`expected`=期望序列。

| 指标 | 公式 | 怎么读 | 目标 |
|------|------|--------|------|
| **Tool Selection Accuracy** | `|期望工具∩实际工具| / |期望工具|` | 选对工具没 | ≥0.90 |
| **Parameter Accuracy** | 匹配到的工具里参数正确的比例 | 传对参没 | ≥0.85 |
| **Unnecessary Call Rate** | 多余调用 / 实际调用总数 | 画蛇添足；越低越好 | ≤0.10 |
| **Task Completion Rate** | 成功完成的 case / 总 case | 做没做成 | ≥0.85 |
| **Error Recovery Rate** | 途中有工具失败仍完成的比例 | 遇错韧性 | ≥0.70 |
| **Avg Turns / Avg Tool Calls** | 平均对话轮次 / 平均工具次数 | 效率（越低越省） | 观测 |
| **Agent Stability** | pass@N：跑 N 次至少 1 次成功 | 能不能做成（下界） | ≥0.95 |
| **Consecutive Success Rate** | all@N：跑 N 次全成功 | 可靠性（上界） | ≥0.80 |

**手算例**：期望工具 `{lookup, refund}`，实际 `[lookup, refund, extra]` →
Tool Selection=2/2=1，Unnecessary=1/3=0.33。

---

## 4. 工程层 Engineering
输入：运行记录（原始输出串 / 延迟 / token / 重试）。

| 指标 | 公式 / 做法 | 怎么读 | 目标 |
|------|-----------|--------|------|
| **JSON 合法率** | 能 `json.loads` 的输出 / 总数 | 结构化输出稳不稳 | ≥0.98 |
| **Schema 通过率** | 含全部必填键的对象 / 总数（逐 case 用自身 schema） | 字段契约达标 | ≥0.95 |
| **枚举准确率** | 落在合法枚举内的取值 / 总数 | 分类/状态字段越界没 | ≥0.95 |
| **TTFT** | 首 token 时延 mean/p50/p90/p95/p99 | 响应“开口”快不快 | 观测 |
| **E2E Latency** | 端到端时延同上分位 | 整体体验；看 p95/p99 尾部 | 观测 |
| **Input/Output Tokens** | 输入/输出 token 总量与均值 | 成本与上下文预算 | 观测 |
| **重试率** | 发生过重试的 case / 总数 | 底层调用稳不稳 | ≤0.20 |
| **拒答率** | 命中拒答标记的回答 / 总数 | 结合语境读（对抗题拒答对） | 观测 |
| **幻觉率** | `mean(1 − faithfulness)`（仅有上下文的非拒答题） | 无依据断言比例；越低越好 | ≤0.10 |
| **格式遵循率** | 匹配指定正则/模板的输出 / 总数 | 受控格式遵循度 | ≥0.95 |

TTFT / Latency / Tokens 是**观测型**（出分布不设硬门槛），用 p95/p99 盯尾部。

---

## 怎么用
```bash
python scripts/run_eval.py --layer all --mock          # 纯规则跑通四层
python scripts/run_eval.py --layer generation --judge  # 接 LLM judge
```
* 每次 run 落 `eval/eval_results.db`，带归因字段：
  `prompt_version / model_id / dataset_version / input_hash / git_commit`，
  第二次起自动出与上次基线的 `Δ` 对比。
* 指标总数：见 `eval/metrics.py: metric_count()`（注册表 `METRIC_GROUPS`）。

## 一句话记忆
> 检索管「找得到、排得前」，生成管「不瞎编、答到点、不漏、用了料、扛噪」，
> Agent 管「选对工具、传对参、能收尾、扛重试、稳」，工程管「结构合法、尾延迟、
> 成本、重试、格式」。
