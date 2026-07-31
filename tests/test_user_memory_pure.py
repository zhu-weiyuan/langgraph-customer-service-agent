# -*- coding: utf-8 -*-
"""Phase 2 纯 stdlib 单测：用户身份 + 用户级长期记忆 + 压缩修正。

无第三方依赖（不装 PyJWT / langchain / numpy / httpx / psycopg 也能跑）；
embed_fn / llm_fn 全部 mock，DB 走 tempfile。

覆盖：
- JWT 签发/校验往返（含 tenant/scope、过期、篡改）与密码哈希。
- 记忆按 user_id 隔离（两 user 互不可见，SQL 层硬过滤）。
- 向量召回打分（relevance × importance × decay）与时间衰减单调性。
- 幂等去重（同批 + 跨批，源消息范围哈希）。
- 假设句不入库；PII 写入前脱敏。
- 压缩保留首尾（首 user query 在、中间被摘要、最近 N 轮在）。
- 旧库 ALTER 迁移（补 user_id 列 + 新建 sessions/users 表）。

运行：python tests/test_user_memory_pure.py  或  python -m unittest tests.test_user_memory_pure
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# JWT_SECRET 必须在调用 auth 前配置（auth 动态读 env，导入顺序无关，仍显式设置）。
os.environ.setdefault("JWT_SECRET", "unit-test-secret-key-0123456789abcdef")

from agent import auth  # noqa: E402
from agent import user_memory as um  # noqa: E402
from agent.user_memory import (MemoryStore, cosine, decay, score_memory,  # noqa: E402
                               redact_pii, is_hypothetical, dedup_key,
                               rule_extract)
from agent.context_compaction import (ContextCompactor, KEEP_RECENT_TURNS,  # noqa: E402
                                      HumanMessage, AIMessage)


# ── mock embedding：把文本映射到确定性向量，便于控制相似度 ──────────

def _hash_embed(text: str):
    """Deterministic 1024-dimensional vector matching production pgvector."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [(h[i % len(h)] / 255.0) for i in range(1024)]


def _onehot_embed_factory(vocab):
    """把关键词映射到正交 one-hot；query 命中同词 → cosine=1，否则 0。"""
    index = {w: i for i, w in enumerate(vocab)}
    n = 1024

    def embed(text: str):
        vec = [0.0] * n
        for w, i in index.items():
            if w in text:
                vec[i] = 1.0
        return vec or [0.0] * n
    return embed


def _temp_db():
    d = tempfile.mkdtemp()
    return os.path.join(d, "mem.db")


