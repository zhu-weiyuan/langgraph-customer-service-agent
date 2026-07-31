# 真实端到端评测系统 · EVAL_REAL

> 检索层调**真实 embedding + hybrid/pgvector 检索**;生成层用**真实 LLM 生成**,再用
> **另一次 LLM 调用当 Judge** 打分。跑在你自己的机器上(有真实 LLM/embedding/pgvector)。

相关文件:

| 文件 | 作用 |
|------|------|
| `scripts/eval_real.py` | 真实评测主脚本(检索 + 生成 + LLM-as-Judge + plain vs agentic 对比) |
| `eval/rag_eval_hard.jsonl` | 加难评测集(92 条,对照真实 KB 标注) |
| `scripts/_gen_hard_eval.py` | 评测集“源码”(改语料后重跑它再生成 jsonl) |
| `eval/EVAL_REAL_README.md` | 本文档 |

---

## ⚠️ 这是**真实**评测,会产生费用

真实模式下每条题会调用:

- **embedding**:hybrid/pgvector 检索时对每个查询做查询嵌入(plain 1 次;agentic 因改写多变体 + 多轮会更多);
- **生成 LLM**:plain 每条 1 次;agentic 每条 = 改写(1) + 可选评估(0/1) + 生成(1);
- **Judge LLM**:每条 pointwise 打分 plain/agentic 各 1 次 + 成对比较 2 次(交换顺序去偏)。

> 一条题(mode=both)大约 **6~9 次 LLM 调用**。跑全量 92 条前,**务必先用 `--limit` 小跑看费用**。

`--mock` 模式**不产生任何费用**(注入假 retriever/llm/judge),仅用于在容器/无依赖环境验证脚本逻辑。

---

## 怎么跑(三步走,从零费用到全量)

### 1) 先在无依赖环境验证逻辑(零费用)

```cmd
python scripts\eval_real.py --mock --mode both --limit 5
```

看到分层报告 + 成本表正常打印,说明脚本逻辑通。`--mock` 会打印每一步假调用记录,
结构与真实模式一致。

### 2) 真实小跑,先看费用(强烈建议 5~10 条起步)

```cmd
:: 配好 .env(OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL / EMBEDDING_*),再选后端
set RAG_BACKEND=hybrid
python scripts\eval_real.py --mode both --limit 8
```

脚本每次 LLM 调用都会打印一行(模型、in/out token、耗时),**可与你的 LLM 后台调用记录逐条对上**。
成本表末尾给出被评(gen)总 token 与 Judge 总 token,据此估算全量费用(≈ 单条成本 × 92)。

### 3) 确认费用可接受后再全量 / 分层跑

```cmd
python scripts\eval_real.py --mode both --backend pgvector --csv real_result.csv
python scripts\eval_real.py --mode retrieval --backend hybrid          :: 只检索,便宜
python scripts\eval_real.py --mode generation --tier high --judge-model gpt-4o-mini
```

### 命令行参数

| 参数 | 说明 |
|------|------|
| `--mode retrieval\|generation\|both` | 只评检索 / 只评生成 / 两者(默认 both) |
| `--backend tfidf\|hybrid\|pgvector` | 覆盖环境变量 `RAG_BACKEND` |
| `--limit N` | 只跑前 N 条(**省钱,先小跑!**) |
| `--tier normal\|edge\|adversarial\|high` | 只跑某一层 |
| `--k` | Top-K(默认 5) |
| `--judge-model` | 指定裁判模型(不填=与生成同模型;建议用**不同/更强**模型当裁判) |
| `--csv PATH` | 导出逐条结果 |
| `--mock` | 注入假实现,容器验证逻辑,零费用 |
| `--quiet` | 不打印每次 LLM 调用(只看汇总) |

> `--mode retrieval` 只做检索(agentic 仍需 LLM 改写),**不生成、不 Judge**,是最省钱的观测方式。

---

## 指标怎么读

### 检索层(文件级 + 小节级,分开报)

同一批 query 对 **plain RAG** 与 **Agentic RAG** 各算一组,并在**两个粒度**分别判定命中:

- **文件级(golden_context_ids)** — 命中 `hit["source"]`(文件 stem)即算相关。衡量“找对文件没有”。
- **小节级(golden_section)** — 命中 `hit["title"]`(小节标题)才算相关。衡量“找对**那一节**没有”
  —— 这一级才真正暴露 **rerank / 精排** 的价值:找对文件很容易,找对小节难。

| 指标 | 含义 | 方向 |
|------|------|------|
| HitRate@K | Top-K 里至少 1 条相关 | 越高越好 |
| Recall@K | 相关目标被 Top-K 覆盖的比例 | 越高越好 |
| MRR | 第一条相关结果排名的倒数 | 越高越好 |
| Precision@K | Top-K 里相关占比 | 越高越好 |

拒答题(should_refuse)无 golden 目标,**不计入检索聚合**。

### 生成层(LLM-as-Judge)

用真实 LLM 生成答案后,由**另一次 LLM 调用**结构化 JSON 打分:

