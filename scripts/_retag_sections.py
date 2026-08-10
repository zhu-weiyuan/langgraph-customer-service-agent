# -*- coding: utf-8 -*-
"""_retag_sections.py — 原地重标 rag_chunks.section（max-overlap，不重新嵌入）。

ingest 时按 chunk.start 落在哪个小节标注，会导致跨小节 chunk 被标成开头那节。
本脚本按“chunk 文本区间与各小节区间重叠面积最大”重标，更贴近真实内容归属。

用法：
    python scripts/_retag_sections.py            # 连库重标（需 .env PG_DSN）
    python scripts/_retag_sections.py --dry-run  # 只统计将变化的行数
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def section_for_span(start: int, length: int, sections) -> str:
    """max-overlap：返回与 [start, start+length) 重叠最多的小节标题。"""
    end = start + length
    best, best_overlap = "", 0
    for sec in sections:
        s = int(sec.get("start", 0))
        sec_end = int(sec.get("_end", 10 ** 12))
        overlap = min(end, sec_end) - max(start, s)
        if overlap > best_overlap:
            best_overlap = overlap
            best = str(sec.get("title") or "")
    return best


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from agent.hybrid_rag import chunk_document
    from agent.knowledge_sections import parse_markdown_sections
    from agent.pgvector_hybrid import PgHybridStore

    kb_dir = ROOT / "knowledge"
    mds = sorted(kb_dir.glob("*.md"))
    store = PgHybridStore.from_env(embed_fn=None)

    # 给每个 section 补 _end
    def with_ends(sections):
        for i, sec in enumerate(sections):
            sec["_end"] = int(sections[i + 1]["start"]) if i + 1 < len(sections) else 10 ** 12
        return sections

    total_changed = 0
    with store._connect() as conn:
        with conn.cursor() as cur:
            for md in mds:
                text = md.read_text(encoding="utf-8")
                chunked = chunk_document(text, doc_id=md.stem)
                sections = with_ends(parse_markdown_sections(text))
                updates = []  # (chunk_id, section)
                for pid, parent in chunked["parents"].items():
                    updates.append((pid, section_for_span(
                        int(parent.get("start", 0)), len(parent.get("text", "")), sections)))
                for child in chunked["children"]:
                    updates.append((child["child_id"], section_for_span(
                        int(child.get("start", 0)), len(child.get("text", "")), sections)))
                for cid, sec in updates:
                    if args.dry_run:
                        cur.execute("SELECT section FROM rag_chunks WHERE chunk_id = %s", (cid,))
                        row = cur.fetchone()
                        if row and row[0] != sec:
                            total_changed += 1
                    else:
                        cur.execute(
                            "UPDATE rag_chunks SET section = %s WHERE chunk_id = %s",
                            (sec, cid))
                        if cur.rowcount:
                            total_changed += cur.rowcount
                print(f"[retag] {md.name}: {len(updates)} chunks")
    print(f"[retag] {'would change' if args.dry_run else 'updated'} {total_changed} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
