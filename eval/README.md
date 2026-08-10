# eval/：评测数据、指标和结果

这里专门放“怎么评测”和“评测什么”，评测执行脚本主要在 scripts/。

## 先看这几个文件

| 文件 | 作用 |
|---|---|
| intent_emotion_rag_dataset.jsonl | 意图、情绪和 RAG 综合评测数据集 |
| golden_set.jsonl | 基础黄金集 |
| rag_eval_hard.jsonl | 困难 RAG 样本 |
| harness.py | 通用评测运行器 |
| metrics.py | 纯函数指标实现 |
| EVAL_METRICS.md | 指标公式、阈值和手算示例 |
| reports/ | 评测报告输出目录（如果已经生成） |

## 推荐运行

    python scripts/run_intent_emotion_rag_eval.py
    python scripts/eval_retrieval.py --backend pgvector
    python scripts/run_eval.py --layer all --mock

## 已有一批基线结果

- 数据集：45 条。
- 意图准确率 / Macro-F1：100% / 100%。
- 情绪准确率 / Macro-F1：100% / 100%。
- RAG 正样本 Hit@1：96.88%。
- RAG Hit@3 / Hit@5：100% / 100%。
- RAG MRR：98.44%。
- 负样本误命中率：0%。
- 平均检索耗时：约 784 ms。

注意：这批综合评测的意图是快速路由层，情绪采用 keyword 模式；如果要评估完整 LLM 分类器，需要单独设计真实 LLM 评测集和 judge 规则。

## 看结果时先问三个问题

1. 是检索错了，还是生成答案没有使用正确证据？
2. 指标是否来自 pgvector 当前后端，而不是 fallback？
3. 数据集是否包含负样本、边界样本和多轮上下文？

---

## 真实链路评测 run_real_eval.py（v2，85 条）

`run_real_eval.py` 是当前主评测：真实 pgvector 检索 + 远程 rerank + 本地 LLM 生成 +
同模型 LLM-as-Judge，数据集 `golden_set_v2.jsonl`（85 条）。

### 运行

    python eval/run_real_eval.py --limit 5        # 小跑（先看费用/耗时）
    python eval/run_real_eval.py --ids id1,id2    # 只跑指定题
    python eval/run_real_eval.py --all --multi-turn   # 全量 85 条 + 多轮注入

### 评测严格性（默认开启，指标可信的前提）

- `RAG_STRICT=1`：pgvector 检索失败**抛错并记录**，而不是静默回落 TF-IDF（否则
  报告里混入降级结果，指标不可比）。每条记录 `backend_actual` / `fallback_used` /
  `fallback_reason`。
- `RAG_SEARCH_CACHE_TTL=0`：关闭检索结果缓存，避免重复/相似问题复用上一题结果。
- `--permissive` 可关闭上述严格模式（仅排查用）。

### 统计口径（2026-08-10 修正，与旧报告不直接可比）

- **拒答题（should_refuse，12 条）不计入检索/生成聚合**：旧实现空 golden 时
  Hit/MRR 硬编码 1.0，虚增了 Hit@5（README 声称的口径现在由代码落实）。
- 拒答单独报三项：**拒答正确率**（LLM judge 判定）、**误拒答率**（正常题拒答，
  保守规则启发式）、**危险配合率**（该拒却照做）。
- **Judge 解析失败不计 0 分**：parse 失败 → 该条指标为 N/A（不进均值），报告单列
  `judge_parse_failures` 计数，原始响应存 `eval/reports/judge_raw_{ts}.jsonl`
  （区分“Judge 故障”与“模型低分”）。
- 引用校验：`n` 越界（> 检索条目数）一律记为不支持，并记录
  `citation_out_of_range`；逐条记录 `citation_markers_in_answer` 与
  `citation_detail` 对照（审计发现 41/85 条数量不一致，现在可量化）。
- 每次运行记录 meta：git commit、语料 corpus hash、embedding/reranker/LLM 模型、
  RAG_STRICT/TTL、multi_turn，报告头部可溯源。

### 已知局限（步骤 2/3 处理）

- 小节级/块级 golden 命中（golden_sections / golden_chunk_ids）尚未实现——数据集
  目前只有文件级 `golden_context_ids`，rerank 的真实增益仍需同题 A/B 才能量化。
- 误拒答率为规则启发式，可能有误报；judge 侧对超长上下文的截断导致部分
  CP/CR 解析失败（已可见、可计数）。
- `--all` 默认跑 v2 85 条；旧 `golden_set.jsonl`（63 条）需 `--dataset` 显式指定。
