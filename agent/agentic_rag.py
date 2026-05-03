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

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

REWRITE_PROMPT = """你是一个信息检索专家。用户问了一个问题，你需要生成 2-3 个不同的搜索查询词，用于在知识库中检索相关信息。

要求：
- 每个查询词 3-8 个字，尽量用关键词而非完整句子
- 从不同角度覆盖用户问题（同义词、相关概念）
- 返回严格的 JSON 数组格式：["查询1", "查询2", "查询3"]
不要返回其他任何文字。"""

EVALUATE_PROMPT = """你是一个信息质量评估专家。下面是一次知识库检索的结果，请判断这些结果是否能回答用户的问题。

用户问题：{query}

检索结果：
{results}

返回严格的 JSON 格式（不要其他文字）：
{{"sufficient": true/false, "reason": "简短理由", "new_queries": ["新查询1", "新查询2"]}}

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
            hits = rag_retrieve(q, top_k=3)
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

        # Step 4: Evaluate sufficiency
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
        text = llm.chat(
            [{"role": "user", "content": f"用户问题：{query}\n请生成搜索查询词。"}],
            REWRITE_PROMPT,
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
    
    # Extract keywords by removing stop words
    keywords = [w for w in query if w not in STOP_WORDS and len(w.strip()) > 0]
    core = ''.join(keywords)
    
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
