# -*- coding: utf-8 -*-
"""
Agentic RAG — Let the LLM decide how to search.

Instead of blindly calling retrieve(query, top_k), an Agentic RAG loop:
  1. Rewrites the user query into better search keywords
  2. Retrieves & returns results to the LLM for evaluation
  3. If results are insufficient, generates alternative queries and retries
  4. After max rounds, returns the best available context (may be empty)

This is a function-level agent — no LangGraph node needed here.
The calling node passes user_query and gets back enriched context.
"""

from typing import List, Dict, Optional

from .rag import retrieve as rag_retrieve
from .llm_client import get_llm_client

# RAG_BACKEND=tfidf|hybrid|pgvector 后端选择（含运行期优雅降级 TF-IDF）。
# rag_backend 不可用时保持旧行为（直接走 rag.retrieve）。
try:
    from .rag_backend import retrieve as _backend_retrieve
except Exception:  # pragma: no cover
    def _backend_retrieve(query, top_k=3):
        return rag_retrieve(query, top_k=top_k)

# ---------------------------------------------------------------------------
# Prompts (Improved with Few-Shot and Entity Extraction)
# ---------------------------------------------------------------------------

REWRITE_PROMPT = """你是智能客服检索助手。将用户问题改写为 2-3 个知识库搜索查询。

【改写策略】
1. **提取关键词**：保留核心实体（产品名、错误码、功能名）
2. **标准化表达**：口语→书面语（"连不上"→"无法连接"，"咋弄"→"如何操作"）
3. **补充隐含信息**：根据上下文补全型号、场景等
4. **多角度覆盖**：从症状、原因、解决方案不同角度构造

【特殊处理】
- **错误码**：E001/E018 等直接保留，生成"错误码 E001"、"E001 解决方法"
- **产品型号**：X-100/X-200/X-300 Pro 保留，可生成型号专用查询
- **多跳问题**：拆分因果链（"为什么灯闪不能控制"→["灯闪烁原因", "设备无法控制"]）

【输出格式】
严格 JSON 数组，无其他文字：["查询 1", "查询 2", "查询 3"]

【示例】
用户："我家那个小音箱咋连 WiFi 啊" → ["音箱 WiFi 配网步骤", "智能音箱连接无线网络", "音箱无法连接 WiFi 排查"]
用户："E018 是什么错误" → ["错误码 E018", "E018 过载保护", "智能插座过载处理"]

现在改写："""

EVALUATE_PROMPT = """你是一个信息质量评估专家。下面是一次知识库检索的结果，请判断这些结果是否能回答用户的问题。

用户问题：{query}

检索结果：
{results}

返回严格的 JSON 格式（不要其他文字）：
{{"sufficient": true/false, "reason": "简短理由", "new_queries": ["新查询 1", "新查询 2"]}}

规则：
- 如果结果中包含了回答用户问题所需的关键信息 → sufficient: true
- 如果结果完全不相关或信息不足 → sufficient: false，并给出新的搜索查询词
- 如果不确定但有一定相关性 → sufficient: true（宁可多给一点上下文）"""