def _cleanup_test_memories(*user_ids):
    """???????????????????? PostgreSQL ???"""
    dsn = os.environ.get("PG_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        return
    try:
        import psycopg
        with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as conn:
            conn.execute("DELETE FROM user_memories WHERE user_id = ANY(%s)",
                         (list(user_ids),))
    except Exception:
        # ?????????????????????????
        pass


# ════════════════════════════════════════════════════════════════════
# 1) JWT 签发/校验往返 + 密码
# ════════════════════════════════════════════════════════════════════

class TestJWT(unittest.TestCase):
    def setUp(self):
        os.environ["JWT_SECRET"] = "unit-test-secret-key-0123456789abcdef"

    def test_roundtrip_claims(self):
        token = auth.create_access_token("alice", tenant="tenantA",
                                         ttl=60, scope="admin")
        claims = auth.verify_token(token)
        self.assertEqual(claims["sub"], "alice")
        self.assertEqual(claims["tenant_id"], "tenantA")
        self.assertEqual(claims.get("scope"), "admin")
        self.assertGreater(claims["exp"], claims["iat"])

    def test_expired_rejected(self):
        token = auth.create_access_token("bob", ttl=-5)
        with self.assertRaises(ValueError):
            auth.verify_token(token)

    def test_tampered_rejected(self):
        token = auth.create_access_token("carol", ttl=60)
        tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
        with self.assertRaises(ValueError):
            auth.verify_token(tampered)

    def test_wrong_secret_rejected(self):
        token = auth.create_access_token("dave", ttl=60)
        os.environ["JWT_SECRET"] = "a-completely-different-secret-key-xyz"
        with self.assertRaises(ValueError):
            auth.verify_token(token)

    def test_missing_secret_raises(self):
        os.environ["JWT_SECRET"] = ""
        try:
            with self.assertRaises(ValueError):
                auth.create_access_token("eve")
            with self.assertRaises(ValueError):
                auth.verify_token("x.y.z")
        finally:
            os.environ["JWT_SECRET"] = "unit-test-secret-key-0123456789abcdef"

    def test_stdlib_codec_roundtrip(self):
        # 直接验证 stdlib 降级路径（不经 PyJWT）。
        tok = auth._stdlib_jwt_encode({"sub": "x", "exp": int(time.time()) + 60},
                                      "s3cr3t")
        self.assertEqual(auth._stdlib_jwt_decode(tok, "s3cr3t")["sub"], "x")
        with self.assertRaises(ValueError):
            auth._stdlib_jwt_decode(tok, "wrong")

    def test_password_hash_verify(self):
        h = auth.hash_password("hunter2")
        self.assertTrue(auth.verify_password("hunter2", h))
        self.assertFalse(auth.verify_password("wrong", h))
        self.assertFalse(auth.verify_password("hunter2", "garbage"))
        # 不同 salt → 不同哈希
        self.assertNotEqual(h, auth.hash_password("hunter2"))


# ════════════════════════════════════════════════════════════════════
# 2) 记忆按 user_id 隔离
# ════════════════════════════════════════════════════════════════════

class TestUserIsolation(unittest.TestCase):
    def setUp(self):
        _cleanup_test_memories("alice", "bob")
        self.store = MemoryStore(db_path=_temp_db(), embed_fn=_hash_embed,
                                 ttl_days=30)

    def test_two_users_isolated(self):
        self.store.add_memory("alice", "alice 喜欢深色模式", kind="preference")
        self.store.add_memory("bob", "bob 的订单号 A123", kind="fact")

        alice_hits = self.store.recall("alice", "偏好", top_k=10)
        bob_hits = self.store.recall("bob", "订单", top_k=10)

        self.assertTrue(all(h["user_id"] == "alice" for h in alice_hits))
        self.assertTrue(all(h["user_id"] == "bob" for h in bob_hits))
        alice_contents = " ".join(h["content"] for h in alice_hits)
        self.assertNotIn("bob", alice_contents)

    def test_tenant_hard_filter(self):
        self.store.add_memory("u1", "租户A事实", tenant_id="A")
        self.store.add_memory("u1", "租户B事实", tenant_id="B")
        a = self.store.recall("u1", "事实", tenant_id="A", top_k=10)
        self.assertEqual([h["content"] for h in a], ["租户A事实"])

    def test_list_only_own(self):
        self.store.add_memory("alice", "a1")
        self.store.add_memory("bob", "b1")
        self.assertEqual(len(self.store.list_memories("alice")), 1)
        self.assertEqual(self.store.list_memories("alice")[0]["content"], "a1")

    def test_delete_scoped_to_owner(self):
        mid = self.store.add_memory("alice", "秘密", kind="fact")
        # bob 无法删 alice 的记忆
        self.assertFalse(self.store.delete_memory("bob", mid))
        self.assertEqual(len(self.store.list_memories("alice")), 1)
        # alice 本人可删（软删除后不再列出）
        self.assertTrue(self.store.delete_memory("alice", mid))
        self.assertEqual(len(self.store.list_memories("alice")), 0)

    def test_include_deleted_is_explicit(self):
        mid = self.store.add_memory("alice", "?????", kind="fact")
        self.assertTrue(self.store.delete_memory("alice", mid))
        self.assertEqual(self.store.list_memories("alice"), [])
        deleted = self.store.list_memories("alice", include_deleted=True)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["content"], "?????")


