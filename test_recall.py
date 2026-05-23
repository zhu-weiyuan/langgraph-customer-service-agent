# -*- coding: utf-8 -*-
"""Quick recall test for RAG knowledge base."""
import sys, io
if not sys.stdin.isatty() is False and '__pytest' not in sys.modules:
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    except (AttributeError, ValueError):
        pass

from agent.rag import retrieve, _load_knowledge_base

tests = [
    ("保修多久", "should hit faq→售后与保修"),
    ("怎么退货", "should hit returns-refunds→退货流程"),
    ("快递几天到", "should hit shipping-logistics→配送时效"),
    ("发票怎么开", "should hit product-manual or faq"),
    ("音箱连不上WiFi", "should hit troubleshooting→WiFi连接失败"),
    ("产品有哪些", "should hit faq→产品介绍"),
    ("价格多少", "should hit pricing info"),
    ("我要投诉", "should hit complaint/feedback"),
]

docs = _load_knowledge_base()
total_sections = sum(len(d["sections"]) for d in docs)
print("Knowledge base: {} docs, {} sections\n".format(len(docs), total_sections))

for q, expectation in tests:
    results = retrieve(q, top_k=3)
    print("Q: {} (expect: {})".format(q, expectation))
    if not results:
        print("  FAIL: No results\n")
    else:
        for i, r in enumerate(results, 1):
            print("  [{}] score={:.2f} | {} ({})".format(i, r['score'], r['title'], r['source']))
    print()