def agentic_rag(user_query: str, max_rounds: int = 2) -> Dict[str, any]:
    """Run the Agentic RAG loop.

    Args:
        user_query: Original user question
        max_rounds: Maximum retrieval rounds (default 2)

    Returns:
        {
            "context": str,          # Final formatted context string
            "rounds": int,           # How many rounds were used
            "queries_tried": list,   # All queries attempted
            "sufficient": bool,      # Whether results are deemed sufficient
        }
    """
    llm = get_llm_client()
    # 链路提速开关 (降低单次提问的 LLM 调用数与总时延):
    #   AGENTIC_RAG_MAX_ROUNDS: 覆盖最大检索轮数 (默认沿用入参)
    #   AGENTIC_RAG_EVAL=0    : 跳过"充分性评估"LLM 调用，首轮结果直接采用
    import os as _os
    _env_rounds = _os.environ.get("AGENTIC_RAG_MAX_ROUNDS", "").strip()
    if _env_rounds.isdigit():
        max_rounds = max(1, int(_env_rounds))
    _skip_eval = _os.environ.get("AGENTIC_RAG_EVAL", "").strip() == "0"
    result = {
        "context": "",
        "rounds": 0,
        "queries_tried": [],
        "sufficient": False,
    }

    current_query = user_query

    for round_num in range(1, max_rounds + 1):
        result["rounds"] = round_num

        # Step 1: Rewrite query into search keywords
        queries = _rewrite_query(llm, current_query)
        print(f"[Agentic RAG Round {round_num}] Rewritten queries: {queries}")

        # Step 2: Retrieve with each query, merge & deduplicate
        all_results = []
        seen_titles = set()
        for q in queries:
            result["queries_tried"].append(q)
            hits = _backend_retrieve(q, top_k=3)
            for h in hits:
                key = (h["title"], h["source"])
                if key not in seen_titles:
                    seen_titles.add(key)
                    all_results.append(h)

        # Sort by score descending, keep top 5
        all_results.sort(key=lambda x: x["score"], reverse=True)
        all_results = all_results[:5]

        if not all_results:
            print(f"[Agentic RAG Round {round_num}] No results found")
            break

        # Step 3: Format results for evaluation
        formatted = _format_results(all_results)
        print(f"[Agentic RAG Round {round_num}] Retrieved {len(all_results)} sections")

        # Step 4: 充分性判定。
        # 默认走"启发式门控"——检索本就良好 (命中数达标) 时直接采用首轮结果，
        # 不再花一次 LLM 调用去评估 (用户反馈：改写良好时应只改写一次)。
        # 仅当:① 显式 AGENTIC_RAG_EVAL=1 强制评估，或 ② 检索偏弱 (结果数 < 阈值)
        # 且还有下一轮预算时，才发 LLM 评估来决定是否再改写。
        _force_eval = _os.environ.get("AGENTIC_RAG_EVAL", "").strip() == "1"
        _min_hits = int(_os.environ.get("AGENTIC_RAG_MIN_HITS", "3"))
        _retrieval_good = len(all_results) >= _min_hits
        if _skip_eval or (_retrieval_good and not _force_eval):
            # 首轮检索良好 (或显式跳过评估):直接采用，zero 额外 LLM 调用
            result["context"] = _build_context_string(all_results)
            result["sufficient"] = True
            break
        if round_num >= max_rounds:
            # 最后一轮：不值得再花评估调用，直接用现有结果
            result["context"] = _build_context_string(all_results)
            result["sufficient"] = True
            break
        # 检索偏弱且还有下一轮预算 → 用 LLM 评估决定是否再改写
        eval_result = _evaluate(llm, user_query, formatted)
        sufficient = eval_result.get("sufficient", False)
        reason = eval_result.get("reason", "")
        new_queries = eval_result.get("new_queries", [])

        print(f"[Agentic RAG Round {round_num}] Evaluation: sufficient={sufficient}, reason={reason}")

        if sufficient:
            result["context"] = _build_context_string(all_results)
            result["sufficient"] = True
            break

        # Not sufficient — try again with new queries
        if new_queries and round_num < max_rounds:
            current_query = " ".join(new_queries)
            print(f"[Agentic RAG Round {round_num}] New queries for next round: {current_query}")
        else:
            # Last round or no new queries — use what we have anyway
            result["context"] = _build_context_string(all_results)
            result["sufficient"] = len(all_results) > 0
            break

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rewrite_query(llm, query: str) -> List[str]:
    """Ask LLM to generate alternative search queries."""
    try:
        # 修复：LLMClient.chat(messages, temperature=..., max_tokens=...) 没有
        # system 位置参数；旧写法把 REWRITE_PROMPT 传给了 temperature，又与
        # temperature=0.5 关键字冲突 → TypeError。system prompt 应放进 messages。
        text = llm.chat(
            [
                {"role": "system", "content": REWRITE_PROMPT},
                {"role": "user", "content": f"用户问题：{query}\n请生成搜索查询词。"},
            ],
            max_tokens=128,
            temperature=0.5,
        )
        # Parse JSON array
        import re, json
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                queries = json.loads(match.group())
                if isinstance(queries, list) and len(queries) > 0:
                    return [str(q).strip() for q in queries[:3]]
            except json.JSONDecodeError:
                pass
    except Exception as e:
        print(f"[Agentic RAG] Query rewrite failed: {e}")

    # Fallback: use original query + keyword extraction (no LLM needed)
    return _fallback_queries(query)


def _evaluate(llm, query: str, results_text: str) -> dict:
    """Ask LLM to evaluate if results are sufficient."""
    prompt = EVALUATE_PROMPT.format(query=query, results=results_text)
    try:
        return llm.chat_json(
            [{"role": "user", "content": prompt}],
            "你是一个评估助手，只返回 JSON。",
            max_tokens=256,
        )
    except Exception as e:
        print(f"[Agentic RAG] Evaluation failed: {e}")
        return {"sufficient": True, "reason": "evaluation error, accepting results", "new_queries": []}


def _format_results(results: List[dict]) -> str:
    """Format retrieval results for LLM evaluation."""
    lines = []
    for i, r in enumerate(results, 1):
        text_preview = r["text"][:200]
        lines.append(f"[{i}] {r['title']} (score={r['score']:.2f}, source={r['source']})\n{text_preview}")
    return "\n\n".join(lines)


def _build_context_string(results: List[dict]) -> str:
    """Build the final context string to inject into system prompt."""
    if not results:
        return ""
    parts = ["\n## 参考资料（知识库 · Agentic RAG）\n"]
    for i, r in enumerate(results, 1):
        text = r["text"]
        if len(text) > 500:
            text = text[:500] + "..."
        parts.append(f"\n### [{i}] {r['title']}\n{text}\n")
    return "".join(parts)


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len characters."""
    if len(text) <= max_len:
        return text
    return text[:max_len]


def _fallback_queries(query: str) -> List[str]:
    """Generate search query variants without LLM — used when rewrite fails.

    Strips common stop words and generates keyword combinations.
    This is better than naive truncation because it preserves semantic keywords.
    """
    # Common Chinese stop words to strip for keyword extraction
    STOP_WORDS = {'的', '了', '是', '在', '有', '和', '就', '都', '而',
                  '与', '及', '这', '那', '吧', '吗', '呢', '啊', '哦',
                  '什么', '怎么', '如何', '可以', '能够', '请问', '一下',
                  '一下下', '帮我', '我想', '我要', '能不能', '有没有'}

    # 修复：旧版 `for w in query` 逐字符遍历，多字停用词（如"什么"）永远
    # 匹配不上，还会误删正常词里的单字（如"的哥"→"哥"）。改为按词遍历：
    # 先按长度降序把停用词整体剔除，再拼回核心关键词。
    import re as _re
    core = query
    for word in sorted(STOP_WORDS, key=len, reverse=True):
        core = core.replace(word, ' ')
    tokens = [t for t in _re.split(r'\s+', core) if t.strip()]
    core = ''.join(tokens)

    queries = [query]  # Always include original
    if core and core != query:
        queries.append(core)  # Core keywords without stop words
    if len(query) > 4:
        queries.append(_truncate(query, 4))  # Short version

    # Deduplicate while preserving order
    seen = set()
    result = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            result.append(q)

    return result[:3]
