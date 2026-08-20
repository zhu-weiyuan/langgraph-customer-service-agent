#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于真实 TF-IDF 检索结果，为 golden_set_v2 生成 noise_probe 样本

策略：
1. 对每个样本跑检索 (top_k=10)
2. 找出 score>阈值 但 source 不在 golden_context_ids 的 chunk → 噪声候选
3. 选取最迷惑的噪声（score 最高的非相关 chunk）
4. 生成 probe 样本：注明 injected_noise_chunk_id, noise_position
"""

import json
import os
import sys
from pathlib import Path
from copy import deepcopy

# 修复导入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 强制 TF-IDF 无向量模式
os.environ['RAG_BACKEND'] = 'tfidf'

# Monkey patch rag.retrieve to disable vector
import agent.rag as rag
_orig_retrieve = rag.retrieve
def _tfidf_only_retrieve(query, top_k=3, use_vector=False, **kwargs):
    return _orig_retrieve(query, top_k=top_k, use_vector=False, **kwargs)
rag.retrieve = _tfidf_only_retrieve

from agent.rag_backend import retrieve
from agent.hybrid_rag import chunk_document

BASE = Path(__file__).resolve().parent.parent
V2_PATH = BASE / "eval" / "golden_set_v2.jsonl"
OUTPUT_PATH = BASE / "eval" / "golden_set_v2_noise_probes.jsonl"

# 噪声判定阈值
MIN_NOISE_SCORE = 8.0  # TF-IDF 分数阈值，低于此不视为有迷惑性的噪声
MAX_PROBES_PER_CATEGORY = 3  # 每类最多生成几个 probe


def load_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line.strip()) for line in f if line.strip()]


def save_jsonl(path: Path, data):
    with path.open("w", encoding="utf-8") as f:
        for obj in data:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _real_child_manifest():
    """Return actual child chunks from the production chunker, keyed by ID."""
    manifest = {}
    kb_dir = BASE / "knowledge"
    for path in sorted(kb_dir.glob("*.md")):
        if path.name == "README.md" or path.name.startswith("_"):
            continue
        chunked = chunk_document(
            path.read_text(encoding="utf-8"), child_size=300,
            parent_size=1200, doc_id=path.stem,
        )
        for child in chunked.get("children", []):
            manifest[child["child_id"]] = {
                "id": child["child_id"],
                "source": path.stem,
                "title": child.get("section") or path.stem,
                "text": child.get("text", ""),
            }
    return manifest


def _overlap_score(a: str, b: str) -> float:
    a_chars = set((a or "").replace(" ", ""))
    b_chars = set((b or "").replace(" ", ""))
    return len(a_chars & b_chars) / max(1, len(a_chars))


def find_noise_chunks(query: str, golden_context_ids: list, top_k: int = 10):
    """Return candidates with IDs from the real production chunk manifest.

    The legacy version guessed ``source:p0:c0`` for every hit. That silently
    created probes pointing at arbitrary chunks. A candidate is now accepted
    only when its retrieved section can be mapped to a real child chunk.
    """
    hits = retrieve(query, top_k=top_k)
    manifest = _real_child_manifest()
    noise = []
    for hit in hits:
        source = hit.get("source", "")
        if source in golden_context_ids:
            continue
        text = hit.get("text") or hit.get("content") or ""
        candidates = [c for c in manifest.values() if c["source"] == source]
        if not candidates:
            continue
        best = max(candidates, key=lambda c: _overlap_score(text, c["text"]))
        overlap = _overlap_score(text, best["text"])
        if overlap < 0.35 or float(hit.get("score", 0) or 0) < MIN_NOISE_SCORE:
            continue
        noise.append({
            "chunk_id": best["id"],
            "score": float(hit.get("score", 0) or 0),
            "source": source,
            "title": hit.get("title") or best["title"],
            "text_preview": best["text"][:120],
            "mapping_overlap": round(overlap, 4),
        })
    return noise


def main():
    print("=" * 60)
    print("🔍 基于真实 TF-IDF 检索生成 noise_probe 样本")
    print("=" * 60)
    
    samples = load_jsonl(V2_PATH)
    print(f"加载 {len(samples)} 条样本")
    
    # 统计每类已有多少 probe
    category_counts = {}
    probes = []
    
    for idx, sample in enumerate(samples):
        if idx % 10 == 0:
            print(f"  进度: {idx}/{len(samples)}")
        
        sid = sample["id"]
        category = sample.get("category", "unknown")
        query = sample.get("query") or sample.get("question")
        golden_context_ids = sample.get("golden_context_ids", [])
        
        # 跳过已有 noise_probe 的、拒答题、无检索需求的
        if sample.get("noise_probe") or sample.get("should_refuse") or not golden_context_ids:
            continue
        
        # 类别配额控制
        if category_counts.get(category, 0) >= MAX_PROBES_PER_CATEGORY:
            continue
        
        noise_chunks = find_noise_chunks(query, golden_context_ids)
        
        if not noise_chunks:
            continue
        
        # 选 score 最高的噪声
        best_noise = noise_chunks[0]
        
        # 生成 probe 样本
        probe = deepcopy(sample)
        probe["id"] = f"{sid}-noise-{category_counts.get(category, 0) + 1:02d}"
        probe["base_id"] = sid
        probe["noise_probe"] = True
        probe["injected_noise_chunk_id"] = best_noise["chunk_id"]
        probe["injected_noise_source"] = best_noise["source"]
        probe["injected_noise_score"] = best_noise["score"]
        probe["injected_noise_title"] = best_noise["title"]
        probe["noise_position"] = 2  # 插在第 2 位最迷惑
        # 清理派生字段
        probe.pop("conversation", None)  # 保持简单
        
        probes.append(probe)
        category_counts[category] = category_counts.get(category, 0) + 1
        print(f"  ✅ {probe['id']} <- 噪声: {best_noise['source']} ({best_noise['score']:.2f}) [{best_noise['title'][:40]}]")
    
    print(f"\n📊 生成 {len(probes)} 条 noise_probe 样本")
    for cat, cnt in sorted(category_counts.items()):
        print(f"  {cat}: {cnt}")
    
    # Probes are a separate experiment and must never change the core golden set.
    save_jsonl(OUTPUT_PATH, probes)
    print(f"\n💾 已保存独立 probes: {OUTPUT_PATH} ({len(probes)} 条)")
    print(f"ℹ️ 核心集保持不变: {V2_PATH} ({len(samples)} 条)")


if __name__ == "__main__":
    main()