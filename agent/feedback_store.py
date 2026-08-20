# -*- coding: utf-8 -*-
"""PostgreSQL-backed feedback store for the self-improvement loop."""
from __future__ import annotations

import sqlite3
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
RATING_MIN = 0
RATING_MAX = 5


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
    """Persist self-improvement signals.

    Live application instances use PostgreSQL.  A SQLite path is accepted only
    as an explicit test/one-off compatibility adapter so the production code
    never silently falls back to a local file.
    """
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or "postgresql"
        self._sqlite = bool(db_path and str(db_path).lower() != "postgresql")
        if self._sqlite:
            self._init_sqlite_schema()
        else:
            init_runtime_schema()

    def _connect(self):
        if self._sqlite:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn
        return connect()

    def _init_sqlite_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS bad_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    session_id TEXT NOT NULL,
                    request_id TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL DEFAULT '',
                    answer TEXT NOT NULL DEFAULT '',
                    signal_type TEXT NOT NULL,
                    score REAL,
                    comment TEXT NOT NULL DEFAULT '',
                    trace_ref TEXT NOT NULL DEFAULT '',
                    processed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS session_last_query (
                    session_id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    ts REAL NOT NULL
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def _insert(self, *, session_id: str, request_id: str, query: str,
                answer: str, signal_type: str, score: Optional[float],
                comment: str, trace_ref: str) -> int:
        conn = self._connect()
        try:
            values = (time.time(), session_id, request_id, _redact_text(query),
                      _redact_text(answer), signal_type, score,
                      _redact_text(comment), trace_ref)
            if self._sqlite:
                row = conn.execute(
                    """INSERT INTO bad_cases
                       (ts,session_id,request_id,query,answer,signal_type,score,comment,trace_ref,processed)
                       VALUES (?,?,?,?,?,?,?, ?,?,0)""", values)
                result = int(row.lastrowid)
            else:
                row = conn.execute(
                    """INSERT INTO bad_cases
                       (ts,session_id,request_id,query,answer,signal_type,score,comment,trace_ref,processed)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0) RETURNING id""",
                    values,
                ).fetchone()
                result = int(row["id"])
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_rating(self, session_id: str, stars: int, *, request_id: str = "",
                      query: str = "", answer: str = "", trace_ref: str = "") -> Optional[int]:
        stars = int(stars)
        if not (RATING_MIN <= stars <= RATING_MAX):
            return None
        if stars >= LOW_RATING_THRESHOLD:
            return None
        return self._insert(session_id=session_id, request_id=request_id, query=query,
                            answer=answer, signal_type="rating", score=float(stars),
                            comment="", trace_ref=trace_ref)

    def record_reaction(self, session_id: str, emoji: str, active: bool, *,
                        request_id: str = "", query: str = "", answer: str = "",
                        trace_ref: str = "") -> Optional[int]:
        # Accept the actual Unicode thumbs-down glyph as well as API aliases.
        negative = {"\U0001f44e", "\U0001f44e\U0001f3fb", "dislike", "down", "thumbs_down", "thumbs-down"}
        if not active or str(emoji).strip().lower() not in negative:
            return None
        return self._insert(session_id=session_id, request_id=request_id, query=query,
                            answer=answer, signal_type="reaction", score=0.0,
                            comment=emoji, trace_ref=trace_ref)

    def record_feedback(self, session_id: str, query: str, answer: str,
                        rating: int, comment: str, *, request_id: str = "",
                        trace_ref: str = "") -> Optional[int]:
        rating = int(rating)
        if not (RATING_MIN <= rating <= RATING_MAX):
            return None
        if rating >= LOW_RATING_THRESHOLD and not comment.strip():
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
        conn = self._connect()
        try:
            placeholder = "?" if self._sqlite else "%s"
            row = conn.execute(
                f"SELECT query FROM session_last_query WHERE session_id={placeholder}",
                (session_id,),
            ).fetchone()
            if self._sqlite:
                conn.execute(
                    "INSERT OR REPLACE INTO session_last_query(session_id,query,ts) VALUES (?,?,?)",
                    (session_id, redacted, now),
                )
            else:
                conn.execute(
                    """INSERT INTO session_last_query(session_id,query,ts) VALUES (%s,%s,%s)
                       ON CONFLICT(session_id) DO UPDATE SET query=EXCLUDED.query, ts=EXCLUDED.ts""",
                    (session_id, redacted, now),
                )
            conn.commit()
        finally:
            conn.close()
        if row is None:
            return None
        previous = row["query"]
        similarity = SequenceMatcher(None, previous, redacted).ratio()
        if similarity <= REPEAT_SIMILARITY_THRESHOLD:
            return None
        return self._insert(session_id=session_id, request_id=request_id, query=query,
                            answer=answer, signal_type="repeat_question", score=similarity,
                            comment=f"similarity={similarity:.2f} prev={previous[:80]}",
                            trace_ref=trace_ref)

    def unprocessed_batch(self, limit: int = 200) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            placeholder = "?" if self._sqlite else "%s"
            rows = conn.execute(
                f"SELECT * FROM bad_cases WHERE processed=0 ORDER BY ts ASC LIMIT {placeholder}",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_processed(self, ids: Sequence[int]) -> int:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        conn = self._connect()
        try:
            if self._sqlite:
                marks = ",".join("?" for _ in ids)
                cur = conn.execute(f"UPDATE bad_cases SET processed=1 WHERE id IN ({marks})", tuple(ids))
            else:
                cur = conn.execute("UPDATE bad_cases SET processed=1 WHERE id = ANY(%s)", (ids,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def stats(self) -> Dict[str, Any]:
        conn = self._connect()
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
