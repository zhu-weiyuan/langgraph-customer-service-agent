# -*- coding: utf-8 -*-
"""RAG module unit test."""

from agent.rag import build_context, retrieve, reload

def test_retrieve():
    """Test RAG retrieval with various queries."""
    test_queries = [
        ("音箱怎么连WiFi", "WiFi connection"),
        ("设备离线怎么办", "Device offline"),
        ("保修多久", "Warranty period"),
        ("支持哪些音乐平台", "Music platforms"),
        ("怎么开发票", "Invoice"),
    ]

    print("=" * 60)
    print("RAG Retrieval Test")
    print("=" * 60)

    for query, desc in test_queries:
        results = retrieve(query, top_k=2)
        print(f"\nQuery: '{query}' ({desc})")
        if results:
            for r in results:
                print(f"  [{r['score']:.3f}] {r['title']} ({r['source']})")
                preview = r['text'][:80].replace('\n', ' ')
                print(f"    → {preview}...")
        else:
            print("  (no results)")

    # Test context building
    print("\n" + "=" * 60)
    print("Context Building Test")
    print("=" * 60)

    ctx = build_context("音箱连不上WiFi怎么办")
    print(f"\nQuery: '音箱连不上WiFi怎么办'")
    print(f"Context length: {len(ctx)} chars")
    sections = ctx.count("###") if ctx else 0
    print(f"Sections included: {sections}")

    if ctx:
        print("\nPreview:")
        print(ctx[:500] + "..." if len(ctx) > 500 else ctx)

    # Test with no match
    ctx_empty = build_context("abcdefg123456xyz")
    print(f"\nNo-match query context: '{ctx_empty}' (should be empty)")

    print("\nRAG tests complete!")


if __name__ == "__main__":
    test_retrieve()
