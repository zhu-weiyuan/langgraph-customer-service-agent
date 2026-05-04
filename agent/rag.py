# -*- coding: utf-8 -*-
"""
Enhanced RAG Module — BM25 + TF-IDF Hybrid Retrieval

Combines:
1. BM25 with jieba Chinese tokenization (precise word boundaries)
2. TF-IDF with n-gram fallback (catches terms jieba might miss)
3. Source diversity bonus (don't return too many from same doc)
4. Title boost (section titles are strong relevance signals)

No external vector DB or embedding models needed — pure Python + jieba.
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
BM25_WEIGHT = 0.7
TFIDF_WEIGHT = 0.3
TITLE_BOOST = 2.0
SOURCE_DIVERSITY_PENALTY = 0.3

# Cached documents and indices
_documents = []
_bm25_index = {}
_doc_lengths = []
_avg_doc_length = 0.0


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
    """Build BM25 inverted index."""
    global _bm25_index, _doc_lengths, _avg_doc_length
    
    _bm25_index = {}
    _doc_lengths = []
    
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
    """Compute TF-IDF score with n-gram tokens."""
    if not query_tokens or doc_id >= len(_doc_lengths):
        return 0.0
    
    # Get document tokens (from cached sections)
    section = _get_section(doc_id)
    if not section:
        return 0.0
    
    doc_tokens = _tokenize_ngram(section["text"])
    if not doc_tokens:
        return 0.0
    
    query_set = set(query_tokens)
    doc_set = set(doc_tokens)
    overlap = query_set & doc_set
    
    if not overlap:
        return 0.0
    
    # Simple TF-IDF scoring
    score = 0.0
    for token in overlap:
        tf = doc_tokens.count(token) / len(doc_tokens)
        df = sum(1 for d in _documents for s in d["sections"] if token in _tokenize_ngram(s["text"]))
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


def retrieve(query: str, top_k: int = 3) -> List[dict]:
    """Retrieve most relevant sections using hybrid BM25 + TF-IDF."""
    docs = _load_knowledge_base()
    if not docs:
        return []

    jieba_tokens = _tokenize_jieba(query)
    ngram_tokens = _tokenize_ngram(query)
    
    # Score each section with both methods
    scored_sections = []
    for doc_id, doc in enumerate(docs):
        section_offset = sum(len(d["sections"]) for d in docs[:doc_id])
        
        for i, section in enumerate(doc["sections"]):
            global_id = section_offset + i
            
            # BM25 score (jieba tokens)
            bm25 = _bm25_score(jieba_tokens, global_id) if jieba_tokens else 0
            
            # TF-IDF score (n-gram tokens)
            tfidf = _tfidf_score(ngram_tokens, global_id) if ngram_tokens else 0
            
            # Normalize and combine
            combined = BM25_WEIGHT * bm25 + TFIDF_WEIGHT * tfidf
            
            # Title boost
            title_jieba = _tokenize_jieba(section["title"])
            title_bm25 = _bm25_score(jieba_tokens, global_id) if title_jieba else 0
            if title_bm25 > 0:
                combined += TITLE_BOOST * title_bm25 * 0.1
            
            if combined > 0:
                scored_sections.append({
                    "title": section["title"],
                    "text": section["text"],
                    "score": round(combined, 4),
                    "source": doc["title"]
                })

    # Sort by score
    scored_sections.sort(key=lambda x: x["score"], reverse=True)
    
    # Source diversity
    diverse_results = []
    source_counts = {}
    for result in scored_sections:
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
