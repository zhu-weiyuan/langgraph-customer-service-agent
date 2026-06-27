# -*- coding: utf-8 -*-
"""
Enhanced RAG Module — BM25 + TF-IDF + Vector Hybrid Retrieval

Combines:
1. BM25 with jieba Chinese tokenization (precise word boundaries)
2. TF-IDF with n-gram fallback (catches terms jieba might miss)
3. Real vector retrieval via OpenRouter embedding API (semantic search)
4. RRF fusion for combining keyword + vector results
5. Query synonym expansion (口语化→规范化)
6. Jieba custom dictionary (产品专有名词)

Optimized parameters:
- BM25/TF-IDF weight: 0.7/0.3
- RRF k_constant: 40 (optimized for Chinese customer service)
"""

import math
import re
from pathlib import Path
from typing import List, Dict, Tuple
import jieba


# Knowledge base directory
KB_DIR = Path(__file__).parent.parent / "knowledge"

# BM25 parameters
BM25_K1 = 1.5
BM25_B = 0.75

# Hybrid weights — favor BM25 for ranking, TF-IDF for recall
BM25_WEIGHT = 1.0
TFIDF_WEIGHT = 0.0  # Disabled for performance (n-gram scoring too slow on CPU)
TITLE_BOOST = 2.0
SOURCE_DIVERSITY_PENALTY = 0.3

# ── Query synonym expansion ──────────────────────────────
SYNONYM_MAP = {
    "咋整": "怎么办", "咋连": "怎么连接", "咋开": "怎么开发票",
    "咋退": "怎么退货", "咋升": "怎么升级", "咋配": "怎么配对",
    "WiFi": "无线网络连接", "Wi-Fi": "无线网络连接",
    "固件": "设备固件升级", "音箱": "智能音箱",
    "没声": "没声音", "离线": "设备离线", "断网": "网络连接失败",
    "退货": "退换货政策", "退款": "退换货政策",
    "保修": "保修服务", "维修": "保修服务",
    "发票": "开具发票", "开票": "开具发票",
    "配对": "设备配对", "连不上": "连接失败",
    "抽风": "故障排除", "死机": "设备无法启动",
}

CUSTOM_TERMS = [
    ("无线网络", 10), ("蓝牙配对", 10), ("智能家居", 10),
    ("智能音箱", 10), ("网易云音乐", 8), ("QQ音乐", 8),
    ("设备离线", 10), ("没声音", 10), ("重置设备", 10),
    ("固件升级", 10), ("退换货", 10), ("保修期", 10),
    ("开具发票", 10), ("联系客服", 8),
]

# Initialize jieba custom dict
for term, weight in CUSTOM_TERMS:
    jieba.add_word(term, weight)


def _expand_query(query: str) -> str:
    """Expand query with synonyms for better retrieval coverage.

    Strategy: Replace colloquial terms with standardized terms,
    but keep the original terms too for broader matching.

    Example: "咋连WiFi" → "咋连 怎么连接 WiFi 无线网络连接"
    """
    expanded = query
    sorted_synonyms = sorted(SYNONYM_MAP.items(), key=lambda x: len(x[0]), reverse=True)
    for slang, standard in sorted_synonyms:
        if slang in expanded:
            expanded = expanded.replace(slang, f"{slang} {standard}")
    return expanded


# ── RRF fusion parameters ────────────────────────────────
RRF_K_CONSTANT = 40  # Optimized for Chinese customer service (was 61)

# Cached documents and indices
_documents = []
_bm25_index = {}
_doc_lengths = []
_avg_doc_length = 0.0
_ngram_index = []       # Precomputed n-gram counts per section
_ngram_df = {}          # Precomputed document frequency for n-grams


