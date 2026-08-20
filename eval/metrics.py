# -*- coding: utf-8 -*-
"""
分层评测指标库 —— 纯函数实现（zero third-party import，stdlib only）。

四层，每层一组指标，每个指标一个纯函数：签名清晰、可手算验证、docstring
写明公式 / 适用场景 / 怎么读。所有涉及 LLM / embedding 的指标都以
``judge_fn`` / ``embed_fn`` 注入，缺省走规则降级（关键词 / token 重叠 /
长度启发式），因此在无任何三方依赖的环境里也能整套跑通。

分组
────
1. 检索层  retrieval : recall_at_k / hit_rate_at_k / mrr / precision_at_k /
                       context_precision / context_recall
2. 生成层  generation: faithfulness / answer_relevance / completeness /
                       context_usage / noise_sensitivity
3. Agent 层 agent    : tool_selection_accuracy / parameter_accuracy /
                       unnecessary_call_rate / task_completion_rate /
                       error_recovery_rate / avg_turns / avg_tool_calls /
                       agent_stability / consecutive_success_rate
4. 工程层  engineering: json_validity_rate / schema_pass_rate / enum_accuracy /
                       latency_stats / token_stats / retry_rate / refusal_rate /
                       hallucination_rate / format_following_rate

Judge 位置偏差治理：pairwise_judge_debiased 复用 shadow_eval 的“交换顺序评两
次、结论一致才判胜负”模式。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence

# ─────────────────────────────────────────────────────────────────────
# 通用小工具（分词 / 集合 / 统计）—— 保持确定性，便于手算验证
# ─────────────────────────────────────────────────────────────────────

# 拒答标记（与 shadow_eval 对齐）

_CJK = r"一-鿿"
_TOKEN_RE = re.compile(r"[a-z0-9]+|[" + _CJK + r"]")


def tokenize(text: str) -> List[str]:
    """确定性分词：ASCII 词整体保留（小写），每个 CJK 字符单独成 token。

    用于所有 token-overlap 类指标，保证结果可手算复现。
    """
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _token_set(text: str) -> set:
    return set(tokenize(text))


# 高频功能词 / 停用词（中英）——用于 answer_relevance 只比内容词，降低噪声
STOPWORDS = set(
    "怎么吗呢啊呀吧的了和与及或请帮我你他她它们是不有个这那哪几多久什如何"
    "样能可以要想会下一也都就还在把被给到过着为对于之其此该们么当各每某"
) | {
    "the", "a", "an", "is", "are", "am", "do", "does", "i", "you", "he", "she",
    "it", "we", "they", "how", "what", "please", "me", "my", "to", "of", "and",
    "or", "for", "in", "on", "can", "could", "would", "should", "will",
}


def _content_token_set(text: str) -> set:
    return {t for t in tokenize(text) if t not in STOPWORDS}


def overlap_ratio(a: str, b: str) -> float:
    """|tokens(a) ∩ tokens(b)| / |tokens(a)| —— a 的 token 有多少被 b 覆盖。"""
    ta = _token_set(a)
    if not ta:
        return 0.0
    tb = _token_set(b)
    return len(ta & tb) / len(ta)


def mean(values: Sequence[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def percentile(values: Sequence[float], p: float) -> float:
    """线性插值分位数，p ∈ [0,100]。空输入返回 0.0。"""
    vals = sorted(values)
    if not vals:
        return 0.0
    if len(vals) == 1:
        return float(vals[0])
    rank = (p / 100.0) * (len(vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(vals) - 1)
    frac = rank - lo
    return float(vals[lo] + (vals[hi] - vals[lo]) * frac)


def split_sentences(text: str) -> List[str]:
    """按中英文句末标点 + 换行切句，用于句子级 / 断言级指标。"""
    if not text:
        return []
    parts = re.split(r"[。！？!?\n;；]+", text)
    return [p.strip() for p in parts if p.strip()]


# ── 拒答判定 ────────────────────────────────────────────────────────
# 旧实现是"命中任一关键词就算拒答",而 "无法/不能/建议咨询" 在**正常**回答里
# 极常见("如果仍无法解决,建议联系人工客服"),导致误拒答率虚高到 1.000。
# 现在改成:整句级正则 + 只看开头(拒答一定开门见山)+ 长答案有实质内容则免判。
_REFUSAL_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"(抱歉|很遗憾|不好意思)[^。!!\n]{0,30}(无法|不能|不提供|没有找到|没有相关|不清楚)",
    r"无法(回答|提供|解答|查询到|找到|获取)",
    r"不能(回答|提供|解答)",
    r"(没有|未能?)(找到|查到|检索到)[^。\n]{0,8}(信息|内容|资料|记录|依据)",
    r"(知识库|资料|文档)(中|里)?(没有|未)(相关|提及)",
    r"不属于[^。\n]{0,15}(范围|领域|业务|服务)",
    r"(不在|超出)[^。\n]{0,15}(范围|能力)",
    r"(只能|仅能|只负责)(回答|处理|提供|解答)",
    r"(让我们|我们)?回到[^。\n]{0,10}(话题|问题|正题)",
    r"i\s+(cannot|can'?t|am unable to|don'?t have)",
    r"sorry,?\s+i",
)]
# 实质内容信号:分步骤、排查动作等。有这些说明答案在真正解决问题。
_SUBSTANCE_RE = re.compile(
    r"(^|\n)\s*(\d+[.、)]|[-*•])|步骤|请检查|请确认|重启|重新启动|长按|按下|"
    r"设置为|切换到|拔掉|插好|恢复出厂|升级固件|第[一二三四五六七八]步")
_REFUSAL_HEAD = 120        # 拒答一定出现在开头
_SUBSTANCE_MIN_LEN = 200   # 超过这个长度且有实质内容,开头的客套不算拒答


def _substance_score(a: str) -> int:
    """实质内容信号个数(分步骤、排查动作等)。>=2 视为真的在解决问题。"""
    return len(_SUBSTANCE_RE.findall(a))


def is_refusal(answer: str) -> bool:
    """是否为拒答。空答案算拒答;开头命中拒答句式算拒答,
    但"给出了多条实质排查步骤"视为正常回答(开头可能只是礼貌性铺垫)。"""
    a = (answer or "").strip()
    if not a:
        return True
    if any(p.search(a[:_REFUSAL_HEAD]) for p in _REFUSAL_PATTERNS):
        # 有 2 个以上实质动作信号 → 是"先说声抱歉再认真解决",不算拒答
        return not (_substance_score(a) >= 2)
    # 开头没命中:只有短答案才整体再扫一遍(长答案里的"无法"多是条件从句)
    if len(a) < _SUBSTANCE_MIN_LEN and any(p.search(a) for p in _REFUSAL_PATTERNS):
        return _substance_score(a) < 2
    return False


def keyword_hit_ratio(text: str, keywords: Sequence[str]) -> float:
    """关键词命中率 = 命中数 / 关键词数。无关键词记 1.0（无约束）。"""
    kws = [k for k in keywords if k]
    if not kws:
        return 1.0
    text_low = (text or "").lower()
    hits = sum(1 for k in kws if k.lower() in text_low)
    return hits / len(kws)


# ═════════════════════════════════════════════════════════════════════
# 1) 检索层  RETRIEVAL
#    输入统一：retrieved = 按 rank 从高到低排列的 doc_id 列表；
#             relevant  = golden 相关 doc_id 集合 / 列表。
# ═════════════════════════════════════════════════════════════════════

def recall_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Recall@k = |relevant ∩ top-k(retrieved)| / |relevant|。

    公式：命中的相关文档数 / 全部相关文档数。
    适用：衡量“该找到的有没有被找到”，召回覆盖度。越高越好。
    边界：relevant 为空返回 1.0（无东西可召回视为满分）。
    """
    rel = set(relevant)
    if not rel:
        return 1.0
    topk = set(retrieved[:max(0, k)])
    return len(rel & topk) / len(rel)