# ════════════════════════════════════════════════════════════════════
# 3) 向量召回打分与衰减
# ════════════════════════════════════════════════════════════════════

class TestScoringAndDecay(unittest.TestCase):
    def setUp(self):
        _cleanup_test_memories("u1")

    def test_decay_monotonic(self):
        self.assertEqual(decay(0), 1.0)
        self.assertLess(decay(86400), 1.0)
        self.assertLess(decay(30 * 86400), decay(86400))
        # 半衰期 ~30 天
        self.assertAlmostEqual(decay(30 * 86400), 0.5, places=2)

    def test_score_formula(self):
        # score = relevance × importance × decay
        self.assertAlmostEqual(score_memory(1.0, 1.0, 0), 1.0)
        self.assertAlmostEqual(score_memory(0.5, 0.5, 0), 0.25)
        self.assertLess(score_memory(1.0, 1.0, 86400),
                        score_memory(1.0, 1.0, 0))

    def test_cosine(self):
        self.assertAlmostEqual(cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(cosine([], [1]), 0.0)
        self.assertEqual(cosine([0, 0], [1, 1]), 0.0)

    def test_recall_ranks_by_relevance(self):
        embed = _onehot_embed_factory(["wifi", "退货", "音箱"])
        store = MemoryStore(db_path=_temp_db(), embed_fn=embed, ttl_days=30)
        store.add_memory("u1", "wifi 连不上", kind="issue", importance=0.5)
        store.add_memory("u1", "想要退货", kind="issue", importance=0.9)
        hits = store.recall("u1", "wifi", top_k=5)
        # query "wifi" 与 "wifi 连不上" 相关(cosine=1)，与"退货"正交(0)
        self.assertEqual(hits[0]["content"], "wifi 连不上")
        self.assertGreater(hits[0]["score"], 0)

    def test_expired_soft_deleted_not_recalled(self):
        store = MemoryStore(db_path=_temp_db(), embed_fn=_hash_embed,
                            ttl_days=30)
        # 手动写一条已过期的记忆
        store.add_memory("u1", "过期事实", kind="fact",
                         expires_at=um._iso(time.time() - 100))
        store.add_memory("u1", "有效事实", kind="fact")
        hits = store.recall("u1", "事实", top_k=10)
        contents = [h["content"] for h in hits]
        self.assertIn("有效事实", contents)
        self.assertNotIn("过期事实", contents)


# ════════════════════════════════════════════════════════════════════
# 4) 幂等去重 + 假设句 + PII
# ════════════════════════════════════════════════════════════════════

class TestExtractionGuards(unittest.TestCase):
    def setUp(self):
        _cleanup_test_memories("u1")
        self.store = MemoryStore(db_path=_temp_db(), embed_fn=_hash_embed,
                                 ttl_days=30)

    def test_dedup_key_stable(self):
        k1 = dedup_key("u1", "s1", ["hello world"])
        k2 = dedup_key("u1", "s1", ["hello  world"])  # 空白归一化
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, dedup_key("u2", "s1", ["hello world"]))

    def test_idempotent_same_source_no_duplicate(self):
        msgs = [{"role": "user", "content": "我住在上海"}]
        # llm_fn 每次返回同一条事实
        llm = lambda _t: [{"content": "用户住在上海", "kind": "fact",
                           "importance": 0.7}]
        r1 = self.store.extract_and_store("u1", msgs, source_session="s1",
                                          llm_fn=llm)
        r2 = self.store.extract_and_store("u1", msgs, source_session="s1",
                                          llm_fn=llm)
        self.assertEqual(len(r1["stored"]), 1)
        self.assertEqual(len(r2["stored"]), 0)   # 跨批去重
        self.assertGreaterEqual(r2["deduped"], 1)
        self.assertEqual(len(self.store.list_memories("u1")), 1)

    def test_within_batch_dedup(self):
        llm = lambda _t: [
            {"content": "重复事实", "kind": "fact", "importance": 0.5},
            {"content": "重复事实", "kind": "fact", "importance": 0.5},
        ]
        r = self.store.extract_and_store("u1", [{"role": "user", "content": "x"}],
                                         source_session="s1", llm_fn=llm)
        self.assertEqual(len(r["stored"]), 1)

    def test_hypothetical_not_stored(self):
        self.assertTrue(is_hypothetical("我可能要退货"))
        self.assertTrue(is_hypothetical("maybe I will cancel"))
        self.assertFalse(is_hypothetical("我要退货"))
        llm = lambda _t: [
            {"content": "用户也许想升级套餐", "kind": "preference", "importance": 0.6},
            {"content": "用户确认要升级套餐", "kind": "fact", "importance": 0.8},
        ]
        r = self.store.extract_and_store("u1", [{"role": "user", "content": "x"}],
                                         source_session="s1", llm_fn=llm)
        self.assertEqual(len(r["stored"]), 1)
        self.assertEqual(r["skipped_hypothetical"], 1)
        self.assertEqual(self.store.list_memories("u1")[0]["content"],
                         "用户确认要升级套餐")

    def test_pii_redacted_before_store(self):
        self.assertEqual(redact_pii("手机13800138000"), "手机[PHONE]")
        self.assertEqual(redact_pii("邮箱 a.b@c.com 结束"), "邮箱 [EMAIL] 结束")
        mid = self.store.add_memory("u1", "用户手机是13800138000")
        self.assertIsNotNone(mid)
        stored = self.store.list_memories("u1")[0]["content"]
        self.assertIn("[PHONE]", stored)
        self.assertNotIn("13800138000", stored)

    def test_rule_extract_fallback(self):
        # llm_fn=None → 规则降级
        text = "用户: wifi 一直报错连不上\n客服: 请重启\n用户: 我喜欢简短回复"
        items = rule_extract(text)
        kinds = {i["content"]: i["kind"] for i in items}
        self.assertEqual(kinds.get("wifi 一直报错连不上"), "issue")
        self.assertEqual(kinds.get("我喜欢简短回复"), "preference")


