# -*- coding: utf-8 -*-
"""Test BM25 RAG with eval module."""

from agent.rag import retrieve, _load_knowledge_base
_load_knowledge_base()

print("Testing BM25 retrieval:")
test_queries = [
    "快递几天到",
    "保修多久",
    "怎么退货",
    "音箱连不上蓝牙",
]

for q in test_queries:
    r = retrieve(q, top_k=2)
    print(f"\nQuery: '{q}'")
    for x in r:
        score = x.get("score", 0)
        title = x.get("title", "?")
        source = x.get("source", "?")
        print(f"  [{score:.4f}] {title} ({source})")

# Run eval
print("\n\nRunning full eval with BM25:")
from agent.eval import evaluate, print_report
metrics = evaluate(top_k=3)
print_report(metrics)
