# 普通 RAG vs Agentic RAG 真实对比报告

> 本报告的所有数字均来自**容器内真实检索**:纯 Python BM25 检索器
> (`scripts/_local_retriever.py`)在**真实知识库** `knowledge/*.md`
> (12 个文件、264 个段落)上跑出,非 mock 语料。运行脚本:
> `python3 scripts/compare_rag_local.py`。

## 1. 数据集设计与配比

分层评测集:`eval/rag_eval_layered.jsonl`,共 **48 条**,严格按业务风险分层:

| tier | 含义 | 条数 | 占比 | 权重 |
|------|------|------|------|------|
| normal | 高频、清晰的主流场景(连 WiFi、退货时长、开发票…) | 24 | 50% | 1 |
| edge | 信息缺失 / 多义 / 跨领域("它不工作了"缺主语、"网关"多义、退货+保修混问) | 12 | 25% | 1 |
| adversarial | 模型易错输入(错别字"音象脸wifi"、口语"咋整没动静"、闲聊混入、超长啰嗦、否定陷阱) | 7 | 14.6% | 1 |
| high_weight | 答错代价大的关键类型(退款折旧费、保修期限、过热/过载安全操作) | 5 | 10.4% | 3 |

每条字段:`{id, tier, category, query, golden_context_ids, expected_keywords, weight}`。

`golden_context_ids` **逐条对照真实 `knowledge/` 文件的标题结构与正文标注**
(文件名去扩展名),例如:

- `h03 音箱过热了还能继续用吗` → `error-codes`(真实存在 `## E007 音箱过热保护`)
- `h01 无理由退货会扣多少折旧费` → `returns-refunds`(`## 部分退款与折旧扣费`,正文"最高不超过实付 20%")
- `a06 付费维修怎么收费` → `warranty-service`(`## 有偿维修收费标准`)
- `e02 网关`(多义)→ `error-codes` + `installation-guide` + `troubleshooting` + `product-manual`

## 2. 评测方法

**检索器**(零外部依赖,容器无 LLM/embedding/jieba):
- 语料:按 `## ` 切节 → `{title, text, source}`;标题加权(拼 3 次)。
- 分词:中文**字符 bigram + 单字**混合,英数字整词 + 长词字符 bigram(兜错别字);不依赖 jieba。
- 打分:**BM25**(k1=1.5, b=0.75),IDF 带 +1 平滑恒正。

**两条管线**:
- **普通 RAG(plain)**:单次 BM25 检索 top_k。
- **Agentic RAG**:`rewrite()` 生成原 query + 2~3 变体(同义词表 `agent/synonyms.json`
  + 疑问词/语气词剥离 + 关键词抽取)→ 多路 BM25 召回**合并去重** →
  **命中门控**(合并结果 ≥ `min_hits` 即停)→ 不足则用提纯后的关键词串**再改写 1 轮**。

**指标**(逐条判相关性后按 tier 分组 + 总体,float 判定):
`HitRate@K / Recall@K / MRR / Precision@K`,relevance = 命中片段 `source ∈ golden_context_ids`。
加权:high_weight 权重 3 计入加权 HitRate。成本 = 平均改写次数 / 检索次数。

参数:`Top-K=5, rounds=2, min_hits=3`。

## 3. 真实运行结果(逐层对比表)

```
真实检索对比(BM25 本地检索器 · 知识库真实段落) · Top-5 · N=48
==============================================================================

[normal]  (24 条)          plain | agentic |   Δ
  HitRate@5     1.000 |  1.000 | +0.000 ==
  Recall@5      1.000 |  1.000 | +0.000 ==
  MRR           0.969 |  0.924 | -0.045 DOWN
  Precision@5   0.558 |  0.608 | +0.050 UP

[edge]  (12 条)            plain | agentic |   Δ
  HitRate@5     0.833 |  1.000 | +0.167 UP
  Recall@5      0.736 |  0.903 | +0.167 UP
  MRR           0.642 |  0.808 | +0.167 UP
  Precision@5   0.467 |  0.617 | +0.150 UP

[adversarial]  (7 条)      plain | agentic |   Δ
  HitRate@5     1.000 |  1.000 | +0.000 ==
  Recall@5      1.000 |  1.000 | +0.000 ==
  MRR           0.905 |  0.929 | +0.024 UP
  Precision@5   0.543 |  0.600 | +0.057 UP

[high_weight]  (5 条)      plain | agentic |   Δ
  HitRate@5     1.000 |  1.000 | +0.000 ==
  Recall@5      1.000 |  1.000 | +0.000 ==
  MRR           1.000 |  0.867 | -0.133 DOWN
  Precision@5   0.640 |  0.640 | +0.000 ==

==============================================================================
总体(全部 case 平均):     plain | agentic |   Δ
  HitRate@5     0.958 |  1.000 | +0.042 UP
  Recall@5      0.934 |  0.976 | +0.042 UP
  MRR           0.881 |  0.890 | +0.009 UP
  Precision@5   0.542 |  0.612 | +0.071 UP

加权 HitRate(high_weight×3, 权重总和=58):
  plain=0.966 | agentic=1.000 | Δ=+0.034 UP

成本:
  平均改写次数    plain=0.00 | agentic=1.00
  平均检索次数    plain=1.00 | agentic=3.15
```

