#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ingest_knowledge.py — knowledge/*.md → chunk(child300/parent1200) → embed → pgvector

用法：
    python scripts/ingest_knowledge.py                # 真实导入（需 PG + OPENAI_API_KEY）
    python scripts/ingest_knowledge.py --dry-run      # 只统计分块，不连 PG、不嵌入
    python scripts/ingest_knowledge.py --prune-stale  # 显式删除知识目录中已移除的旧索引
    python scripts/ingest_knowledge.py --kb-dir path\\to\\knowledge

环境（.env 自动加载）：
    PG_DSN              默认 postgresql://postgres:postgres@localhost:5432/agent
    OPENAI_API_KEY      embedding 认证（必需，除非 --dry-run）
    OPENAI_BASE_URL     默认 https://api.openai.com/v1
    EMBEDDING_MODEL     默认 text-embedding-3-small
    RAG_INDEX_VERSION   默认 v1（写入 index_version 列，取其中数字部分）
"""

# .env 加载（脚本独立运行也生效；python-dotenv 缺失时静默跳过）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_KB_DIR = ROOT / "knowledge"
CHILD_SIZE = 300
PARENT_SIZE = 1200


# ── 纯函数（stdlib 单测覆盖）─────────────────────────────────

def parse_index_version(value: str, default: int = 1) -> int:
    """'v1' / '2' / 'v10-beta' → int（列类型为 INT；取首段数字，缺失用默认）。"""
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else default


def collect_chunk_stats(kb_dir: Path,
                        child_size: int = CHILD_SIZE,
                        parent_size: int = PARENT_SIZE) -> Dict:
    """扫描 kb_dir/*.md 并分块统计（不连 PG、不嵌入；目录缺失返回零值）。"""
    from agent.hybrid_rag import chunk_document

    stats = {"files": 0, "parents": 0, "children": 0, "chars": 0, "per_file": []}
    kb_dir = Path(kb_dir)
    if not kb_dir.is_dir():
        return stats
    for md in sorted(kb_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        chunked = chunk_document(text, child_size=child_size,
                                 parent_size=parent_size, doc_id=md.stem)
        entry = {"file": md.name, "chars": len(text),
                 "parents": len(chunked["parents"]),
                 "children": len(chunked["children"])}
        stats["per_file"].append(entry)
        stats["files"] += 1
        stats["chars"] += entry["chars"]
        stats["parents"] += entry["parents"]
        stats["children"] += entry["children"]
    return stats


# ── 真实导入 ─────────────────────────────────────────────────

def run_ingest(kb_dir: Path, child_size: int, parent_size: int,
               index_version: int, prune_stale: bool = False) -> int:
    from agent.embedding_client import EmbeddingClient
    from agent.pgvector_hybrid import PgHybridStore

    kb_dir = Path(kb_dir)
    md_files = sorted(kb_dir.glob("*.md")) if kb_dir.is_dir() else []
    if not md_files:
        print(f"[ingest] no *.md found under {kb_dir} — nothing to do")
        return 1

    client = EmbeddingClient.from_env(strict=True)  # 缺配置 → 清晰报错
    store = PgHybridStore.from_env(embed_fn=client.embed_one)

    print(f"[ingest] ensuring schema (equivalent to migrations/001_hybrid_rag.sql)")
    store.ensure_schema()

    # 语料清单 + 指纹（防删除危险：manifest 外旧文档会被清理）
    import hashlib
    manifest = {md.stem: md.name for md in md_files}
    h = hashlib.sha256()
    for md in md_files:
        h.update(md.name.encode("utf-8"))
        h.update(md.read_bytes())
    corpus_hash = h.hexdigest()[:12]
    print(f"[ingest] manifest={len(manifest)} files, corpus_hash={corpus_hash}")

    total = {"parents": 0, "children": 0}
    for i, md in enumerate(md_files, 1):
        text = md.read_text(encoding="utf-8")
        print(f"[ingest] ({i}/{len(md_files)}) {md.name} "
              f"({len(text)} chars) → chunk + embed + upsert ...")
        counts = store.upsert_document(
            doc_id=md.stem, text=text, title=md.stem, source=md.stem,
            tenant_id=None, tags=[], index_version=index_version,
            child_size=child_size, parent_size=parent_size)
        print(f"          parents={counts['parents']} children={counts['children']}")
        total["parents"] += counts["parents"]
        total["children"] += counts["children"]

    # 陈旧文档检测：库中 doc_id 不在当前 manifest → 删除（级联删 chunk）
    with store._connect().cursor() as cur:
        cur.execute("SELECT doc_id FROM rag_documents")
        existing = {r[0] for r in cur.fetchall()}
        stale = sorted(existing - set(manifest))
        if stale:
            print(f"[ingest] stale docs (deleted from KB but still indexed): {stale}")
            if prune_stale:
                cur.execute(
                    "DELETE FROM rag_documents WHERE doc_id = ANY(%s)", (stale,))
                print(f"[ingest] deleted {len(stale)} stale doc(s) + their chunks")
            else:
                print("[ingest] kept stale docs (safe default). Re-run with --prune-stale to delete them.")
        else:
            print("[ingest] no stale docs")
        cur.execute(
            "UPDATE rag_documents SET corpus_hash = %s WHERE doc_id = ANY(%s)",
            (corpus_hash, list(manifest)))

    print(f"[ingest] DONE files={len(md_files)} parents={total['parents']} "
          f"children={total['children']} index_version={index_version} "
          f"corpus_hash={corpus_hash}")
    print("[ingest] 验证：python scripts/eval_retrieval.py --backend pgvector")
    return 0


# ── CLI ──────────────────────────────────────────────────────

def main(argv: List[str] = None) -> int:
    import os
    ap = argparse.ArgumentParser(description="knowledge/*.md → pgvector 导入")
    ap.add_argument("--kb-dir", default=str(DEFAULT_KB_DIR))
    ap.add_argument("--child-size", type=int, default=CHILD_SIZE)
    ap.add_argument("--parent-size", type=int, default=PARENT_SIZE)
    ap.add_argument("--dry-run", action="store_true",
                    help="只统计分块（不连 PG、不嵌入）")
    ap.add_argument("--prune-stale", action="store_true",
                    help="显式删除 knowledge 目录中已不存在、但数据库仍保留的旧文档索引")
    args = ap.parse_args(argv)

    kb_dir = Path(args.kb_dir)

    if args.dry_run:
        stats = collect_chunk_stats(kb_dir, args.child_size, args.parent_size)
        print(f"[dry-run] kb_dir={kb_dir} files={stats['files']} "
              f"chars={stats['chars']} parents={stats['parents']} "
              f"children={stats['children']}")
        for entry in stats["per_file"]:
            print(f"  - {entry['file']}: {entry['chars']} chars → "
                  f"{entry['parents']} parents / {entry['children']} children")
        if stats["files"] == 0:
            print("[dry-run] 提示：目录不存在或无 *.md（容器内可能没有知识库，属预期）")
        return 0

    index_version = parse_index_version(
        os.environ.get("RAG_INDEX_VERSION", "v1"))
    return run_ingest(kb_dir, args.child_size, args.parent_size, index_version,
                      prune_stale=args.prune_stale)


if __name__ == "__main__":
    sys.exit(main())
