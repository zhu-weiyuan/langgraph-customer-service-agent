# -*- coding: utf-8 -*-
"""enrich_golden_v2.py — 步骤 3：golden_set_v2.jsonl 数据集增强。

为每条题补充（幂等，可重复跑）：
  1. golden_sections   — "src::小节标题" 列表（derive_golden_sections，只读 KB 推导）
  2. golden_chunk_ids  — 对应小节在 pgvector 里的 child chunk_id 列表（需先重建索引）
  3. intent            — 语义意图（取自 answer_type）
  4. emotion           — 情绪启发式标签（愤怒/紧急/受挫/中性）

用法：
    python eval/enrich_golden_v2.py            # 全量增强 + 写回 golden_set_v2.jsonl
    python eval/enrich_golden_v2.py --dry-run  # 只打印统计，不写回

依赖：agent.knowledge_sections（纯函数）；golden_chunk_ids 需要 PG 可达
（不可达时该字段留空并在报告中标出）。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATASET = ROOT / "eval" / "golden_set_v2.jsonl"

# 情绪启发式（保守，宁缺毋滥）
_EMOTION_ANGER = ("骂", "投诉", "气死", "火大", "太差", "垃圾", "坑人", "愤怒", "差评", "骗子")
_EMOTION_URGENT = ("急", "马上", "立刻", "尽快", "加急", "今天就要", "现在就")
_EMOTION_FRUSTRATED = ("为什么又", "总是", "一直", "还是不行", "老", "反复", "又坏了")


def derive_emotion(query: str) -> str:
    q = query or ""
    if any(w in q for w in _EMOTION_ANGER):
        return "anger"
    if any(w in q for w in _EMOTION_URGENT):
        return "urgent"
    if any(w in q for w in _EMOTION_FRUSTRATED):
        return "frustrated"
    return "neutral"


def fetch_chunk_ids(pairs):
    """从 pgvector 查 (doc_id, section) 对应的 child chunk_id。"""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        from agent.pgvector_hybrid import PgHybridStore
        store = PgHybridStore.from_env(embed_fn=None)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] PG 不可达，golden_chunk_ids 留空: {exc}")
        return {}
    out = {}
    with store._connect().cursor() as cur:
        for pair in sorted(set(pairs)):
            if "::" not in pair:
                continue
            src, _, title = pair.partition("::")
            # section 列匹配 OR 正文含小节标题（防跨小节 chunk 漏标）
            cur.execute(
                "SELECT chunk_id FROM rag_chunks "
                "WHERE doc_id = %s AND is_parent = FALSE "
                "AND (section = %s OR POSITION(%s IN content) > 0)",
                (src, title, title))
            ids = [r[0] for r in cur.fetchall()]
            out[pair] = ids
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    items = [json.loads(l) for l in open(DATASET, encoding="utf-8") if l.strip()]
    print(f"dataset: {DATASET} ({len(items)} items)")

    from agent.knowledge_sections import derive_golden_sections

    all_pairs = set()
    for it in items:
        all_pairs.update(derive_golden_sections(it))

    chunk_map = fetch_chunk_ids(all_pairs) if not args.dry_run else {}
    no_chunk_pairs = [p for p in all_pairs if p not in chunk_map or not chunk_map[p]]

    stats = {"with_sections": 0, "with_chunk_ids": 0, "no_section": []}
    for it in items:
        secs = derive_golden_sections(it)
        it["golden_sections"] = secs
        if secs:
            stats["with_sections"] += 1
        else:
            stats["no_section"].append(it["id"])
        ids = []
        for p in secs:
            ids.extend(chunk_map.get(p, []))
        it["golden_chunk_ids"] = sorted(set(ids))
        if it["golden_chunk_ids"]:
            stats["with_chunk_ids"] += 1
        it["intent"] = it.get("answer_type", "")
        it["emotion"] = derive_emotion(it.get("query", ""))

    print(f"sections: {stats['with_sections']}/{len(items)} items got golden_sections")
    if stats["no_section"]:
        print(f"  no-section items ({len(stats['no_section'])}): {stats['no_section']}")
    print(f"chunk_ids: {stats['with_chunk_ids']}/{len(items)} items got golden_chunk_ids")
    if no_chunk_pairs:
        print(f"  pairs without chunks ({len(no_chunk_pairs)}): "
              f"{no_chunk_pairs[:10]}{'...' if len(no_chunk_pairs) > 10 else ''}")
    print(f"emotion: {dict(Counter(it['emotion'] for it in items))}")
    print(f"intent:  {dict(Counter(it['intent'] for it in items))}")

    if args.dry_run:
        print("[dry-run] 未写回")
        return 0

    with open(DATETIME_PATH := DATASET, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    print(f"written: {DATASET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
