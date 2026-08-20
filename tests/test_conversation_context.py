from langchain_core.messages import AIMessage, HumanMessage

from agent.nodes import _is_history_recall_query
from agent.memory import timeline_langchain_messages


def test_postgres_timeline_becomes_model_history(monkeypatch):
    monkeypatch.setattr(
        "agent.memory.get_conversation_messages",
        lambda _session_id, limit=100: [
            {"role": "user", "content": "我上次说过的问题"},
            {"role": "assistant", "content": "我记得，正在处理"},
        ],
    )

    messages = timeline_langchain_messages("session-1")

    assert [type(message) for message in messages] == [HumanMessage, AIMessage]
    assert [message.content for message in messages] == ["我上次说过的问题", "我记得，正在处理"]


def test_postgres_timeline_deduplicates_checkpoint_replay(monkeypatch):
    monkeypatch.setattr(
        "agent.memory.get_conversation_messages",
        lambda _session_id, limit=100: [
            {"role": "user", "content": "同一个问题"},
            {"role": "assistant", "content": "同一个回答"},
            {"role": "user", "content": "同一个问题"},
            {"role": "assistant", "content": "同一个回答"},
        ],
    )

    messages = timeline_langchain_messages("session-1")

    assert [message.content for message in messages] == ["同一个问题", "同一个回答"]


def test_history_recall_queries_skip_agentic_rag():
    assert _is_history_recall_query("我刚才问你的订单问题，你让我提供哪两项信息？")
    assert _is_history_recall_query("不是哥们，你失忆了吗，你刚才说了什么？")
    assert not _is_history_recall_query("订单物流什么时候到？")
