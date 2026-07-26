"""
RAG (Retrieval-Augmented Generation) Module

Simple keyword + TF-IDF based retrieval for local knowledge base.
No external vector DB needed — uses pure Python with basic text matching.

Knowledge base files stored in: knowledge/ directory
"""

import os
import re
import math
from pathlib import Path
from typing import List, Dict, Tuple


# Knowledge base directory
KB_DIR = Path(__file__).parent.parent / "knowledge"

# Cached documents
_documents = []  # List of {"title": str, "content": str, "sections": [{"title": str, "text": str}]}


def _load_knowledge_base() -> List[dict]:
    """Load all markdown files from knowledge/ directory."""
    global _documents
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

    return _documents


def _parse_markdown(text: str, title: str) -> dict:
    """Parse markdown into sections based on headings."""
    sections = []
    lines = text.split("\n")
    current_heading = title
    current_content = []

    for line in lines:
        if line.startswith("#"):
            # Save previous section
            if current_content:
                sections.append({
                    "title": current_heading,
                    "text": "\n".join(current_content).strip()
                })
            # New section
            level = len(re.match(r'^(#+)', line).group(1))
            heading_text = line.lstrip("# ").strip()
            if level <= 3:  # Only h1-h3 as section boundaries
                current_heading = heading_text
                current_content = []
            else:
                current_content.append(line)
        else:
            current_content.append(line)

    # Save last section
    if current_content:
        sections.append({
            "title": current_heading,
            "text": "\n".join(current_content).strip()
        })

    return {"title": title, "content": text, "sections": sections}


def _tokenize(text: str, include_title: bool = False) -> List[str]:
    """Chinese + English tokenizer.
    
    For Chinese: extracts n-grams (unigram..trigram) from contiguous Chinese runs.
    For English: splits on whitespace and keeps words with 2+ chars.
    Numbers are kept as-is.
    """
    # Extract contiguous Chinese character sequences
    chinese_runs = re.findall(r'[\u4e00-\u9fff]+', text)
    english_words = re.findall(r'[a-zA-Z]+', text)
    numbers = re.findall(r'\d+', text)
    
    tokens = []
    
    # Generate n-grams from each contiguous Chinese run
    for run in chinese_runs:
        chars = list(run)
        # Unigrams (individual characters) — lower weight at scoring time
        for c in chars:
            tokens.append(c)
        # Bigrams
        for i in range(len(chars) - 1):
            tokens.append(chars[i] + chars[i+1])
        # Trigrams (captures 3-char terms like "开发票", "质保期")
        for i in range(len(chars) - 2):
            tokens.append(chars[i] + chars[i+1] + chars[i+2])
    
    # English words (lowercase, 2+ chars)
    for w in english_words:
        if len(w) >= 2:
            tokens.append(w.lower())
    
    # Numbers
    for n in numbers:
        tokens.append(n)
    
    return tokens


def _compute_similarity(query_tokens: List[str], doc_tokens: List[str]) -> float:
    """Compute TF-IDF-like similarity between query and document."""
    if not query_tokens or not doc_tokens:
        return 0.0

    # Unique query terms (set-based for efficiency)
    query_set = set(query_tokens)
    doc_set = set(doc_tokens)

    # Jaccard-like overlap with BM25-style weighting
    overlap_terms = query_set & doc_set
    if not overlap_terms:
        return 0.0

    # Term frequency in doc (for IDF calculation)
    dtf = {}
    for t in doc_tokens:
        dtf[t] = dtf.get(t, 0) + 1

    # Score: sum of IDF weights for matching terms, boosted by coverage ratio
    overlap = 0.0
    for token in overlap_terms:
        df = dtf.get(token, 1)
        idf = math.log((1 + len(doc_tokens)) / (1 + df))
        # Single-char Chinese tokens get lower weight than bigrams/English words
        if len(token) == 1 and re.match(r'[\u4e00-\u9fff]', token):
            idf *= 0.5  # single char match is weaker signal
        overlap += max(idf, 0.1)

    # Coverage bonus: what fraction of query terms matched
    coverage = len(overlap_terms) / max(len(query_set), 1)
    
    return overlap * (0.5 + coverage * 0.5)


def retrieve(query: str, top_k: int = 3) -> List[dict]:
    """Retrieve most relevant knowledge base sections for a query.

    Uses text similarity + title boost. Section titles are strong relevance signals
    because they summarize the content (e.g., "保修政策" directly matches "保修多久").

    Args:
        query: User's question
        top_k: Number of sections to return

    Returns:
        List of {"title": str, "text": str, "score": float} sorted by relevance
    """
    docs = _load_knowledge_base()
    if not docs:
        return []

    query_tokens = _tokenize(query)
    scored_sections = []

    for doc in docs:
        for section in doc["sections"]:
            section_tokens = _tokenize(section["text"])
            score = _compute_similarity(query_tokens, section_tokens)

            # Title boost: section titles are strong relevance signals
            title_tokens = _tokenize(section["title"])
            if title_tokens:
                title_score = _compute_similarity(query_tokens, title_tokens)
                # Title matches count extra — multiply by 1.8x boost
                if title_score > 0:
                    score = score * 1.5 + title_score * 2.0

            if score > 0:
                scored_sections.append({
                    "title": section["title"],
                    "text": section["text"],
                    "score": score,
                    "source": doc["title"]
                })

    # Sort by score descending
    scored_sections.sort(key=lambda x: x["score"], reverse=True)
    return scored_sections[:top_k]


def build_context(query: str, max_length: int = 1500) -> str:
    """Build RAG context string to inject into LLM system prompt.

    Args:
        query: User's question
        max_length: Maximum context length in characters

    Returns:
        Formatted context string, or empty string if no relevant docs found
    """
    results = retrieve(query, top_k=3)
    if not results:
        return ""

    parts = ["\n## 参考资料（知识库）\n"]
    total_length = len(parts[0])

    for i, section in enumerate(results, 1):
        section_text = section["text"]
        # Truncate if too long
        if len(section_text) > 500:
            section_text = section_text[:500] + "..."

        block = f"\n### [{i}] {section['title']}\n{section_text}\n"

        if total_length + len(block) > max_length and i > 1:
            break

        parts.append(block)
        total_length += len(block)

    return "".join(parts)


def reload():
    """Force reload knowledge base (for hot updates)."""
    global _documents
    _documents = []
    return _load_knowledge_base()
