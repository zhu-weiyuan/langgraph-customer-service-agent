"""SQLite-backed persistence for repeatable evaluation results."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EvalResultStore:
    """Lazily initializes a local SQLite database for evaluation audit history."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else Path(__file__).with_name("eval_results.db")
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if not self._initialized:
            conn.execute("""CREATE TABLE IF NOT EXISTS eval_results (
                eval_id TEXT PRIMARY KEY, task_name TEXT NOT NULL, scores TEXT NOT NULL,
                timestamp TEXT NOT NULL, passed INTEGER NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_results_task ON eval_results(task_name)")
            conn.commit()
            self._initialized = True
        return conn

    def record_result(self, eval_id: str, task_name: str, scores: dict[str, Any], passed: bool,
                      timestamp: str | None = None) -> None:
        timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("""INSERT OR REPLACE INTO eval_results
                (eval_id, task_name, scores, timestamp, passed) VALUES (?, ?, ?, ?, ?)""",
                (eval_id, task_name, json.dumps(scores, ensure_ascii=False), timestamp, int(passed)))

    def get_results(self, task_name: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT eval_id, task_name, scores, timestamp, passed FROM eval_results"
        params: tuple[Any, ...] = ()
        if task_name is not None:
            query += " WHERE task_name = ?"
            params = (task_name,)
        query += " ORDER BY timestamp DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [{"eval_id": row["eval_id"], "task_name": row["task_name"],
                 "scores": json.loads(row["scores"]), "timestamp": row["timestamp"],
                 "passed": bool(row["passed"])} for row in rows]

    def export_to_json(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(self.get_results(), ensure_ascii=False, indent=2), encoding="utf-8")
        return destination
