from unittest.mock import patch

from agent.agentic_rag import agentic_rag
from agent.nodes import build_reply_context


def test_agentic_rag_fails_closed_when_evidence_is_insufficient():
    hit = {
        "title": "智能家居套装",
        "text": "支持远程监控和定时任务，但没有空调控制说明。",
        "score": 2.0,
        "source": "product-manual",
    }
    with patch("agent.agentic_rag._rewrite_query", return_value=["空调温度控制"]), patch(
        "agent.agentic_rag.rag_retrieve", return_value=[hit]
    ), patch(
        "agent.agentic_rag._evaluate",
        return_value={"sufficient": False, "reason": "仅有相关功能", "new_queries": []},
    ):
        result = agentic_rag("可以远程把空调调到26度吗？", max_rounds=1)

    assert result["sufficient"] is False
    assert result["context"] == ""


def test_agentic_rag_rejects_related_results_without_the_requested_object():
    hit = {
        "title": "智能家居套装",
        "text": "支持远程监控、定时任务和智能插座。",
        "score": 2.0,
        "source": "product-manual",
    }
    with patch("agent.agentic_rag._rewrite_query", return_value=["空调温度控制"]), patch(
        "agent.agentic_rag.rag_retrieve", return_value=[hit]
    ):
        result = agentic_rag("可以远程把空调调到26度吗？", max_rounds=1)

    assert result["sufficient"] is False
    assert result["context"] == ""


def test_context_requires_explicit_knowledge_evidence_when_rag_is_empty():
    with patch("agent.nodes.agentic_rag", return_value={
        "context": "", "rounds": 1, "queries_tried": ["空调温度控制"], "sufficient": False,
    }):
        result = build_reply_context(
            messages=[],
            intent="consult",
            user_query="可以远程把空调调到26度吗？",
            need_rag=True,
        )

    assert "知识库证据状态" in result["system_prompt"]
    assert "不得根据常识" in result["system_prompt"]
