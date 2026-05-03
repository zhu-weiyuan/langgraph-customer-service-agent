# -*- coding: utf-8 -*-
"""Test Agentic RAG retrieval."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from agent.agentic_rag import agentic_rag

tests = [
    "保修多久",
    "怎么退货",
    "快递几天到",
    "发票怎么开",
    "音箱连不上WiFi",
    "我要投诉",
]

for q in tests:
    print(f"\n{'='*60}")
    print(f"Query: {q}")
    print('='*60)
    result = agentic_rag(q, max_rounds=2)
    print(f"\nRounds: {result['rounds']}")
    print(f"Sufficient: {result['sufficient']}")
    print(f"Queries tried: {result['queries_tried']}")
    if result['context']:
        titles = [line.strip() for line in result['context'].split('###') if line.strip().startswith('[')]
        for t in titles[:5]:
            print(f"  → {t}")