| 指标 | 含义 | 方向 |
|------|------|------|
| Faithfulness | 逐句核查:被检索上下文支撑的句子比例 | 越高越好 |
| Answer Relevance | 是否切题回答了用户问题 | 越高越好 |
| Completeness | reference_answer 要点覆盖度 | 越高越好 |
| 幻觉率 | = 1 − Faithfulness,无依据断言的平均比例 | 越低越好 |
| 拒答正确性 | should_refuse 题是否正确拒答(确定性判定,不花 Judge) | 越高越好 |
| 误拒答率 | 正常题却拒答的比例(辅助信号,规则判定) | 越低越好 |
| 成对偏好 | plain vs agentic 谁更好(去偏后 agentic胜/plain胜/平) | 看 agentic 是否更优 |

### 成本

真实 token(取 API `usage`,缺失时按中文 ≈1.5 char/token 估算)、LLM 调用数、检索次数、延迟,
**plain 与 agentic、被评(gen)与 Judge 全部分开记录**,直观呈现“质量 vs 成本”权衡:agentic
通常检索/生成更好,但多花改写+多轮+(可能)评估的 LLM 调用。

---

## Judge 偏差治理

1. **裁判与被评分离**:生成用 gen 模型,打分用 judge 模型;`--judge-model` 可指定不同(更强)模型,
   调用数与 token **分列**,不混淆成本归因。
2. **结构化输出**:Judge 走固定 system prompt + JSON schema(faithfulness / unsupported_claims /
   answer_relevance / completeness),temperature=0,解析失败时该条标 `judge_ok=false` 优雅降级。
3. **位置偏差治理(成对比较)**:plain vs agentic 比较时**交换顺序评两次**,两次结论一致且非平局才判胜负,
   否则记平局 —— 复用 `eval/metrics.pairwise_judge_debiased` / `shadow_eval` 同款模式,消解“LLM 偏爱前一个”的位置偏差。
4. **逐句判**:Faithfulness 要求 Judge 逐句核查并列出无依据断言(unsupported_claims),而非笼统给分。

---

## 为什么之前 BM25 离线评测“正常层满分”,而这个能拉开差距?

之前的离线检索评测(`scripts/compare_rag_local.py` 等)在**正常层**里,query 的用词和知识库
高度重合(问“音箱怎么连WiFi”,KB 标题就写着“WiFi 配网”),**纯 BM25/TF-IDF 靠字面重叠就能满分**,
于是 hybrid(BM25+向量)相比纯 BM25 看不出差别 —— 不是 hybrid 没用,是**题目太浅、没触发向量的价值**。

本评测集专门加了 **27 条“语义鸿沟题”(sg)**:query 用词与知识库**完全不同**,字面零重叠:

| 用户怎么问(query) | 知识库怎么写(golden_section) |
|--------------------|------------------------------|
| “音箱**不吭声**了,一点**动静**都没有” | 音箱**没声音/不出声/静音**了 |
| “智能灯**连不上**那个**网关**” | Zigbee 设备**配对失败** |
| “客厅那个**小盒子红灯**一直亮” | **网关离线**/掉线 |
| “监控画面**卡成幻灯片**” | 摄像头远程画面**延迟高**/打不开 |
| “老提示**存不下**了,录像**传不上去**” | 云存储**空间不足** |

这类题上 **BM25 必挂**(没有共同词),**只有向量语义检索能对** —— 于是 hybrid/pgvector 相对纯 BM25
的差距被放大,评测才有区分度。此外还叠加了:

- **细粒度定位**:golden 标到具体**小节标题**,要求命中对的那一节而非只对文件 → 测精排;
- **多跳推理(6 条)**:答案需综合 2 个小节(如“X-300 Pro 过保了修要多少钱”= 保修期限总表 + 有偿维修收费标准);
- **对抗层**:中英混输(speaker/sound、gateway/offline)、大量错别字(音想=音箱、往关=网关)、
  反问句(“你们是不是不支持公积金支付”)、否定陷阱、多轮指代(“那它呢”)、超纲拒答题。

> 结论:离线 BM25 正常层满分是**题目效应**,不代表检索到顶。要衡量 hybrid/精排的真实增益,
> 必须用**语义鸿沟题 + 小节级 golden + 真实向量检索**,这正是本系统存在的意义。

---

## 评测集结构(`eval/rag_eval_hard.jsonl`,92 条)

四层配比:**正常 50%(46) / 边缘 25%(23) / 对抗 15%(14) / 高权重 10%(9)**。

每条字段:

```json
{
  "id": "n01", "tier": "normal", "category": "语义鸿沟-音箱无声",
  "query": "音箱突然不吭声了，一点动静都没有",
  "golden_context_ids": ["troubleshooting", "error-codes"],   // 文件级 golden
  "golden_section": ["音箱没声音/不出声/静音了"],              // 小节级 golden(测精排)
  "expected_keywords": ["音量", "静音", "扬声器"],
  "reference_answer": "先确认音量没被调到0…重启后仍无声可能是扬声器故障(E004)需报修。",
  "should_refuse": false, "weight": 1.0,
  "sg": true, "multi_hop": false                              // 附加标注:语义鸿沟 / 多跳
}
```

统计:语义鸿沟题 **27**、多跳题 **6**、拒答题 **4**、覆盖全部 **12** 个 KB 文件。改语料后
重跑 `python scripts\_gen_hard_eval.py` 会重新校验并打印配比。
