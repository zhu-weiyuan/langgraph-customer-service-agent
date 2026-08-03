from agent.remote_reranker import RemoteReranker


def test_remote_reranker_sorts_by_provider_scores():
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        return 200, {"results": [
            {"index": 1, "relevance_score": 0.91},
            {"index": 0, "relevance_score": 0.12},
        ]}

    rr = RemoteReranker("secret", "https://example.test/v1", "rerank",
                        transport=transport)
    results = [{"content": "first"}, {"content": "second"}]
    ranked = rr.rerank("query", results, top_n=2)
    assert [item["content"] for item in ranked] == ["second", "first"]
    assert ranked[0]["reranker_provider"] == "siliconflow"
    assert calls[0][1]["Authorization"] == "Bearer secret"
    assert calls[0][2]["documents"] == ["first", "second"]


def test_remote_reranker_falls_back_on_error():
    class Fallback:
        def rerank(self, query, results, top_n):
            return [{**results[0], "fallback": True}]

    rr = RemoteReranker(
        "secret", "https://example.test/v1", "rerank", fallback=Fallback(),
        transport=lambda *args: (503, {"error": "down"}),
    )
    ranked = rr.rerank("query", [{"content": "first"}, {"content": "second"}])
    assert ranked == [{"content": "first", "fallback": True}]
    assert rr.last_error and "503" in rr.last_error
