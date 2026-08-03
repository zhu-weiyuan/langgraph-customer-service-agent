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