# ════════════════════════════════════════════════════════════════════
# 5) 压缩保留首尾（首 query + 中间摘要 + 最近 N 轮）
# ════════════════════════════════════════════════════════════════════

class TestCompactionKeepsFirstAndRecent(unittest.TestCase):
    def _build_messages(self, turns: int):
        msgs = []
        for i in range(turns):
            msgs.append(HumanMessage(content=f"U{i}"))
            msgs.append(AIMessage(content=f"A{i}"))
        return msgs

    def test_first_query_and_recent_preserved(self):
        compactor = ContextCompactor()
        # 确定性摘要，避免触发真实 LLM/Gateway
        compactor._compact_old_messages = lambda middle: "MIDDLE_SUMMARY"

        msgs = self._build_messages(10)   # 20 条消息，10 轮
        first = msgs[0]                   # U0（首个 user query，常含订单号/背景）
        result = compactor.maybe_compact(msgs, session_id="sX", force=True)

        self.assertTrue(result.compacted)
        self.assertEqual(result.summary, "MIDDLE_SUMMARY")
        # 首个 user query 必须仍在，且位于最前
        self.assertIs(result.messages[0], first)
        self.assertEqual(result.messages[0].content, "U0")
        # 最近 KEEP_RECENT_TURNS 轮（= 2*N 条）完整保留在尾部
        keep = KEEP_RECENT_TURNS * 2
        self.assertEqual(result.messages[-keep:], msgs[-keep:])
        self.assertEqual(result.messages[-1].content, "A9")
        # 结构 = [首 query] + 最近窗口
        self.assertEqual(len(result.messages), 1 + keep)
        # 中间轮次被摘要掉（U1..U4 的内容不再出现在保留消息里）
        preserved_contents = [m.content for m in result.messages]
        self.assertNotIn("U1", preserved_contents)
        self.assertNotIn("U4", preserved_contents)

    def test_no_middle_no_compaction(self):
        # 消息太少（首 turn 与最近窗口相邻）→ 不压缩
        compactor = ContextCompactor()
        msgs = self._build_messages(5)   # 10 条，keep=10 → 无中间
        result = compactor.maybe_compact(msgs, session_id="sY", force=True)
        self.assertFalse(result.compacted)
        self.assertEqual(result.messages, msgs)

    def test_cached_summary_still_keeps_first(self):
        compactor = ContextCompactor()
        compactor._compact_old_messages = lambda middle: "SUM1"
        msgs = self._build_messages(10)
        compactor.maybe_compact(msgs, session_id="sZ", force=True)  # 填缓存
        # 第二次：命中缓存分支，首 query 仍须保留
        result = compactor.maybe_compact(msgs, session_id="sZ", force=True)
        self.assertEqual(result.summary, "SUM1")
        self.assertEqual(result.messages[0].content, "U0")