def hit_rate_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """HitRate@k = 1.0 如果 top-k 里至少有一个相关文档，否则 0.0。

    适用：只关心“有没有命中至少一条”，问答类检索常用。
    """
    rel = set(relevant)
    if not rel:
        return 1.0
    return 1.0 if rel & set(retrieved[:max(0, k)]) else 0.0


def mrr(retrieved: Sequence[str], relevant: Sequence[str]) -> float:
    """MRR = 1 / rank(第一个相关文档)，rank 从 1 计；无命中记 0.0。

    适用：强调“第一个正确答案排多前”，排序质量。
    手算例：relevant 在第 2 位 → 1/2 = 0.5。
    """
    rel = set(relevant)
    for idx, doc in enumerate(retrieved):
        if doc in rel:
            return 1.0 / (idx + 1)
    return 0.0


def precision_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Precision@k = |relevant ∩ top-k| / k。

    适用：衡量返回结果里“有多少是相关的”，噪声控制。
    边界：k<=0 返回 0.0。
    """
    if k <= 0:
        return 0.0
    rel = set(relevant)
    topk = retrieved[:k]
    if not topk:
        return 0.0
    hits = sum(1 for d in topk if d in rel)
    return hits / k


def context_precision(retrieved: Sequence[str], relevant: Sequence[str]) -> float:
    """Context Precision（rank 加权）= Σ_k [Precision@k · rel_k] / |命中相关数|。

    含义：相关上下文是否尽量排在前面。每命中一个相关文档，用它所在位置的
    Precision@k 加权，越靠前贡献越大（平均精度 AP 的形式）。
    适用：RAG 上下文排序质量；越高说明相关块越靠前。
    边界：无命中返回 0.0。
    手算例：retrieved=[R,N,R], relevant={R}...见测试。
    """
    rel = set(relevant)
    if not rel:
        return 1.0
    hit_count = 0
    weighted = 0.0
    for i, doc in enumerate(retrieved):
        if doc in rel:
            hit_count += 1
            weighted += hit_count / (i + 1)  # Precision@(i+1) 的相关项贡献
    if hit_count == 0:
        return 0.0
    return weighted / hit_count


def context_recall(
    reference_points: Sequence[str],
    contexts: Sequence[str],
    judge_fn: Optional[Callable[[str, str], bool]] = None,
) -> float:
    """Context Recall = 参考答案要点被检索上下文覆盖的比例。

    规则降级：要点关键词（子串）出现在任一 context 即算覆盖。
    LLM 可选：judge_fn(point, joined_context) -> bool 覆盖与否。
    适用：检索到的上下文是否“足够回答参考答案”。越高越好。
    边界：无要点返回 1.0。
    """
    points = [p for p in reference_points if p]
    if not points:
        return 1.0
    joined = "\n".join(contexts or [])
    covered = 0
    for pt in points:
        if judge_fn is not None:
            if judge_fn(pt, joined):
                covered += 1
        elif pt.lower() in joined.lower():
            covered += 1
    return covered / len(points)


# ═════════════════════════════════════════════════════════════════════
# 2) 生成层  GENERATION
#    输入：query, answer, contexts(list[str]), reference / key_points
# ═════════════════════════════════════════════════════════════════════

def _claim_supported(claim: str, joined_context: str, threshold: float) -> bool:
    """规则降级：断言 token 有 >= threshold 比例被 context 覆盖即算有支撑。"""
    return overlap_ratio(claim, joined_context) >= threshold


def faithfulness(
    answer: str,
    contexts: Sequence[str],
    judge_fn: Optional[Callable[[str, str], bool]] = None,
    support_threshold: float = 0.6,
) -> float:
    """Faithfulness = 有 context 支撑的断言数 / 全部断言数（句子级）。

    步骤：把 answer 切成句子（≈断言），逐句判断是否被 context 支撑。
    规则降级：句子与拼接 context 的 token 覆盖率 >= support_threshold 即支撑。
    LLM 可选：judge_fn(sentence, joined_context) -> bool。
    适用：幻觉检测的正面度量，越高越忠实。1 - faithfulness = 幻觉比例。
    边界：answer 无可切句子返回 1.0（无断言即无幻觉）。
    """
    sentences = split_sentences(answer)
    if not sentences:
        return 1.0
    joined = "\n".join(contexts or [])
    supported = 0
    for s in sentences:
        if judge_fn is not None:
            ok = bool(judge_fn(s, joined))
        else:
            ok = _claim_supported(s, joined, support_threshold)
        supported += 1 if ok else 0
    return supported / len(sentences)


def answer_relevance(
    query: str,
    answer: str,
    judge_fn: Optional[Callable[[str, str], float]] = None,
    embed_fn: Optional[Callable[[str], Sequence[float]]] = None,
) -> float:
    """Answer Relevance = 答案是否真的回答了问题，[0,1]。

    优先级：judge_fn(query, answer)->[0,1] > embed 余弦相似 > 规则降级。
    规则降级：query 内容 token 被 answer 覆盖的比例；若答案是（非预期）拒答
             则封顶 0.3。
    适用：答非所问 / 空泛回答检测。越高越相关。
    """
    if judge_fn is not None:
        return float(max(0.0, min(1.0, judge_fn(query, answer))))
    if embed_fn is not None:
        return cosine_similarity(embed_fn(query), embed_fn(answer))
    # 规则降级：只比“内容词”（去停用词）的覆盖率，减少功能词噪声
    q = _content_token_set(query)
    if not q:
        q = _token_set(query)
    if not q:
        return 0.0
    a = _token_set(answer)
    score = len(q & a) / len(q)
    if is_refusal(answer):
        score = min(score, 0.3)
    return score


def completeness(answer: str, key_points: Sequence[str],
                 judge_fn: Optional[Callable[[str, str], bool]] = None) -> float:
    """Completeness = 参考答案关键要点被答案覆盖的比例。

    规则降级：要点关键词子串出现在 answer 即覆盖。
    LLM 可选：judge_fn(point, answer) -> bool。
    适用：漏答检测。越高越完整。无要点返回 1.0。
    """
    points = [p for p in key_points if p]
    if not points:
        return 1.0
    covered = 0
    for pt in points:
        if judge_fn is not None:
            covered += 1 if judge_fn(pt, answer) else 0
        elif pt.lower() in (answer or "").lower():
            covered += 1
    return covered / len(points)


def context_usage(answer: str, contexts: Sequence[str]) -> float:
    """Context Usage = 答案 token 中来自检索上下文的比例。

    公式：|tokens(answer) ∩ tokens(contexts)| / |tokens(answer)|。
    适用：判断生成是否真的用了检索结果（而非凭空作答 / 忽略 context）。
    读法：太低→没用上下文（可能幻觉或检索白做）；太高（≈1）也可能是照抄。
    边界：answer 为空返回 0.0。
    """
    ans = _token_set(answer)
    if not ans:
        return 0.0
    ctx = _token_set("\n".join(contexts or []))
    return len(ans & ctx) / len(ans)


def noise_sensitivity(
    clean_answer: str,
    noisy_answer: str,
    key_points: Sequence[str],
    judge_fn: Optional[Callable[[str, str], bool]] = None,
) -> float:
    """Noise Sensitivity = 注入无关上下文后答案质量的下降量（对比实验）。

    做法：分别用 completeness(要点覆盖) 给 clean / noisy 答案打分，
         sensitivity = max(0, score_clean - score_noisy)。
    读法：0 = 抗噪（注入噪声无影响）；越大 = 越容易被无关上下文带偏。越低越好。
    """
    s_clean = completeness(clean_answer, key_points, judge_fn)
    s_noisy = completeness(noisy_answer, key_points, judge_fn)
    return max(0.0, s_clean - s_noisy)


# ─── embedding / judge 辅助 ──────────────────────────────────────────

def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度，裁剪到 [0,1]（负相关按 0 处理，适配相关性语义）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def pointwise_judge(
    judge_fn: Callable[[str, str], str],
    system_prompt: str,
    question: str,
    answer: str,
    context: str = "",
) -> float:
    """单点 LLM 打分包装：解析 judge 返回里的 0-5 分并归一化到 [0,1]。

    judge_fn(system, user) -> str。找不到数字记 0.0。
    """
    user = f"问题：{question}\n\n上下文：{context}\n\n回答：{answer}\n\n请给 0-5 分。"
    raw = (judge_fn(system_prompt, user) or "").strip()
    m = re.search(r"([0-5](?:\.\d+)?)", raw)
    if not m:
        return 0.0
    return max(0.0, min(1.0, float(m.group(1)) / 5.0))


def pairwise_judge_debiased(
    judge_fn: Callable[[str, str], str],
    system_prompt: str,
    question: str,
    answer_a: str,
    answer_b: str,
) -> str:
    """成对 judge + 位置偏差治理（复用 shadow_eval 模式）。

    交换顺序评两次；只有两次结论一致（且非平局）才判胜负，否则 'tie'。
    返回 'a' | 'b' | 'tie'。
    """
    def _one(x: str, y: str) -> str:
        user = f"问题：{question}\n\n回答A：{x}\n\n回答B：{y}\n\n哪个更好？只答 A/B/T。"
        raw = (judge_fn(system_prompt, user) or "").strip().upper()
        m = re.search(r"[ABT]", raw)
        return m.group(0) if m else "T"

    r1 = _one(answer_a, answer_b)          # A=a
    r2 = _one(answer_b, answer_a)          # A=b
    first = {"A": "a", "B": "b", "T": "tie"}[r1]
    second = {"A": "b", "B": "a", "T": "tie"}[r2]
    if first == second and first != "tie":
        return first
    return "tie"


# ═════════════════════════════════════════════════════════════════════
# 3) AGENT 层
#    trajectory 约定：list[dict]，每步 {"tool": str, "args": dict, "ok": bool}
#    expected 约定：   list[dict]，每步 {"tool": str, "args": dict}
# ═════════════════════════════════════════════════════════════════════

def _tool_names(traj: Sequence[Dict[str, Any]]) -> List[str]:
    return [str(step.get("tool")) for step in traj if step.get("tool")]


def tool_selection_accuracy(
    actual: Sequence[Dict[str, Any]],
    expected: Sequence[Dict[str, Any]],
) -> float:
    """工具选择准确率 = 期望工具中被实际调用到的比例（按集合）。

    公式：|set(期望工具) ∩ set(实际工具)| / |set(期望工具)|。
    适用：Agent 有没有选对该用的工具。无期望工具返回 1.0。
    """
    exp = set(_tool_names(expected))
    if not exp:
        return 1.0
    act = set(_tool_names(actual))
    return len(exp & act) / len(exp)


def parameter_accuracy(
    actual: Sequence[Dict[str, Any]],
    expected: Sequence[Dict[str, Any]],
) -> float:
    """参数准确率 = 在成功匹配到工具的调用里，参数完全正确的比例。

    对每个期望步，找第一个同名实际调用，比较其 args 是否 ⊇ 期望 args
    （期望 args 的每个键值都相等）。
    适用：不仅选对工具，还要传对参数。无期望步返回 1.0。
    """
    exp = [s for s in expected if s.get("tool")]
    if not exp:
        return 1.0
    remaining = list(actual)
    correct = 0
    for e in exp:
        want_args = e.get("args", {}) or {}
        for i, a in enumerate(remaining):
            if a.get("tool") == e.get("tool"):
                got = a.get("args", {}) or {}
                if all(got.get(k) == v for k, v in want_args.items()):
                    correct += 1
                remaining.pop(i)
                break
    return correct / len(exp)


def unnecessary_call_rate(
    actual: Sequence[Dict[str, Any]],
    expected: Sequence[Dict[str, Any]],
) -> float:
    """不必要调用率 = 多余工具调用数 / 实际调用总数。

    多余 = 实际调用了但期望里没有的工具（按名计数，超出期望次数的部分）。
    读法：越低越好；0 表示没有画蛇添足。无实际调用返回 0.0。
    """
    act = _tool_names(actual)
    if not act:
        return 0.0
    from collections import Counter
    exp_c = Counter(_tool_names(expected))
    extra = 0
    for name in act:
        if exp_c.get(name, 0) > 0:
            exp_c[name] -= 1
        else:
            extra += 1
    return extra / len(act)


def task_completion_rate(successes: Sequence[bool]) -> float:
    """任务完成率 = 成功完成的 case 数 / 总 case 数。

    successes：每个 case 是否达成最终目标（布尔）。空输入返回 0.0。
    """
    s = list(successes)
    return (sum(1 for x in s if x) / len(s)) if s else 0.0


def error_recovery_rate(records: Sequence[Dict[str, Any]]) -> float:
    """错误恢复率 = 在“途中有工具失败”的 case 里，仍最终成功的比例。

    record：{"trajectory": [...含 ok=False 的步...], "success": bool}。
    只统计出现过工具失败（任一步 ok is False）的 case。
    读法：Agent 遇错重试 / 换路径的韧性。无失败 case 返回 1.0。
    """
    failed_cases = [
        r for r in records
        if any(step.get("ok") is False for step in r.get("trajectory", []))
    ]
    if not failed_cases:
        return 1.0
    recovered = sum(1 for r in failed_cases if r.get("success"))
    return recovered / len(failed_cases)


def avg_turns(records: Sequence[Dict[str, Any]]) -> float:
    """平均轮次 = mean(每个 case 的对话轮次 turns)。"""
    return mean([float(r.get("turns", 0)) for r in records])


def avg_tool_calls(records: Sequence[Dict[str, Any]]) -> float:
    """平均工具调用次数 = mean(每个 case 的 trajectory 长度)。"""
    return mean([float(len(r.get("trajectory", []))) for r in records])


def agent_stability(run_successes: Sequence[bool]) -> float:
    """Agent 稳定性 = pass@N：同一任务跑 N 次，至少 1 次成功记 1.0。

    run_successes：同一 case 的 N 次运行成功标志。读法：能不能做成（下界）。
    """
    runs = list(run_successes)
    if not runs:
        return 0.0
    return 1.0 if any(runs) else 0.0


def consecutive_success_rate(run_successes: Sequence[bool]) -> float:
    """连续成功率 = all@N：同一任务跑 N 次，全部成功记 1.0，否则 0.0。

    读法：稳定性上界 / 可靠性；对确定性要求高的任务看这个。
    """
    runs = list(run_successes)
    if not runs:
        return 0.0
    return 1.0 if all(runs) else 0.0


# ═════════════════════════════════════════════════════════════════════
# 4) 工程层  ENGINEERING
#    输入：运行记录（原始输出串 / 延迟 / token / 重试 等）
# ═════════════════════════════════════════════════════════════════════

def json_validity_rate(outputs: Sequence[str]) -> float:
    """格式合规（JSON 合法率）= 能被 json.loads 解析的输出 / 总输出。

    适用：结构化输出稳定性。空输入返回 1.0。
    """
    outs = list(outputs)
    if not outs:
        return 1.0
    ok = 0
    for o in outs:
        try:
            json.loads(o)
            ok += 1
        except Exception:
            pass
    return ok / len(outs)


def schema_pass_rate(objects: Sequence[Any], required_keys: Sequence[str]) -> float:
    """Schema 通过率 = 含全部 required_keys 的对象 / 总对象。

    objects：已解析的 dict（或可 json.loads 的串）。适用：字段契约达标率。
    空输入返回 1.0。
    """
    objs = list(objects)
    if not objs:
        return 1.0
    req = list(required_keys)
    ok = 0
    for o in objs:
        d = o
        if isinstance(o, str):
            try:
                d = json.loads(o)
            except Exception:
                continue
        if isinstance(d, dict) and all(k in d for k in req):
            ok += 1
    return ok / len(objs)


def enum_accuracy(values: Sequence[Any], valid_set: Sequence[Any]) -> float:
    """枚举准确率 = 落在合法枚举集合内的取值 / 总取值。

    适用：意图 / 分类 / 状态字段是否越界。空输入返回 1.0。
    """
    vals = list(values)
    if not vals:
        return 1.0
    valid = set(valid_set)
    return sum(1 for v in vals if v in valid) / len(vals)


def latency_stats(latencies_ms: Sequence[float]) -> Dict[str, float]:
    """延迟统计：返回 mean / p50 / p90 / p95 / p99 / max（毫秒）。

    可用于 TTFT（首 token 时延）与 E2E Latency（端到端时延），传对应序列即可。
    读法：p95 / p99 比均值更能反映尾部体验。
    """
    vals = list(latencies_ms)
    return {
        "count": len(vals),
        "mean": round(mean(vals), 2),
        "p50": round(percentile(vals, 50), 2),
        "p90": round(percentile(vals, 90), 2),
        "p95": round(percentile(vals, 95), 2),
        "p99": round(percentile(vals, 99), 2),
        "max": round(max(vals), 2) if vals else 0.0,
    }


def token_stats(input_tokens: Sequence[int], output_tokens: Sequence[int]) -> Dict[str, float]:
    """Token 统计：输入 / 输出 token 的总量与均值。

    适用：成本与上下文预算归因。
    """
    ins = list(input_tokens)
    outs = list(output_tokens)
    return {
        "input_total": sum(ins),
        "output_total": sum(outs),
        "input_mean": round(mean([float(x) for x in ins]), 2),
        "output_mean": round(mean([float(x) for x in outs]), 2),
        "total": sum(ins) + sum(outs),
    }


def retry_rate(records: Sequence[Dict[str, Any]]) -> float:
    """重试率 = 发生过重试的 case 数 / 总 case 数。

    record：{"retries": int}；retries>0 记为发生重试。空输入返回 0.0。
    读法：越高说明底层调用越不稳（限流 / 超时 / 解析失败重试）。
    """
    recs = list(records)
    if not recs:
        return 0.0
    return sum(1 for r in recs if int(r.get("retries", 0)) > 0) / len(recs)


def refusal_rate(answers: Sequence[str]) -> float:
    """拒答率 = 命中拒答标记的回答 / 总回答。

    读法：need 结合语境——对抗 / 越权题拒答是对的，正常题拒答是问题。
    空输入返回 0.0。
    """
    ans = list(answers)
    if not ans:
        return 0.0
    return sum(1 for a in ans if is_refusal(a)) / len(ans)


def hallucination_rate(
    records: Sequence[Dict[str, Any]],
    judge_fn: Optional[Callable[[str, str], bool]] = None,
    support_threshold: float = 0.6,
) -> float:
    """幻觉率 = 平均每条回答里“无 context 支撑的断言”比例 = mean(1 - faithfulness)。

    record：{"answer": str, "contexts": list[str]}。
    读法：越低越好；是 faithfulness 的负向聚合视角。空输入返回 0.0。
    """
    recs = list(records)
    if not recs:
        return 0.0
    total = 0.0
    for r in recs:
        f = faithfulness(r.get("answer", ""), r.get("contexts", []),
                         judge_fn=judge_fn, support_threshold=support_threshold)
        total += (1.0 - f)
    return total / len(recs)


def format_following_rate(outputs: Sequence[str], pattern: str) -> float:
    """格式遵循率 = 匹配指定正则/格式的输出 / 总输出。

    pattern：如订单号 ``^ORD-\\d{6}$``、必须以 JSON 起始 ``^\\s*\\{`` 等。
    适用：受控格式（模板 / 前缀 / 大小写）遵循度。空输入返回 1.0。
    """
    outs = list(outputs)
    if not outs:
        return 1.0
    rx = re.compile(pattern, re.DOTALL)
    return sum(1 for o in outs if rx.search(o or "")) / len(outs)


# 指标注册表（供 harness / 文档 / 自检使用）—— 分四组
METRIC_GROUPS: Dict[str, List[str]] = {
    "retrieval": [
        "recall_at_k", "hit_rate_at_k", "mrr", "precision_at_k",
        "context_precision", "context_recall",
    ],
    "generation": [
        "faithfulness", "answer_relevance", "completeness",
        "context_usage", "noise_sensitivity",
    ],
    "agent": [
        "tool_selection_accuracy", "parameter_accuracy", "unnecessary_call_rate",
        "task_completion_rate", "error_recovery_rate", "avg_turns",
        "avg_tool_calls", "agent_stability", "consecutive_success_rate",
    ],
    "engineering": [
        "json_validity_rate", "schema_pass_rate", "enum_accuracy",
        "latency_stats", "token_stats", "retry_rate", "refusal_rate",
        "hallucination_rate", "format_following_rate",
    ],
}


def metric_count() -> int:
    """速查表 / 注册表里的指标总数。"""
    return sum(len(v) for v in METRIC_GROUPS.values())
