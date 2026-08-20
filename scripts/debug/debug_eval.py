# -*- coding: utf-8 -*-
"""Debug script to check retrieval results structure."""

import json
import re

def _norm_sec(s):
    """归一化小节标题：移除空格、全角空格、斜杠等分隔符。"""
    s = str(s or "")
    # 移除各种空白字符
    s = re.sub(r"[\s　]+", "", s)
    # 移除常见的标题分隔符（斜杠、破折号等）
    s = re.sub(r"[/\\\-–—～]+", "", s)
    return s.lower()

def _match(a, b):
    return bool(a) and bool(b) and (a in b or b in a)

print("=" * 60)
print("Section Matching Test")
print("=" * 60)

golden_sections = [
    "音箱没声音/不出声/静音了",
    "Zigbee 设备配对失败",
    "网关离线/掉线",
    "语音唤醒不灵敏/频繁误唤醒",
]

kb_titles = [
    "音箱没声音/不出声/静音了",
    "Zigbee 设备配对失败",
    "网关离线/掉线",
    "语音唤醒不灵敏/频繁误唤醒",
    "通用排障流程",
    "音箱无法开机",
]

for gs in golden_sections:
    normalized_golden = _norm_sec(gs)
    print(f"\nGolden section: '{gs}'")
    print(f"  Normalized: '{normalized_golden}'")

    for kt in kb_titles:
        normalized_kb = _norm_sec(kt)
        match_result = _match(normalized_golden, normalized_kb)
        if match_result:
            print(f"  ✓ MATCH: '{kt}' (normalized: '{normalized_kb}')")
        # else:
        #     print(f"  ✗ No match: '{kt}' (normalized: '{normalized_kb}')")

# Test with actual retrieval simulation
print("\n" + "=" * 60)
print("Simulated Retrieval Test")
print("=" * 60)

# Simulate what BM25 might return for query "音箱突然不吭声了"
# (it won't find "音箱没声音" because "不吭声" != "没声音")
simulated_hits_plain = [
    {"title": "音箱无法开机", "source": "troubleshooting", "score": 0.5},
    {"title": "音箱 WiFi 连接失败", "source": "troubleshooting", "score": 0.4},
    {"title": "通用排障流程", "source": "troubleshooting", "score": 0.3},
]

# After Agentic RAG rewrites to "音箱没声音", it should find the right section
simulated_hits_agentic = [
    {"title": "音箱没声音/不出声/静音了", "source": "troubleshooting", "score": 0.9},
    {"title": "音箱有杂音/破音/声音断断续续", "source": "troubleshooting", "score": 0.7},
    {"title": "音箱无法开机", "source": "troubleshooting", "score": 0.5},
]

golden = ["音箱没声音/不出声/静音了"]
k = 5

print("\nPlain RAG (no rewrite):")
flags, matched, total = [], 0, len(golden)
gold_norm = [_norm_sec(g) for g in golden]
for h in simulated_hits_plain[:k]:
    title_norm = _norm_sec(h.get("title"))
    is_match = any(_match(g, title_norm) for g in gold_norm)
    flags.append(is_match)
    print(f"  '{h['title']}' -> match={is_match}")
matched = sum(1 for g in gold_norm if any(_match(g, _norm_sec(h.get("title"))) for h in simulated_hits_plain[:k]))
print(f"  HitRate@{k}: {sum(flags)/len(flags) if flags else 0:.1%}")

print("\nAgentic RAG (with rewrite):")
flags, matched, total = [], 0, len(golden)
for h in simulated_hits_agentic[:k]:
    title_norm = _norm_sec(h.get("title"))
    is_match = any(_match(g, title_norm) for g in gold_norm)
    flags.append(is_match)
    print(f"  '{h['title']}' -> match={is_match}")
matched = sum(1 for g in gold_norm if any(_match(g, _norm_sec(h.get("title"))) for h in simulated_hits_agentic[:k]))
print(f"  HitRate@{k}: {sum(flags)/len(flags) if flags else 0:.1%}")
