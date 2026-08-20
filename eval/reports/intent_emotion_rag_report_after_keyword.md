# 意图 / 情绪 / RAG 综合评测报告

- 生成时间：`2026-07-30T23:37:40+0800`
- 数据集：`C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent\eval\intent_emotion_rag_dataset.jsonl`（45 条）
- RAG 后端：`pgvector`
- 情绪评测模式：`keyword`

## 总结

- 意图准确率：**97.78%**；Macro-F1：**97.57%**
- 情绪准确率：**88.89%**；Macro-F1：**81.52%**
- RAG 正样本 Hit@1 / Hit@3 / Hit@5：**90.62% / 100.00% / 100.00%**
- RAG 正样本 MRR：**0.9531**；无关问题误命中率：**38.46%**
- RAG 二分类准确率：**88.89%**；平均检索耗时：**647.9 ms**

## 意图分类

| 标签 | 支持数 | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| chat | 7 | 100.00% | 85.71% | 92.31% |
| complaint | 8 | 100.00% | 100.00% | 100.00% |
| consult | 24 | 96.00% | 100.00% | 97.96% |
| ending | 6 | 100.00% | 100.00% | 100.00% |

## 情绪分类

| 标签 | 支持数 | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| angry | 6 | 100.00% | 83.33% | 90.91% |
| anxious | 7 | 75.00% | 85.71% | 80.00% |
| happy | 8 | 100.00% | 87.50% | 93.33% |
| neutral | 21 | 87.50% | 100.00% | 93.33% |
| sad | 3 | 100.00% | 33.33% | 50.00% |

## 运行说明

- 本轮 embedding 请求未成功，RAG 已按设计降级为 **PostgreSQL 关键词检索**；因此本报告的 Hit 率不能等同于纯向量召回效果。
- 情绪来源统计：`{"neutral-fallback": 24, "keyword": 21}`

## 失败样本

### 意图错误

| ID | 输入 | 期望 | 预测 |
|---|---|---|---|
| eval-005 | 今天天气怎么样 | chat | consult |

### 情绪错误

| ID | 输入 | 期望 | 预测 | 来源 |
|---|---|---|---|---|
| eval-007 | 再见 | happy | neutral | neutral-fallback |
| eval-024 | 怎么申请维修 | anxious | neutral | neutral-fallback |
| eval-030 | 买了会员却不能用，气死我了 | angry | anxious | keyword |
| eval-032 | 质量问题能不能换新 | sad | neutral | neutral-fallback |
| eval-034 | 花了钱却不能用，真的后悔 | sad | anxious | keyword |

### RAG 未命中 / 误命中

| ID | 输入 | 期望 | rank | 返回数 |
|---|---|---|---:|---:|
| eval-001 | 你好 | 不应命中 | - | 3 |
| eval-005 | 今天天气怎么样 | 不应命中 | - | 2 |
| eval-006 | 谢谢你的帮助 | 不应命中 | - | 2 |
| eval-009 | 不用了谢谢 | 不应命中 | - | 2 |
| eval-040 | 帮我写一首诗 | 不应命中 | - | 2 |
