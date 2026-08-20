# -*- coding: utf-8 -*-
"""Quick diagnostic script for RAG evaluation issues."""

import sys
sys.path.insert(0, '.')

from agent.rag import retrieve, _load_knowledge_base

# Test queries from the benchmark
test_cases = [
    ("n01", "音箱突然不吭声了，一点动静都没有", ["音箱没声音/不出声/静音了"]),
    ("n02", "家里的智能灯怎么都连不上那个网关", ["Zigbee 设备配对失败"]),
    ("n03", "客厅那个小盒子红灯一直亮，所有设备都控制不了了", ["网关离线/掉线"]),
    ("n04", "喊了半天音箱都不理我", ["语音唤醒不灵敏/频繁误唤醒"]),
]

def _norm_sec(s):
    import re
    s = str(s or "")
    s = re.sub(r"[\s　]+", "", s)
    s = re.sub(r"[/\\\-–—～]+", "", s)
    return s.lower()

def _match(a, b):
    return bool(a) and bool(b) and (a in b or b in a)

print("=" * 70)
print("RAG Evaluation Diagnostic")
print("=" * 70)

_load_knowledge_base()

for cid, query, expected_sections in test_cases:
    print(f"\n[{cid}] Query: {query}")
    print(f"     Expected sections: {expected_sections}")

    results = retrieve(query, top_k=5)

    if not results:
        print("     ❌ No results returned!")
        continue

    print(f"     Retrieved {len(results)} results:")
    found_any = False
    for i, r in enumerate(results[:5], 1):
        title_norm = _norm_sec(r["title"])
        matches = [_match(_norm_sec(g), title_norm) for g in expected_sections]
        is_match = any(matches)

        status = "✓ MATCH" if is_match else "✗ no match"
        print(f"       [{i}] '{r['title']}' (score={r['score']:.2f}) {status}")

        if is_match:
            found_any = True

    if not found_any:
        print("     ⚠️  WARNING: None of the expected sections found!")
        print("         This explains 0 HitRate@5 for this query.")

print("\n" + "=" * 70)
print("Diagnostic complete.")
print("=" * 70)
