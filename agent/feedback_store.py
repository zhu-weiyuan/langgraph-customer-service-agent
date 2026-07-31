# -*- coding: utf-8 -*-
"""PostgreSQL-backed feedback store for the self-improvement loop."""
from __future__ import annotations

import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence

from agent.runtime_db import connect, init_runtime_schema

try:
    from agent.security.pii_redactor import redact as _pii_redact
except Exception:
    _pii_redact = None

SIGNAL_TYPES = ("rating", "reaction", "feedback", "escalation", "repeat_question")
LOW_RATING_THRESHOLD = 3
REPEAT_SIMILARITY_THRESHOLD = 0.6


def _redact_text(text: Optional[str]) -> str:
    if not text:
        return ""
    if _pii_redact is None:
        return text
    try:
        return _pii_redact(text).redacted_text
    except Exception:
        return text


def default_db_path() -> str:
    """Compatibility name; live storage is always PostgreSQL."""
    return "postgresql"


class FeedbackStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = "postgresql"
        init_runtime_schema()

    def _insert(self, *, session_id: str, request_id: str, query: str,
                answer: str, signal_type: str, score: Optional[float],
                comment: str, trace_ref: str) -> int:
        conn = connect()
        try:
            row = conn.execute(
                """INSERT INTO bad_cases
                   (ts,session_id,request_id,query,answer,signal_type,score,comment,trace_ref,processed)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0) RETURNING id""",
                (time.time(), session_id, request_id, _redact_text(query),
                 _redact_text(answer), signal_type, score,
                 _redact_text(comment), trace_ref),
            ).fetchone()
            conn.commit()
            return int(row["id"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_rating(self, session_id: str, stars: int, *, request_id: str = "",
                      query: str = "", answer: str = "", trace_ref: str = "") -> Optional[int]:
        if int(stars) >= LOW_RATING_THRESHOLD:
            return None
        return self._insert(session_id=session_id, request_id=request_id, query=query,
                            answer=answer, signal_type="rating", score=float(stars),
                            comment="", trace_ref=trace_ref)

    def record_reaction(self, session_id: str, emoji: str, active: bool, *,
                        request_id: str = "", query: str = "", answer: str = "",
                        trace_ref: str = "") -> Optional[int]:
        if not active or emoji not in ("??", "dislike", "down", "thumbs_down"):
            return None
        return self._insert(session_id=session_id, request_id=request_id, query=query,
                            answer=answer, signal_type="reaction", score=0.0,
                            comment=emoji, trace_ref=trace_ref)

    def record_feedback(self, session_id: str, query: str, answer: str,
                        rating: int, comment: str, *, request_id: str = "",
                        trace_ref: str = "") -> Optional[int]:
        if int(rating) >= LOW_RATING_THRESHOLD and not comment.strip():
            return None
        return self._insert(session_id=session_id, request_id=request_id, query=query,
                            answer=answer, signal_type="feedback", score=float(rating),
                            comment=comment, trace_ref=trace_ref)

    def record_escalation(self, session_id: str, *, query: str = "", answer: str = "",
                          reason: str = "", request_id: str = "", trace_ref: str = "") -> int:
        return self._insert(session_id=session_id, request_id=request_id, query=query,
                            answer=answer, signal_type="escalation", score=None,
                            comment=reason, trace_ref=trace_ref)

    def record_repeat_question(self, session_id: str, query: str, *, answer: str = "",
                               request_id: str = "", trace_ref: str = "") -> Optional[int]:
        if not query.strip():
            return None
        redacted = _redact_text(query)
        now = time.time()
        conn = connect()
        try:
            row = conn.execute("SELECT query FROM session_last_query WHERE session_id=%s", (session_id,)).fetchone()
            conn.execute(
                """INSERT INTO session_last_query(session_id,query,ts) VALUES (%s,%s,%s)
                   ON CONFLICT(session_id) DO UPDATE SET query=EXCLUDED.query, ts=EXCLUDED.ts""",
                (session_id, redacted, now))
            conn.commit()
        finally:
            conn.close()
        if row is None:
            return None
        similarity = SequenceMatcher(None, row["query"], redacted).ratio()
        if similarity <= REPEAT_SIMILARITY_THRESHOLD:
            return None
        return self._insert(session_id=session_id, request_id=request_id, query=query,
                            answer=answer, signal_type="repeat_question", score=similarity,
                            comment=f"similarity={similarity:.2f} prev={row['query'][:80]}",
                            trace_ref=trace_ref)

    def unprocessed_batch(self, limit: int = 200) -> List[Dict[str, Any]]:
        conn = connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM bad_cases WHERE processed=0 ORDER BY ts ASC LIMIT %s",
                (int(limit),)).fetchall()]
        finally:
            conn.close()

    def mark_processed(self, ids: Sequence[int]) -> int:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        conn = connect()
        try:
            cur = conn.execute("UPDATE bad_cases SET processed=1 WHERE id = ANY(%s)", (ids,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def stats(self) -> Dict[str, Any]:
        conn = connect()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM bad_cases").fetchone()["n"]
            unprocessed = conn.execute("SELECT COUNT(*) AS n FROM bad_cases WHERE processed=0").fetchone()["n"]
            rows = conn.execute("SELECT signal_type, COUNT(*) AS n FROM bad_cases GROUP BY signal_type").fetchall()
            avg = conn.execute("SELECT AVG(score) AS n FROM bad_cases WHERE score IS NOT NULL").fetchone()["n"]
            return {"total": total, "unprocessed": unprocessed,
                    "by_signal_type": {r["signal_type"]: r["n"] for r in rows},
                    "avg_score": round(float(avg), 3) if avg is not None else None}
        finally:
            conn.close()