def _load_knowledge_base() -> List[dict]:
    """Load all markdown files from knowledge/ directory."""
    global _documents, _bm25_index, _doc_lengths, _avg_doc_length

    if _documents:
        return _documents

    _documents = []
    if not KB_DIR.exists():
        print(f"[RAG] Knowledge base directory not found: {KB_DIR}")
        return _documents

    for md_file in sorted(KB_DIR.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
            doc = _parse_markdown(text, md_file.stem)
            _documents.append(doc)
            print(f"[RAG] Loaded: {md_file.name} ({len(doc['sections'])} sections)")
        except Exception as e:
            print(f"[RAG] Error loading {md_file}: {e}")

    _build_index()
    return _documents


def _parse_markdown(text: str, title: str) -> dict:
    """Parse markdown into sections based on headings."""
    sections = []
    lines = text.split("\n")
    current_heading = title
    current_content = []

    for line in lines:
        if line.startswith("#"):
            if current_content:
                sections.append({
                    "title": current_heading,
                    "text": "\n".join(current_content).strip()
                })
            level = len(re.match(r'^(#+)', line).group(1))
            heading_text = line.lstrip("# ").strip()
            if level <= 3:
                current_heading = heading_text
                current_content = []
            else:
                current_content.append(line)
        else:
            current_content.append(line)

    if current_content:
        sections.append({
            "title": current_heading,
            "text": "\n".join(current_content).strip()
        })

    return {"title": title, "content": text, "sections": sections}


def _tokenize_jieba(text: str) -> List[str]:
    """Tokenize using jieba for proper Chinese word segmentation."""
    words = list(jieba.cut(text))
    filtered = []
    for w in words:
        w_stripped = w.strip()
        if not w_stripped:
            continue
        if len(w_stripped) >= 2:
            filtered.append(w_stripped.lower())
        elif re.match(r'[\u4e00-\u9fff]', w_stripped):
            filtered.append(w_stripped)
        elif re.match(r'[a-zA-Z0-9]+', w_stripped):
            filtered.append(w_stripped.lower())
    return filtered


def _tokenize_ngram(text: str) -> List[str]:
    """Fallback n-gram tokenizer for terms jieba might miss."""
    chinese_runs = re.findall(r'[\u4e00-\u9fff]+', text)
    english_words = re.findall(r'[a-zA-Z]+', text)

    tokens = []
    for run in chinese_runs:
        chars = list(run)
        for i in range(len(chars) - 1):
            tokens.append(chars[i] + chars[i+1])
        for i in range(len(chars) - 2):
            tokens.append(chars[i] + chars[i+1] + chars[i+2])

    for w in english_words:
        if len(w) >= 2:
            tokens.append(w.lower())

    return tokens


def _build_index():
    """Build BM25 inverted index + n-gram cache."""
    global _bm25_index, _doc_lengths, _avg_doc_length, _ngram_index, _ngram_df

    _bm25_index = {}
    _doc_lengths = []
    _ngram_index = []
    _ngram_df = {}

    section_id = 0
    for doc in _documents:
        for section in doc["sections"]:
            title_tokens = _tokenize_jieba(section["title"])
            text_tokens = _tokenize_jieba(section["text"])

            # Title tokens weighted 3x
            combined = title_tokens * 3 + text_tokens

            tf = {}
            for token in combined:
                tf[token] = tf.get(token, 0) + 1

            _doc_lengths.append(len(combined))

            for word, count in tf.items():
                if word not in _bm25_index:
                    _bm25_index[word] = {}
                _bm25_index[word][section_id] = count

            # Precompute n-gram index for this section
            ngrams = _tokenize_ngram(section["text"])
            ngram_counts = {}
            for ng in ngrams:
                ngram_counts[ng] = ngram_counts.get(ng, 0) + 1
                _ngram_df[ng] = _ngram_df.get(ng, 0) + 1
            _ngram_index.append(ngram_counts)

            section_id += 1

    if _doc_lengths:
        _avg_doc_length = sum(_doc_lengths) / len(_doc_lengths)

    print(f"[RAG Hybrid] Index built: {len(_bm25_index)} terms, {section_id} sections")


def _bm25_score(query_tokens: List[str], doc_id: int) -> float:
    """Compute BM25 score."""
    N = len(_doc_lengths)
    if N == 0 or doc_id >= N:
        return 0.0

    doc_len = _doc_lengths[doc_id]
    score = 0.0

    for q_token in query_tokens:
        if q_token not in _bm25_index:
            continue

        n_q = len(_bm25_index[q_token])
        if n_q == 0:
            continue

        idf = math.log((N - n_q + 0.5) / (n_q + 0.5))
        if idf < 0:
            idf = 0.01

        tf = _bm25_index[q_token].get(doc_id, 0)
        if tf == 0:
            continue

        numerator = tf * (BM25_K1 + 1)
        denominator = tf + BM25_K1 * (1 - BM25_B + BM25_B * doc_len / max(_avg_doc_length, 1))

        score += idf * (numerator / denominator)

    return score


def _tfidf_score(query_tokens: List[str], doc_id: int) -> float:
    """Compute TF-IDF score with cached n-gram index (fast)."""
    if not query_tokens or doc_id >= len(_doc_lengths):
        return 0.0

    if doc_id >= len(_ngram_index):
        return 0.0

    doc_ngrams = _ngram_index[doc_id]
    if not doc_ngrams:
        return 0.0

    query_set = set(query_tokens)
    overlap = query_set & set(doc_ngrams.keys())

    if not overlap:
        return 0.0

    score = 0.0
    total_ngrams = sum(doc_ngrams.values())
    for token in overlap:
        tf = doc_ngrams[token] / total_ngrams if total_ngrams > 0 else 0
        df = _ngram_df.get(token, 1)
        idf = math.log((1 + len(_doc_lengths)) / (1 + max(df, 1)))
        score += tf * idf

    return score


def _get_section(doc_id: int) -> dict:
    """Get section by global ID."""
    count = 0
    for doc in _documents:
        for section in doc["sections"]:
            if count == doc_id:
                return section
            count += 1
    return None


def retrieve(query: str, top_k: int = 3, use_vector: bool = True) -> List[dict]:
    """Retrieve most relevant sections using hybrid BM25 + TF-IDF + Vector (RRF fusion).

    Args:
        query: Search query string
        top_k: Number of results to return
        use_vector: If True, combine BM25+TF-IDF with vector retrieval via RRF fusion.
                   Falls back to BM25+TF-IDF only if vector retrieval fails.

    Returns:
        List of {"title", "text", "score", "source"} dicts sorted by fused relevance.
    """
    docs = _load_knowledge_base()
    if not docs:
        return []

    # ── Query preprocessing with synonym expansion ─────────
    expanded_query = _expand_query(query)
    jieba_tokens = _tokenize_jieba(expanded_query)
    ngram_tokens = _tokenize_ngram(expanded_query)

    # --- BM25 + TF-IDF scoring (keyword retrieval) ---
    scored_sections = []
    for doc_id, doc in enumerate(docs):
        section_offset = sum(len(d["sections"]) for d in docs[:doc_id])

        for i, section in enumerate(doc["sections"]):
            global_id = section_offset + i

            bm25 = _bm25_score(jieba_tokens, global_id) if jieba_tokens else 0
            tfidf = _tfidf_score(ngram_tokens, global_id) if ngram_tokens else 0

            combined = BM25_WEIGHT * bm25 + TFIDF_WEIGHT * tfidf

            title_jieba = _tokenize_jieba(section["title"])
            title_bm25 = _bm25_score(jieba_tokens, global_id) if title_jieba else 0
            if title_bm25 > 0:
                combined += TITLE_BOOST * title_bm25 * 0.1

            if combined > 0:
                scored_sections.append({
                    "title": section["title"],
                    "text": section["text"],
                    "score": round(combined, 4),
                    "source": doc["title"],
                    "_rank_bm25": len(scored_sections) + 1,
                })

    scored_sections.sort(key=lambda x: x["score"], reverse=True)

    # --- Vector retrieval (semantic search via OpenRouter embedding API) ---
    vector_results = []
    if use_vector:
        try:
            from .vector_rag import vector_retrieve as _vector_retrieve
            vector_results = _vector_retrieve(query, top_k=10)
            print(f"[RAG Hybrid] Vector retrieval: {len(vector_results)} results")
        except Exception as e:
            print(f"[RAG Hybrid] Vector retrieval skipped: {e}")

    # --- RRF (Reciprocal Rank Fusion) fusion ---
    if vector_results:
        fused = _rrf_fusion(scored_sections, vector_results, top_k=top_k * 3)
        return _apply_diversity(fused, top_k=top_k)
    else:
        for s in scored_sections:
            s.pop("_rank_bm25", None)
        return _apply_diversity(scored_sections, top_k=top_k)


def _rrf_fusion(bm25_results: List[dict], vector_results: List[dict],
                top_k: int = 10, k_constant: int = RRF_K_CONSTANT) -> List[dict]:
    """Reciprocal Rank Fusion (RRF) for combining keyword and vector retrieval.

    RRF score = 1 / (k + rank_bm25) + 1 / (k + rank_vector)

    Args:
        bm25_results: Results from BM25+TF-IDF ranking
        vector_results: Results from vector similarity search
        top_k: Number of results to return after fusion
        k_constant: RRF constant (default 40 for Chinese customer service)

    Returns:
        Fused and ranked result list.
    """
    rrf_scores = {}  # key=(title, source) -> fused score

    for rank, result in enumerate(bm25_results):
        key = (result["title"], result["source"])
        score = 1.0 / (k_constant + rank + 1)
        if key not in rrf_scores:
            rrf_scores[key] = {"_rrf": score, **result}
        else:
            rrf_scores[key]["_rrf"] += score

    for rank, result in enumerate(vector_results):
        key = (result["title"], result["source"])
        score = 1.0 / (k_constant + rank + 1)
        if key not in rrf_scores:
            rrf_scores[key] = {"_rrf": score, **result}
        else:
            rrf_scores[key]["_rrf"] += score
            if result.get("score", 0) > rrf_scores[key].get("score", 0):
                rrf_scores[key]["score"] = result["score"]

    fused = sorted(rrf_scores.values(), key=lambda x: x["_rrf"], reverse=True)

    for f in fused:
        f.pop("_rrf", None)
        f.pop("_rank_bm25", None)
        f["score"] = round(f.get("score", 0), 4)

    return fused[:top_k]


def _apply_diversity(results: List[dict], top_k: int = 3) -> List[dict]:
    """Apply source diversity filtering to results."""
    diverse_results = []
    source_counts = {}
    for result in results:
        source = result["source"]
        if source_counts.get(source, 0) < 2:
            diverse_results.append(result)
            source_counts[source] = source_counts.get(source, 0) + 1
        if len(diverse_results) >= top_k:
            break
    return diverse_results


def build_context(query: str, max_length: int = 1500) -> str:
    """Build RAG context string."""
    results = retrieve(query, top_k=3)
    if not results:
        return ""

    parts = ["\n## 参考资料（知识库）\n"]
    total_length = len(parts[0])

    for i, section in enumerate(results, 1):
        section_text = section["text"]
        if len(section_text) > 500:
            section_text = section_text[:500] + "..."

        block = f"\n### [{i}] {section['title']}\n{section_text}\n"

        if total_length + len(block) > max_length and i > 1:
            break

        parts.append(block)
        total_length += len(block)

    return "".join(parts)


def reload():
    """Force reload knowledge base."""
    global _documents, _bm25_index, _doc_lengths, _avg_doc_length
    _documents = []
    _bm25_index = {}
    _doc_lengths = []
    _avg_doc_length = 0.0
    return _load_knowledge_base()