逐条明细见 `eval/rag_local_results.csv`。

## 4. 发现

**1) Agentic RAG 的价值几乎全部集中在 edge 层。**
edge 是唯一四项指标齐涨的 tier:HitRate 0.833→1.000、Recall/MRR 各 +0.167、
Precision +0.150。原因正是这层 query 的特征——缺主语("它不工作了")、多义
("网关"/"换"/"升级")、跨领域("退货还想问保修")。单次 BM25 只能围绕字面
命中一路;而同义词扩展 + 疑问词剥离 + 多路合并,把被单一表述埋没的正确文件
召回上来。**这是"改写+多轮重检"设计真正兑现收益的地方。**

**2) normal 与 high_weight 层,plain 已经"够好",Agentic 是过度设计甚至有害。**
- normal:plain 的 HitRate/Recall 已是满分 1.000,Agentic 无从提升,反而把
  MRR 从 0.969 拉到 0.924(**-0.045**)。
- high_weight:更明显——plain MRR = **1.000**(每条都把正确文件排在第 1),
  Agentic 却降到 0.867(**-0.133**)。

原因是多变体合并后按**原始 BM25 分数重排**,某个变体会把一个分数更高但**非
golden** 的段落顶到前面,挤掉本已排第 1 的精确答案。对于"退款折旧 20%""过热
E007"这类**答错代价大**的问题,这种降级尤其危险:HitRate 没掉但答案不在首位,
生成阶段更容易引用到旁支内容。

**3) adversarial 层 plain 意外地稳。**
错别字/口语/超长啰嗦在字符 bigram 分词下仍保留了足够的部分重叠,plain HitRate
已达 1.000;Agentic 仅带来 MRR/Precision 的边际改善(+0.024 / +0.057)。说明
**健壮的分词**比"事后改写"更能扛对抗输入。

**4) 成本不对称。**
Agentic 平均每条 **3.15 次检索 + 1 次改写**,而 plain 只需 1 次检索、0 改写。
即为了在 edge 层拿到 +0.167 的 HitRate,全局多付了 ~3 倍检索开销——若无差别地
对所有 query 开启,大部分成本花在了本来就答对的 normal/high_weight 上。

## 5. 结论与建议(启发式门控依据)

**结论**:总体上 Agentic RAG 有小幅正收益(HitRate +0.042、Recall +0.042),
但收益**高度不均**:edge 层大赢,normal/adversarial 打平,high_weight 层 MRR
受损。无差别开启改写 = 用 3 倍检索成本换一层 case 的提升,并在最关键的
high_weight 层引入排名倒退风险。

**建议:用启发式门控,只在"该省则省、该战则战"时触发改写。**

1. **首选先跑一次 plain,按信号决定是否升级为 Agentic**(门控依据来自本报告数据):
   - plain top1 分数**显著高于** top2(分差大)或 top1 命中已知精确锚点
     (错误码 `E0\d+`、型号 `X-\d00`)→ 判定"检索已锐利",**直接用 plain**
     (对应 normal/high_weight:plain MRR 已 0.97~1.00)。
   - plain top-k 命中数 < `min_hits`、或 top 段落分数低/彼此接近(多义、语义发散)
     → 触发 Agentic 改写多路召回(对应 edge:这里才有 +0.167)。
2. **high_weight query 保护首位**:即使触发改写,合并后应**保留 plain 的 top1**
   或对精确锚点命中段加排序偏置,避免像本次 high_weight MRR -0.133 那样把确定
   答案挤下去。
3. **分词优先于改写对抗鲁棒性**:adversarial 层证明字符 bigram 已足够扛错别字,
   不必为对抗输入单独堆改写轮次。
4. **参数**:`min_hits=3、rounds≤2` 在本集上即可拿满 edge 收益;继续加轮数只增
   成本不增质量。

一句话:**Agentic RAG 不是"永远更好",而是"对模糊/多义/跨领域 query 更好"。
把改写做成条件触发的启发式门控,才能既拿到 edge 层的召回红利,又不牺牲
high_weight 层的首位精度和整体成本。**