# ════════════════════════════════════════════════════════════════════
# 6) 旧库 ALTER 迁移
# ════════════════════════════════════════════════════════════════════

@unittest.skip("legacy SQLite migration tests; live runtime is PostgreSQL + pgvector")
class TestMigration(unittest.TestCase):
    def _old_schema(self, conn):
        """模拟 Phase 1 旧库：conversation_history 等无 user_id 列，无 sessions/users。"""
        conn.executescript("""
            CREATE TABLE user_profiles (
                session_id TEXT PRIMARY KEY, name TEXT, preferred_name TEXT,
                language TEXT DEFAULT 'zh', created_at TEXT, updated_at TEXT);
            CREATE TABLE conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                user_message TEXT, bot_reply TEXT, intent TEXT, emotion TEXT,
                emotion_intensity INTEGER DEFAULT 1, resolved INTEGER DEFAULT 0,
                timestamp TEXT);
            CREATE TABLE user_preferences (
                session_id TEXT, product_interests TEXT, known_issues TEXT,
                communication_style TEXT, update_count INTEGER DEFAULT 1,
                UNIQUE(session_id, product_interests));
            CREATE TABLE tickets (
                ticket_id TEXT PRIMARY KEY, session_id TEXT, issue_category TEXT,
                description TEXT, resolution TEXT, satisfaction TEXT,
                priority TEXT, emotion TEXT, emotion_intensity INTEGER,
                message_count INTEGER, created_at TEXT);
        """)
        conn.execute(
            "INSERT INTO conversation_history (session_id, user_message, bot_reply,"
            " intent, emotion, timestamp) VALUES ('s1','hi','hello','chat',"
            "'neutral','2026-01-01T00:00:00')")
        conn.commit()

    def _cols(self, conn, table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    def test_alter_adds_user_id_and_new_tables(self):
        from agent import memory
        conn = sqlite3.connect(":memory:")
        try:
            self._old_schema(conn)
            # 迁移前：无 user_id 列
            self.assertNotIn("user_id", self._cols(conn, "conversation_history"))

            snapshot = memory.migrate(conn)

            # 迁移后：四张旧表都补上 user_id
            for tbl in ("user_profiles", "conversation_history",
                        "user_preferences", "tickets"):
                self.assertIn("user_id", self._cols(conn, tbl),
                              f"{tbl} missing user_id after migrate")
            # 新表存在
            self.assertIn("session_id", self._cols(conn, "sessions"))
            self.assertIn("user_id", self._cols(conn, "sessions"))
            self.assertIn("password_hash", self._cols(conn, "users"))
            # 旧数据不丢
            row = conn.execute(
                "SELECT user_message FROM conversation_history").fetchone()
            self.assertEqual(row[0], "hi")
            # 快照包含所有表
            self.assertIn("sessions", snapshot)
            self.assertIn("users", snapshot)
        finally:
            conn.close()

    def test_migrate_idempotent(self):
        from agent import memory
        conn = sqlite3.connect(":memory:")
        try:
            self._old_schema(conn)
            memory.migrate(conn)
            memory.migrate(conn)  # 再跑一次不应报错
            self.assertIn("user_id", self._cols(conn, "conversation_history"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
