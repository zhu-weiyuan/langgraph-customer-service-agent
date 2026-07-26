"""
Multi-turn Memory Module

跨会话用户记忆系统。使用 SQLite 持久化存储用户偏好和历史信息。

存储内容：
- 用户自报的姓名/称呼
- 历史咨询过的产品
- 之前的投诉记录
- 常用问题模式
"""

import contextlib
import sqlite3
import json
import os
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

# Memory database path
MEMORY_DB_PATH = Path(__file__).parent.parent / "user_memory.db"


def _get_connection() -> sqlite3.Connection:
    """Get SQLite connection with row factory."""
    conn = sqlite3.connect(str(MEMORY_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent read/write
    return conn


@contextlib.contextmanager
def get_connection():
    """Context manager for database connections. Ensures proper cleanup.

    Usage:
        with get_connection() as conn:
            conn.execute(...)
            conn.commit()
    """
    conn = _get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _init_db():
    """Initialize memory database tables."""
    conn = _get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                session_id TEXT PRIMARY KEY,
                name TEXT,
                preferred_name TEXT,
                language TEXT DEFAULT 'zh',
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS conversation_history (
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

            CREATE TABLE IF NOT EXISTS user_preferences (
                session_id TEXT,
                product_interests TEXT,       -- JSON array of products
                known_issues TEXT,            -- JSON array of past issues
                communication_style TEXT,     -- 'formal' / 'casual' / 'direct'
                update_count INTEGER DEFAULT 1,
                UNIQUE(session_id, product_interests)
            );

            CREATE INDEX IF NOT EXISTS idx_history_session ON conversation_history(session_id);
            CREATE INDEX IF NOT EXISTS idx_prefs_session ON user_preferences(session_id);

            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id TEXT PRIMARY KEY,
                session_id TEXT,
                issue_category TEXT,
                description TEXT,
                resolution TEXT,
                satisfaction TEXT,
                priority TEXT,
                emotion TEXT,
                emotion_intensity INTEGER,
                message_count INTEGER,
                created_at TEXT
            );
        """)
        conn.commit()
    finally:
        conn.close()


# Auto-init on import
_init_db()


def save_profile(session_id: str, name: Optional[str] = None,
                 preferred_name: Optional[str] = None,
                 language: str = 'zh'):
    """Save or update user profile."""
    now = datetime.now().isoformat()
    conn = _get_connection()
    try:
        conn.execute(
            """INSERT INTO user_profiles (session_id, name, preferred_name, language, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 name = COALESCE(EXCLUDED.name, user_profiles.name),
                 preferred_name = COALESCE(EXCLUDED.preferred_name, user_profiles.preferred_name),
                 language = COALESCE(EXCLUDED.language, user_profiles.language),
                 updated_at = EXCLUDED.updated_at""",
            (session_id, name, preferred_name, language, now, now)
        )
        conn.commit()
    finally:
        conn.close()


def get_profile(session_id: str) -> Dict[str, Any]:
    """Get user profile."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM user_profiles WHERE session_id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()

    if row:
        return dict(row)
    return {"session_id": session_id, "name": None, "preferred_name": None, "language": "zh"}


def save_conversation(session_id: str, user_message: str, bot_reply: str,
                      intent: str = 'consult', emotion: str = 'neutral',
                      emotion_intensity: int = 1, resolved: bool = False):
    """Save a conversation turn to history."""
    now = datetime.now().isoformat()
    conn = _get_connection()
    try:
        conn.execute(
            """INSERT INTO conversation_history
               (session_id, user_message, bot_reply, intent, emotion, emotion_intensity, resolved, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, user_message[:500], bot_reply[:500], intent, emotion, emotion_intensity, int(resolved), now)
        )

        # Extract product mentions from user message
        _update_product_interests(session_id, user_message)
        conn.commit()
    finally:
        conn.close()




def get_user_context(session_id: str) -> Dict[str, Any]:
    """Get full user context for session — profile + preferences + recent history.

    Returns data suitable for injecting into system prompt.
    """
    profile = get_profile(session_id)
    interests = []
    known_issues = []

    conn = _get_connection()
    try:
        prefs = conn.execute(
            "SELECT product_interests, known_issues FROM user_preferences WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        if prefs:
            if prefs['product_interests']:
                interests = json.loads(prefs['product_interests'])
            if prefs['known_issues']:
                known_issues = json.loads(prefs['known_issues'])

        # Recent unresolved issues
        recent_issues = conn.execute(
            """SELECT user_message, intent FROM conversation_history
               WHERE session_id = ? AND resolved = 0
               ORDER BY timestamp DESC LIMIT 3""",
            (session_id,)
        ).fetchall()

        # Total conversation count
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM conversation_history WHERE session_id = ?", (session_id,)
        ).fetchone()['cnt']
    finally:
        conn.close()

    return {
        "name": profile.get('name'),
        "preferred_name": profile.get('preferred_name'),
        "product_interests": interests,
        "known_issues": known_issues,
        "recent_unresolved": [dict(r) for r in recent_issues],
        "total_conversations": total,
    }


def build_memory_context(session_id: str) -> str:
    """Build memory context string to inject into system prompt.

    Returns formatted context or empty string if no prior memory.
    """
    context = get_user_context(session_id)

    parts = []
    parts.append("\n## 用户记忆")

    if context['name']:
        parts.append(f"- 用户姓名：{context['name']}")
    if context['preferred_name']:
        parts.append(f"- 希望被称呼为：{context['preferred_name']}")
    if context['product_interests']:
        products = '\u3001'.join(context['product_interests'])
        parts.append(f"- 关注产品：{products}")
    if context['total_conversations'] > 0:
        parts.append(f"- 历史对话次数：{context['total_conversations']}")

    if len(parts) <= 1:
        return ""

    return "\n".join(parts)


def mark_resolved(session_id: str):
    """Mark all unresolved conversations for this session as resolved."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE conversation_history SET resolved = 1 WHERE session_id = ? AND resolved = 0",
            (session_id,)
        )
        conn.commit()
    finally:
        conn.close()


def save_ticket(ticket: Dict[str, Any]):
    """Save a service ticket to the database."""
    conn = _get_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO tickets
               (ticket_id, session_id, issue_category, description, resolution,
                satisfaction, priority, emotion, emotion_intensity, message_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticket['ticket_id'], ticket.get('session_id', ''),
                ticket['issue_category'], ticket['description'], ticket['resolution'],
                ticket['satisfaction'], ticket['priority'],
                ticket.get('emotion', 'neutral'), ticket.get('emotion_intensity', 1),
                ticket.get('message_count', 0), ticket['created_at'],
            )
        )
        conn.commit()
    finally:
        conn.close()

def get_stats() -> Dict[str, Any]:
    """Get memory database statistics."""
    conn = _get_connection()
    try:
        sessions = conn.execute("SELECT COUNT(*) as cnt FROM user_profiles").fetchone()['cnt']
        conversations = conn.execute("SELECT COUNT(*) as cnt FROM conversation_history").fetchone()['cnt']
        unresolved = conn.execute("SELECT COUNT(*) as cnt FROM conversation_history WHERE resolved = 0").fetchone()['cnt']
    finally:
        conn.close()
    return {
        "unique_sessions": sessions,
        "total_conversations": conversations,
        "unresolved_issues": unresolved,
        "db_path": str(MEMORY_DB_PATH),
    }


def _update_product_interests(session_id: str, message: str):
    """Auto-detect product interests from user messages (enhanced with LLM-based extraction)."""
    # 原有关键词匹配（快速路径）
    product_keywords = {
        '智能音箱': ['音箱', 'speaker', '音响', '播放音乐'],
        '智能家居套装': ['智能家居', '网关', '灯泡', '插座', '传感器', 'zigbee'],
        '云服务': ['云', '存储', '订阅', '会员', '备份'],
    }

    detected = []
    for product, keywords in product_keywords.items():
        if any(kw in message for kw in keywords):
            detected.append(product)

    # 如果关键词匹配失败，用 LLM 提取（慢速路径）
    if not detected:
        from .llm_client import get_llm_client
        result = get_llm_client().chat_json(
            [{"role": "user", "content": f"从这句话中提取用户感兴趣的产品或功能：{message}\n返回JSON数组，如果没有则返回空数组。"}],
            "你是一个产品兴趣提取器。只返回JSON数组，不要其他文字。"
        )
        if result and isinstance(result, list):
            detected = [str(p) for p in result]

    if not detected:
        return

    conn = _get_connection()
    try:
        # 合并现有兴趣
        existing = conn.execute(
            "SELECT product_interests FROM user_preferences WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        if existing and existing['product_interests']:
            interests = json.loads(existing['product_interests'])
        else:
            interests = []

        for p in detected:
            if p not in interests:
                interests.append(p)

        if interests:
            conn.execute(
                """INSERT INTO user_preferences (session_id, product_interests)
                   VALUES (?, ?)
                   ON CONFLICT(session_id, product_interests) DO UPDATE SET
                     update_count = user_preferences.update_count + 1""",
                (session_id, json.dumps(interests, ensure_ascii=False))
            )
            conn.commit()
    finally:
        conn.close()
