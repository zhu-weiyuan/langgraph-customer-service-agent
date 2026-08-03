"""Focused PostgreSQL storage regressions for observability and feedback triage."""

import os
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires the configured PostgreSQL runtime database"
)


def _session_id(prefix="feedback"):
    return f"{prefix}-{uuid.uuid4()}"


def test_trace_service_persists_jsonb_and_upserts():
    from agent.observability import TraceService, TraceSession

    service = TraceService()
    request_id = f"trace-{uuid.uuid4()}"
    trace = TraceSession(request_id, "test-session", "contact me at redacted@example.com")
    trace.add_event("request_start", {"status": "started"})
    service.save_trace(trace)
    trace.add_event("llm_generation", {"status": "completed"})
    service.save_trace(trace)

    stored = service.get_trace_by_id(request_id)
    assert stored["request_id"] == request_id
    assert isinstance(stored["trace_json"], dict)
    assert len(stored["trace_json"]["events"]) == 2


def test_low_rating_creates_review_queue_but_positive_does_not():
    from agent.memory import (
        ensure_conversation,
        enqueue_feedback_improvement,
        get_connection,
        record_feedback,
    )

    session_id = _session_id()
    ensure_conversation(session_id)
    bad_feedback = record_feedback(session_id, "rating", {"stars": 1})
    queue_id = enqueue_feedback_improvement(
        session_id, bad_feedback, "low_rating", severity="high", evidence={"stars": 1}
    )
    good_feedback = record_feedback(session_id, "rating", {"stars": 5})

    with get_connection() as conn:
        queued = conn.execute(
            "SELECT issue_type, severity, status FROM feedback_improvement_queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
        feedbacks = conn.execute(
            "SELECT COUNT(*) AS total FROM conversation_feedback WHERE id IN (?, ?)",
            (bad_feedback, good_feedback),
        ).fetchone()
    assert queued == {"issue_type": "low_rating", "severity": "high", "status": "open"}
    assert feedbacks["total"] == 2
