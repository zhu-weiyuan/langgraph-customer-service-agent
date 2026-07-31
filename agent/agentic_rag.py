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
    # Fast mode is the interactive default: perform one PostgreSQL + pgvector
    # retrieval and immediately reserve the local LLM for the user-visible
    # answer stream.  The former default used one or more planner/evaluator
    # LLM calls before generation and could hit the 120-second graph timeout.
    # Set AGENTIC_RAG_MODE=deep for the slower evaluate/rewrite loop.
    import os as _os
    mode = _os.environ.get("AGENTIC_RAG_MODE", "fast").strip().lower()
    if mode not in {"fast", "deep"}:
        mode = "fast"
    _env_rounds = _os.environ.get("AGENTIC_RAG_MAX_ROUNDS", "").strip()
    if _env_rounds.isdigit():
        max_rounds = max(1, int(_env_rounds))
    _skip_eval = _os.environ.get("AGENTIC_RAG_EVAL", "").strip() == "0"
    result = {
        "context": "",
        "rounds": 1,
        "queries_tried": [],
        "sufficient": False,
        "hits": [],
        "mode": mode,
        "planner_llm_calls": 0,
    }

    current_query = user_query
    # Literal-query retrieval remains quality-safe because rag_backend performs
    # pgvector retrieval, reranking and lexical relevance filtering.
    initial_hits = _retrieve_queries([user_query], result)
    if mode == "fast":
        result["context"] = _build_context_string(initial_hits) if initial_hits else ""
        result["sufficient"] = bool(initial_hits)
        return result

    # Deep mode only: let the LLM evaluate/rewrite weak retrieval results.
    llm = get_llm_client()
    if initial_hits:
        initial_formatted = _format_results(initial_hits)
        if _skip_eval:
            result["context"] = _build_context_string(initial_hits)
            result["sufficient"] = True
            return result
        result["planner_llm_calls"] += 1
        initial_eval = _evaluate(llm, user_query, initial_formatted)
        if initial_eval.get("sufficient", False):
            result["context"] = _build_context_string(initial_hits)
            result["sufficient"] = True
            return result
        next_queries = initial_eval.get("new_queries") or []
    else:
        next_queries = []

    # 原始检索不充分后，才进入改写检索。max_rounds 表示最多改写次数，
    # 因此默认 2 表示最差情况会改写两次，而不是“原始轮 + 一次改写”。
    for rewrite_round in range(1, max(0, max_rounds) + 1):
        result["rounds"] = rewrite_round + 1
        queries = [str(q).strip() for q in next_queries if str(q).strip()][:3]
        if not queries:
            result["planner_llm_calls"] += 1
            queries = _rewrite_query(llm, current_query)
        print(f"[Agentic RAG Rewrite {rewrite_round}] Queries: {queries}")
        all_results = _retrieve_queries(queries, result)

        if not all_results:
            print(f"[Agentic RAG Rewrite {rewrite_round}] No results found")
            break

        formatted = _format_results(all_results)
        print(f"[Agentic RAG Rewrite {rewrite_round}] Retrieved {len(all_results)} sections")
        if _skip_eval:
            result["context"] = _build_context_string(all_results)
            result["sufficient"] = True
            break

        # 最多两次改写；每次改写结果足够就立刻停止。
        result["planner_llm_calls"] += 1
        eval_result = _evaluate(llm, user_query, formatted)
        if eval_result.get("sufficient", False) or rewrite_round >= max(0, max_rounds):
            result["context"] = _build_context_string(all_results)
            result["sufficient"] = True
            break
        next_queries = eval_result.get("new_queries") or []

    return result


def _retrieve_queries(queries: List[str], result: Dict[str, any]) -> List[dict]:
    """Retrieve each query, deduplicate by section, and preserve the best scored hits."""
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
    all_results.sort(key=lambda x: x["score"], reverse=True)
    selected = all_results[:5]
    _remember_hits(result, selected)
    return selected


def _remember_hits(result: Dict[str, any], hits: List[dict]) -> None:
    """Keep a compact, deduplicated copy of retrieval hits for observability."""
    stored = result.setdefault("hits", [])
    seen = {(h.get("title"), h.get("source")) for h in stored if isinstance(h, dict)}
    for h in hits:
        if not isinstance(h, dict):
            continue
        key = (h.get("title"), h.get("source"))
        if key in seen:
            continue
        seen.add(key)
        stored.append({
            "title": h.get("title", ""),
            "source": h.get("source", ""),
            "score": float(h.get("score") or 0.0),
            "text": str(h.get("text") or "")[:800],
        })


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
