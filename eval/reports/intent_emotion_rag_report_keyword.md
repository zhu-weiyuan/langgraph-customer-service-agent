# 意图 / 情绪 / RAG 综合评测报告

- 生成时间：`2026-07-30T23:19:15+0800`
- 数据集：`C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent\eval\intent_emotion_rag_dataset.jsonl`（45 条）
- RAG 后端：`pgvector`
- 情绪评测模式：`keyword`

## 总结

- 意图准确率：**73.33%**；Macro-F1：**61.26%**
- 情绪准确率：**55.56%**；Macro-F1：**32.60%**
- RAG 正样本 Hit@1 / Hit@3 / Hit@5：**90.62% / 100.00% / 100.00%**
- RAG 正样本 MRR：**0.9531**；无关问题误命中率：**84.62%**
- RAG 二分类准确率：**75.56%**；平均检索耗时：**727.6 ms**

## 意图分类

| 标签 | 支持数 | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| chat | 7 | 85.71% | 85.71% | 85.71% |
| complaint | 8 | 0.00% | 0.00% | 0.00% |
| consult | 24 | 67.65% | 95.83% | 79.31% |
| ending | 6 | 100.00% | 66.67% | 80.00% |

## 情绪分类

| 标签 | 支持数 | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| angry | 6 | 100.00% | 16.67% | 28.57% |
| anxious | 7 | 100.00% | 28.57% | 44.44% |
| happy | 8 | 100.00% | 12.50% | 22.22% |
| neutral | 21 | 51.22% | 100.00% | 67.74% |
| sad | 3 | 0.00% | 0.00% | 0.00% |

## 运行说明

- 本轮 embedding 请求未成功，RAG 已按设计降级为 **PostgreSQL 关键词检索**；因此本报告的 Hit 率不能等同于纯向量召回效果。
- 情绪来源统计：`{"neutral-fallback": 41, "keyword": 4}`

## 失败样本

### 意图错误

| ID | 输入 | 期望 | 预测 |
|---|---|---|---|
| eval-010 | 谢谢，解决了 | ending | consult |
| eval-025 | 这个音箱质量太差了 | complaint | consult |
| eval-026 | 我要退款，买回来就坏了 | complaint | consult |
| eval-027 | 物流拖了这么久还没发货，我很生气 | complaint | consult |
| eval-028 | 太失望了，设备总是掉线 | complaint | consult |
| eval-029 | 你们售后太坑人了，我要投诉 | complaint | consult |
| eval-030 | 买了会员却不能用，气死我了 | complaint | consult |
| eval-033 | 为什么还不退款 | complaint | consult |
| eval-034 | 花了钱却不能用，真的后悔 | complaint | consult |
| eval-040 | 帮我写一首诗 | chat | consult |
| eval-041 | 谢谢 | ending | consult |
| eval-042 | 你好，我的订单还没发货 | consult | chat |

### 情绪错误

| ID | 输入 | 期望 | 预测 | 来源 |
|---|---|---|---|---|
| eval-006 | 谢谢你的帮助 | happy | neutral | neutral-fallback |
| eval-007 | 再见 | happy | neutral | neutral-fallback |
| eval-008 | 好了，没事了 | happy | neutral | neutral-fallback |
| eval-009 | 不用了谢谢 | happy | neutral | neutral-fallback |
| eval-010 | 谢谢，解决了 | happy | neutral | neutral-fallback |
| eval-016 | 忘记密码怎么找回 | anxious | neutral | neutral-fallback |
| eval-019 | 网关红灯一直亮怎么办 | anxious | neutral | neutral-fallback |
| eval-024 | 怎么申请维修 | anxious | neutral | neutral-fallback |
| eval-025 | 这个音箱质量太差了 | angry | neutral | neutral-fallback |
| eval-026 | 我要退款，买回来就坏了 | angry | neutral | neutral-fallback |
| eval-027 | 物流拖了这么久还没发货，我很生气 | angry | neutral | neutral-fallback |
| eval-028 | 太失望了，设备总是掉线 | sad | neutral | neutral-fallback |
| eval-030 | 买了会员却不能用，气死我了 | angry | neutral | neutral-fallback |
| eval-031 | 退款多久到账 | anxious | neutral | neutral-fallback |
| eval-032 | 质量问题能不能换新 | sad | neutral | neutral-fallback |
| eval-033 | 为什么还不退款 | angry | neutral | neutral-fallback |
| eval-034 | 花了钱却不能用，真的后悔 | sad | neutral | neutral-fallback |
| eval-036 | 太好了，终于连上 WiFi 了 | happy | neutral | neutral-fallback |
| eval-037 | 一直登录不上，急死我了 | anxious | neutral | neutral-fallback |
| eval-041 | 谢谢 | happy | neutral | neutral-fallback |

### RAG 未命中 / 误命中

| ID | 输入 | 期望 | rank | 返回数 |
|---|---|---|---:|---:|
| eval-001 | 你好 | 不应命中 | - | 5 |
| eval-002 | 你是谁 | 不应命中 | - | 5 |
| eval-003 | 在吗 | 不应命中 | - | 5 |
| eval-004 | 讲个笑话吧 | 不应命中 | - | 5 |
| eval-005 | 今天天气怎么样 | 不应命中 | - | 5 |
| eval-006 | 谢谢你的帮助 | 不应命中 | - | 5 |
| eval-007 | 再见 | 不应命中 | - | 5 |
| eval-008 | 好了，没事了 | 不应命中 | - | 5 |
| eval-009 | 不用了谢谢 | 不应命中 | - | 5 |
| eval-010 | 谢谢，解决了 | 不应命中 | - | 5 |
| eval-040 | 帮我写一首诗 | 不应命中 | - | 5 |
