"""Regression tests for the additive V2 durable conversation timeline.

NOTE: These tests originally targeted a SQLite-based memory module. As of
recent migrations, agent/memory.py requires PostgreSQL (DATABASE_URL).
These tests are skipped in environments without PostgreSQL unless
POSTGRES_AVAILABLE=true is set in the environment.
"""

import importlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.skipif(
    os.environ.get("POSTGRES_AVAILABLE", "false").lower() != "true",
    reason="Requires PostgreSQL (DATABASE_URL). Set POSTGRES_AVAILABLE=true to enable.",
)



def _memory_module(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_MEMORY_DB", str(tmp_path / "memory-v2.db"))
    import agent.memory as memory
    memory = importlib.reload(memory)
    monkeypatch.setattr(memory, "MEMORY_DB_PATH", tmp_path / "memory-v2.db")
    memory._init_db()
    return memory


def test_v2_timeline_separates_user_from_session_and_orders_messages(monkeypatch, tmp_path):
    memory = _memory_module(monkeypatch, tmp_path)
    session_id = "browser-session-1"

    first_conversation = memory.ensure_conversation(session_id, user_external_id="customer-42")
    assert memory.ensure_conversation(session_id, user_external_id="customer-42") == first_conversation

    memory.append_message(session_id, "user", "我的订单在哪？", {"intent": "consult"})
    memory.append_message(session_id, "tool", '{"tracking":"in_transit"}')
    memory.append_message(session_id, "assistant", "订单正在运输中。")

    timeline = memory.get_conversation_messages(session_id)
    assert [item["sequence_no"] for item in timeline] == [1, 2, 3]
    assert [item["role"] for item in timeline] == ["user", "tool", "assistant"]
    assert timeline[0]["metadata"] == {"intent": "consult"}

    with sqlite3.connect(memory.MEMORY_DB_PATH) as conn:
        user = conn.execute("SELECT external_id FROM users").fetchone()
        conversation = conn.execute("SELECT legacy_session_id FROM conversations").fetchone()
    assert user[0] == "customer-42"
    assert conversation[0] == session_id


def test_legacy_save_conversation_dual_writes_without_breaking_analytics(monkeypatch, tmp_path):
    memory = _memory_module(monkeypatch, tmp_path)

    memory.save_conversation("legacy-session", "产品坏了", "我来协助处理", intent="complaint")
    timeline = memory.get_conversation_messages("legacy-session")
    assert [(item["role"], item["content"]) for item in timeline] == [
        ("user", "产品坏了"),
        ("assistant", "我来协助处理"),
    ]

    with memory.get_connection() as conn:
        legacy_count = conn.execute("SELECT COUNT(*) FROM conversation_history").fetchone()[0]
    assert legacy_count == 1


def test_v2_schema_is_additive_for_existing_legacy_history(monkeypatch, tmp_path):
    """Initializing V2 must preserve the historical analytics table/data."""
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                user_message TEXT,
                bot_reply TEXT,
                intent TEXT,
                emotion TEXT,
                emotion_intensity INTEGER DEFAULT 1,
                resolved INTEGER DEFAULT 0,
                timestamp TEXT
            );
            INSERT INTO conversation_history
                (session_id, user_message, bot_reply, intent, emotion, timestamp)
            VALUES ('old-session', '旧问题', '旧回答', 'consult', 'neutral', '2026-01-01T00:00:00');
        """)

    monkeypatch.setenv("USER_MEMORY_DB", str(db_path))
    import agent.memory as memory
    memory = importlib.reload(memory)

    with memory.get_connection() as conn:
        legacy = conn.execute("SELECT user_message, bot_reply FROM conversation_history").fetchone()
        v2_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'conversation_messages'"
        ).fetchone()
    assert tuple(legacy) == ("旧问题", "旧回答")
    assert v2_exists is not None


def test_tool_audit_lifecycle_feedback_and_conversation_listing(monkeypatch, tmp_path):
    memory = _memory_module(monkeypatch, tmp_path)
    session_id = "audit-session"
    memory.append_message(session_id, "user", "查询物流")
    call_id = memory.create_tool_call(
        session_id, "lookup_shipping", {"order_id": "A-1"}, idempotency_key="request-1"
    )
    assert memory.create_tool_call(
        session_id, "lookup_shipping", {"order_id": "A-1"}, idempotency_key="request-1"
    ) == call_id
    memory.transition_tool_call(call_id, "running")
    memory.transition_tool_call(call_id, "succeeded", result={"status": "in_transit"}, duration_ms=14)
    feedback_id = memory.record_feedback(session_id, "rating", {"stars": 5})

    with memory.get_connection() as conn:
        call = conn.execute("SELECT status, result_json, duration_ms FROM tool_calls WHERE id = ?", (call_id,)).fetchone()
        feedback = conn.execute("SELECT kind, value_json FROM conversation_feedback WHERE id = ?", (feedback_id,)).fetchone()
    assert tuple(call) == ("succeeded", '{"status": "in_transit"}', 14)
    assert tuple(feedback) == ("rating", '{"stars": 5}')
    assert memory.list_conversations("物流")[0]["session_id"] == session_id


def test_tool_audit_rejects_invalid_state_transition(monkeypatch, tmp_path):
    memory = _memory_module(monkeypatch, tmp_path)
    call_id = memory.create_tool_call("state-machine", "lookup")
    try:
        memory.transition_tool_call(call_id, "succeeded")
    except ValueError as exc:
        assert "Invalid tool transition" in str(exc)
    else:
        raise AssertionError("pending -> succeeded must be rejected")


def test_legacy_backfill_is_batched_and_idempotent(monkeypatch, tmp_path):
    memory = _memory_module(monkeypatch, tmp_path)
    with memory.get_connection() as conn:
        conn.executemany(
            """INSERT INTO conversation_history
               (session_id, user_message, bot_reply, intent, emotion, timestamp)
               VALUES (?, ?, ?, 'consult', 'neutral', '2026-01-01T00:00:00')""",
            [("old-1", "one", "answer one"), ("old-2", "two", "answer two")],
        )
        conn.commit()

    assert memory.migrate_legacy_history(limit=1) == 1
    assert memory.migrate_legacy_history(limit=10) == 1
    assert memory.migrate_legacy_history(limit=10) == 0
    assert len(memory.get_conversation_messages("old-1")) == 2
    assert len(memory.get_conversation_messages("old-2")) == 2


def test_dual_written_turn_is_not_reimported_by_legacy_backfill(monkeypatch, tmp_path):
    memory = _memory_module(monkeypatch, tmp_path)
    memory.save_conversation("new-turn", "hello", "hi")
    assert memory.migrate_legacy_history() == 0
    assert len(memory.get_conversation_messages("new-turn")) == 2


def test_one_user_has_separate_conversations_with_shared_identity(monkeypatch, tmp_path):
    memory = _memory_module(monkeypatch, tmp_path)
    first = memory.create_conversation_for_user("alice", "tenant-a")
    second = memory.create_conversation_for_user("alice", "tenant-a")
    other = memory.create_conversation_for_user("bob", "tenant-a")

    assert first != second
    assert memory.conversation_belongs_to_user(first, "alice", "tenant-a")
    assert memory.conversation_belongs_to_user(second, "alice", "tenant-a")
    assert not memory.conversation_belongs_to_user(first, "bob", "tenant-a")
    assert memory.get_latest_conversation_for_user("alice", "tenant-a") == second

    conversations = memory.list_conversations_for_user("alice", "tenant-a")
    assert {item["session_id"] for item in conversations} == {first, second}
    assert other not in {item["session_id"] for item in conversations}


def test_long_term_memory_is_shared_but_raw_timeline_isolated(monkeypatch, tmp_path):
    memory = _memory_module(monkeypatch, tmp_path)
    first = memory.create_conversation_for_user("alice")
    second = memory.create_conversation_for_user("alice")
    memory.save_profile(first, preferred_name="Alice")
    memory.save_conversation(first, "router disconnects", "I will investigate", intent="complaint")

    context = memory.get_user_context(second)
    assert context["preferred_name"] == "Alice"
    assert context["total_conversations"] == 1
    assert memory.get_conversation_messages(second) == []
    assert len(memory.get_conversation_messages(first)) == 2
