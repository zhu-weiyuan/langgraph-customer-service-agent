# -*- coding: utf-8 -*-
"""RAG evaluation module unit tests."""

from agent.rag import _load_knowledge_base
from agent.eval import evaluate, print_report, GROUND_TRUTH


def test_evaluate_returns_metrics():
    """Test that evaluate() returns all expected metrics."""
    _load_knowledge_base()
    metrics = evaluate(top_k=3)

    assert "HitRate@K" in metrics
    assert "Recall@K" in metrics
    assert "MRR" in metrics
    assert "Coverage" in metrics
    assert "NumQueries" in metrics
    assert "TopK" in metrics
    assert "Details" in metrics

    # All scores should be between 0 and 1
    for key in ["HitRate@K", "Recall@K", "MRR", "Coverage"]:
        assert 0.0 <= metrics[key] <= 1.0, f"{key} = {metrics[key]} out of range"

    print(f"  HitRate@3={metrics['HitRate@K']:.1%}, MRR={metrics['MRR']:.3f}")


def test_custom_queries():
    """Test evaluate with custom query set."""
    _load_knowledge_base()
    custom = [
        ("保修多久", ["warranty-service"]),
        ("怎么退货", ["returns-refunds"]),
    ]
    metrics = evaluate(queries=custom, top_k=2)

    assert metrics["NumQueries"] == 2
    assert metrics["TopK"] == 2


def test_ground_truth_coverage():
    """Test that ground truth covers all knowledge base docs."""
    _load_knowledge_base()

    # Check that each KB doc is referenced in at least one expected source
    kb_docs = {"faq", "product-manual", "returns-refunds", "shipping-logistics", "troubleshooting", "warranty-service"}
    covered = set()
    for _, sources in GROUND_TRUTH:
        for s in sources:
            covered.add(s)

    missing = kb_docs - covered
    assert not missing, f"Ground truth doesn't cover: {missing}"


def test_missed_query_identification():
    """Test that missed queries are properly identified."""
    _load_knowledge_base()
    metrics = evaluate(top_k=3)

    missed = [d for d in metrics["Details"] if not d["hit"]]
    # At least verify the structure is correct
    for m in missed:
        assert "query" in m
        assert "expected" in m
        assert "results_count" in m


if __name__ == "__main__":
    test_evaluate_returns_metrics()
    print("test_evaluate_returns_metrics PASSED")

    test_custom_queries()
    print("test_custom_queries PASSED")

    test_ground_truth_coverage()
    print("test_ground_truth_coverage PASSED")

    test_missed_query_identification()
    print("test_missed_query_identification PASSED")

    print("\nAll eval tests passed!")
